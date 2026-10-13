"""A deliberately small, GET-only HTTP server.

Routes are an exact-match table. Nothing a visitor sends reaches a file path or a format string.
PC Health itself only reads. Three routes concern terminals and are held to a stricter access rule:
  /api/terminal      the visitor names a tmux session from the list; the pane to read comes from the
                     monitor's own table, never from the request.
  /api/conversation  earlier turns of the conversation in that session, read from the session's own
                     record. Again only a session name (and where to read) comes from the visitor.
  /tty/...           passed through, unchanged, to the separate terminal service (ttyd) on a private
                     socket. That is where typing happens; PC Health only carries the connection.
"""

import os
import re
import select
import socket
import socketserver
import struct
import threading
import time
import zlib

from . import history, procfs

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/look.js": ("look.js", "text/javascript; charset=utf-8"),
    "/nav.js": ("nav.js", "text/javascript; charset=utf-8"),         # the bar of terminals, on the terminal page
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon-warn.svg": ("favicon-warn.svg", "image/svg+xml"),
    "/favicon-crit.svg": ("favicon-crit.svg", "image/svg+xml"),
    "/terminal": ("terminal.html", "text/html; charset=utf-8"),      # a session you can type in
    "/watch": ("terminal.html", "text/html; charset=utf-8"),         # the same page, view only
    "/terminal.js": ("terminal.js", "text/javascript; charset=utf-8"),
}
SESSION_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Headers handed on to the terminal service; everything else from the browser is dropped.
TTY_HEADERS = (b"host", b"origin", b"user-agent", b"accept", b"accept-encoding", b"accept-language",
               b"sec-websocket-key", b"sec-websocket-version", b"sec-websocket-protocol", b"sec-websocket-extensions")
REQUEST_LINE = re.compile(rb"^([A-Z]{3,8}) (/[A-Za-z0-9._~/?=&%+-]{0,300}) HTTP/1\.([01])$")
MAX_HEADER_BYTES = 8192
MAX_CONNECTIONS = 64            # an open terminal holds one for as long as it is open
IDLE_TIMEOUT_S = 12
SECURITY_HEADERS = (
    b"X-Content-Type-Options: nosniff\r\n"
    b"Referrer-Policy: no-referrer\r\n"
    b"X-Frame-Options: DENY\r\n"
    b"Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'\r\n"
)
REASONS = {200: b"OK", 303: b"See Other", 304: b"Not Modified", 400: b"Bad Request", 401: b"Unauthorized",
           403: b"Forbidden", 404: b"Not Found", 405: b"Method Not Allowed", 431: b"Headers Too Large",
           502: b"Bad Gateway"}
PRIVATE_NETS = ((0x0A000000, 0xFF000000), (0xAC100000, 0xFFF00000), (0xC0A80000, 0xFFFF0000),
                (0xA9FE0000, 0xFFFF0000))
LOGIN_PAGE = (b"<!doctype html><html lang=en><meta charset=utf-8>"
              b"<meta name=viewport content='width=device-width, initial-scale=1'><title>PC Health</title>"
              b"<link rel=stylesheet href=/app.css><main class=login><h1>PC Health</h1>"
              b"<form method=get action=/login><label for=c>Access code</label>"
              b"<input id=c name=code type=password autocomplete=current-password autofocus>"
              b"<button>Open</button></form></main>")


def ip_int(text):
    try:
        return struct.unpack("!I", socket.inet_aton(text))[0]
    except (OSError, struct.error):
        return None


def parse_routes(raw):
    """/proc/net/route -> [(network, mask)] of directly connected IPv4 subnets."""
    nets = []
    for line in raw.decode("ascii", "replace").splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dest = int.from_bytes(bytes.fromhex(f[1]), "little")
            gateway = int(f[2], 16)
            mask = int.from_bytes(bytes.fromhex(f[7]), "little")
        except ValueError:
            continue
        if gateway == 0 and mask not in (0, 0xFFFFFFFF):
            nets.append((dest, mask))
    return nets


def is_loopback(ip):
    return ip is not None and (ip >> 24) == 127


def client_allowed(client, nets, lan):
    """Loopback always; with LAN access, also private addresses on a subnet this PC is attached to."""
    ip = ip_int(client)
    if ip is None:
        return False
    if is_loopback(ip):
        return True
    if not lan or not any(ip & m == n for n, m in PRIVATE_NETS):
        return False
    return any(ip & mask == net for net, mask in nets)


def lan_addresses():
    """This PC's addresses on directly connected private subnets."""
    out = []
    for net, mask in parse_routes(procfs.read("/proc/net/route", 1 << 14) or b""):
        if not any(net & m == n for n, m in PRIVATE_NETS):
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((socket.inet_ntoa(struct.pack("!I", net | 1)), 9))   # no packet is sent
                addr = s.getsockname()[0]
        except OSError:
            continue
        if addr not in out and not addr.startswith("127."):
            out.append(addr)
    return out


def page_urls(cfg):
    port = cfg["port"]
    urls = ["http://localhost:%d" % port]
    if cfg["listen"] == "lan":
        urls.append("http://%s.local:%d" % (socket.gethostname(), port))
        urls.extend("http://%s:%d" % (a, port) for a in lan_addresses())
    return urls


def gzip_bytes(data):
    c = zlib.compressobj(5, zlib.DEFLATED, 31)
    return c.compress(data) + c.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, cfg, monitor):
        self.cfg = cfg
        self.mon = monitor
        self.port = cfg["port"]
        self.lan = cfg["listen"] == "lan"
        self.code = cfg.get("access_code") or ""
        self.protect_page = cfg.get("protect") == "page"
        self.terminal_view = cfg.get("terminal_view", "local")
        self.terminal_type = cfg.get("terminal_type", "off")
        self.tty_socket = cfg.get("terminal_socket", "")
        self.cookie = ""
        if self.code:
            import hashlib
            import hmac
            self._hmac = hmac
            self.cookie = hmac.new(self.code.encode(), b"pc-health-cookie", hashlib.sha256).hexdigest()
        self.hostname = socket.gethostname().lower()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.nets = []
        self.nets_at = 0.0
        self.static = {}
        for route, (name, ctype) in STATIC.items():
            data = procfs.read(os.path.join(STATIC_DIR, name), 1 << 21) or b""
            self.static[route] = (data, gzip_bytes(data), ctype.encode(), b'"%08x"' % zlib.crc32(data))
        self._gz = {}                   # range -> (raw bytes object, gzipped)
        super().__init__(address, Handler)

    def connected_nets(self):
        now = time.monotonic()
        if now - self.nets_at > 60 or not self.nets_at:
            self.nets = parse_routes(procfs.read("/proc/net/route", 1 << 14) or b"")
            self.nets_at = now
        return self.nets

    def verify_request(self, request, client_address):
        return client_allowed(client_address[0], self.connected_nets(), self.lan)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)      # too many open connections: refuse rather than queue
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass                                    # a broken connection is not worth a traceback

    def history_gz(self, rng):
        raw = self.mon.history_bytes(rng)
        cached = self._gz.get(rng)
        if cached is None or cached[0] is not raw:
            cached = self._gz[rng] = (raw, gzip_bytes(raw))
        return cached


class Handler(socketserver.StreamRequestHandler):
    timeout = IDLE_TIMEOUT_S

    def handle(self):
        for _ in range(2000):                   # requests per connection
            try:
                line = self.rfile.readline(2050)
            except OSError:
                return
            if not line:
                return
            line = line.rstrip(b"\r\n")
            if not line:
                continue
            headers = self._read_headers()
            if headers is None:
                return self._send(431, b"headers too large\n", close=True)
            m = REQUEST_LINE.match(line)
            if m is None:
                return self._send(400, b"bad request\n", close=True)
            method, target, minor = m.group(1), m.group(2).decode("ascii"), m.group(3)
            if method != b"GET":
                return self._send(405, b"read-only: GET only\n", close=True, extra=b"Allow: GET\r\n")
            keep = minor == b"1" and headers.get(b"connection", b"").lower() != b"close"
            if not self._origin_ok(headers):
                return self._send(403, b"forbidden\n", close=True)
            path, _, query = target.partition("?")
            if not self._route(path, query, headers, keep) or not keep:
                return

    def _read_headers(self):
        headers = {}
        size = 0
        for _ in range(80):
            try:
                line = self.rfile.readline(MAX_HEADER_BYTES + 2)
            except OSError:
                return None
            size += len(line)
            if size > MAX_HEADER_BYTES or not line:
                return None
            line = line.rstrip(b"\r\n")
            if not line:
                return headers
            name, sep, value = line.partition(b":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return None

    def _origin_ok(self, headers):
        """Refuse requests a web page on another site could be making through the browser."""
        srv = self.server
        allowed = {"localhost", "127.0.0.1", srv.hostname, srv.hostname + ".local",
                   self.request.getsockname()[0]}

        def host_ok(value):
            host, sep, port = value.lower().rpartition(":")
            if not sep:
                host, port = value.lower(), ""
            return host in allowed and port in ("", str(srv.port))
        host = headers.get(b"host", b"").decode("ascii", "replace")
        if not host_ok(host):
            return False
        origin = headers.get(b"origin")
        if origin is not None:
            o = origin.decode("ascii", "replace")
            if not o.startswith("http://") or not host_ok(o[7:]):
                return False
        return headers.get(b"sec-fetch-site", b"").lower() != b"cross-site"

    def _authorised(self, headers):
        srv = self.server
        if not srv.code or is_loopback(ip_int(self.client_address[0])):
            return True
        for part in headers.get(b"cookie", b"").decode("ascii", "replace").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "pch" and srv._hmac.compare_digest(value, srv.cookie):
                return True
        return False

    def _route(self, path, query, headers, keep):
        srv = self.server
        gz_ok = b"gzip" in headers.get(b"accept-encoding", b"")
        if path == "/healthz":
            age = time.time() - srv.mon.snapshot.get("ts", 0)
            return self._send(200, b"ok %d %d\n" % (srv.mon.seq, age), keep=keep, ctype=b"text/plain")
        if path == "/login" and srv.code:
            given = ""
            for pair in query.split("&"):
                k, _, v = pair.partition("=")
                if k == "code":
                    given = v
            if srv._hmac.compare_digest(given.encode(), srv.code.encode()):
                cookie = b"Set-Cookie: pch=%s; HttpOnly; SameSite=Strict; Max-Age=31536000; Path=/\r\nLocation: /\r\n" \
                    % srv.cookie.encode()
                return self._send(303, b"", keep=keep, extra=cookie)
            time.sleep(1.0)                     # slow down guessing
            return self._send(401, LOGIN_PAGE, keep=keep, ctype=b"text/html; charset=utf-8")
        if path == "/api/terminal":
            return self._terminal(query, headers, keep)
        if path == "/api/conversation":
            return self._conversation(query, headers, keep, gz_ok)
        if path == "/api/me":
            body = b'{"local":%s,"watch":%d,"type":%d}' % (
                b"true" if is_loopback(ip_int(self.client_address[0])) else b"false",
                self._may(srv.terminal_view, headers), self._may(srv.terminal_type, headers))
            return self._send(200, body, keep=keep, ctype=b"application/json", extra=b"Cache-Control: no-store\r\n")
        if path == "/tty" or path.startswith("/tty/"):
            status = self._may(srv.terminal_type, headers)
            if status != 200:
                return self._send(status, b"terminals are not available to this device\n", keep=keep)
            return self._proxy_tty(path + ("?" + query if query else ""), headers)
        if path in ("/terminal", "/watch", "/terminal.js"):
            may_type, may_view = self._may(srv.terminal_type, headers), self._may(srv.terminal_view, headers)
            off = {"/terminal": may_type == 404, "/watch": may_view == 404,
                   "/terminal.js": may_type == 404 and may_view == 404}[path]       # the script serves both pages
            if off:
                return self._send(404, b"not found\n", keep=keep)
        if self.server.protect_page and not self._authorised(headers):
            if path in ("/app.css",):
                pass                            # the login page needs its stylesheet
            elif path == "/":
                return self._send(401, LOGIN_PAGE, keep=keep, ctype=b"text/html; charset=utf-8")
            else:
                return self._send(401, b"access code required\n", keep=keep)
        if path == "/api/snapshot":
            if query != "peek=1":             # the dashboard is on screen: sample at the fast rate
                srv.mon.viewed(self.client_address[0])
            if query == "rescan=1":           # a terminal was just started, renamed or closed: look again now
                srv.mon.rescan()
            return self._send(200, srv.mon.snapshot_bytes, keep=keep, ctype=b"application/json",
                              extra=b"Cache-Control: no-store\r\n")
        if path == "/api/history":
            rng = "1h"
            for pair in query.split("&"):
                k, _, v = pair.partition("=")
                if k == "range" and v in history.RANGES:
                    rng = v
            raw, gz = srv.history_gz(rng)
            body, enc = (gz, b"Content-Encoding: gzip\r\n") if gz_ok else (raw, b"")
            return self._send(200, body, keep=keep, ctype=b"application/json",
                              extra=b"Cache-Control: no-store\r\nVary: Accept-Encoding\r\n" + enc)
        entry = srv.static.get(path)
        if entry is not None:
            raw, gz, ctype, etag = entry
            extra = b"Cache-Control: no-cache\r\nVary: Accept-Encoding\r\nETag: " + etag + b"\r\n"
            if headers.get(b"if-none-match") == etag:
                return self._send(304, b"", keep=keep, extra=extra)
            if gz_ok:
                return self._send(200, gz, keep=keep, ctype=ctype, extra=extra + b"Content-Encoding: gzip\r\n")
            return self._send(200, raw, keep=keep, ctype=ctype, extra=extra)
        return self._send(404, b"not found\n", keep=keep)

    def _may(self, mode, headers):
        """May this client use a terminal feature set to `mode`? -> 200, 401 (enter the code), 403 or 404."""
        if mode == "off":
            return 404
        if is_loopback(ip_int(self.client_address[0])):
            return 200
        if mode != "lan":
            return 403
        if self.server.code and not self._authorised(headers):
            return 401
        return 200

    def _terminal(self, query, headers, keep):
        """The screen of a tmux session, read-only."""
        srv = self.server
        js = b"application/json"
        nostore = b"Cache-Control: no-store\r\n"
        status = self._may(srv.terminal_view, headers)
        if status != 200:
            reason = {404: b"off", 403: b"local", 401: b"code"}[status]
            return self._send(status, b'{"error":"%s"}' % reason, keep=keep, ctype=js, extra=nostore)
        name = ""
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "session":
                name = v
        body = srv.mon.terminal(name) if SESSION_NAME.match(name) else None
        if body is None:
            return self._send(404, b'{"error":"gone"}', keep=keep, ctype=js, extra=nostore)
        return self._send(200, body, keep=keep, ctype=js, extra=nostore)

    def _conversation(self, query, headers, keep, gz_ok):
        """Earlier turns of the conversation in a tmux session, for whoever may view that session."""
        srv = self.server
        js = b"application/json"
        nostore = b"Cache-Control: no-store\r\n"
        status = self._may(srv.terminal_view, headers)
        if status != 200:
            reason = {404: b"off", 403: b"local", 401: b"code"}[status]
            return self._send(status, b'{"error":"%s"}' % reason, keep=keep, ctype=js, extra=nostore)
        name, before, after = "", None, None
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "session":
                name = v
            elif k in ("before", "after") and v.isdigit() and len(v) <= 15:
                before, after = (int(v), after) if k == "before" else (before, int(v))
        body = srv.mon.conversation(name, before, after) if SESSION_NAME.match(name) else None
        if body is None:
            return self._send(404, b'{"error":"none"}', keep=keep, ctype=js, extra=nostore)
        if gz_ok and len(body) > 2048:
            return self._send(200, gzip_bytes(body), keep=keep, ctype=js, extra=nostore + b"Content-Encoding: gzip\r\n")
        return self._send(200, body, keep=keep, ctype=js, extra=nostore)

    def _proxy_tty(self, target, headers):
        """Hand the connection to the terminal service and carry bytes both ways until either side closes.

        The service listens on a private socket, so this is the only way in: every request has already
        passed the client-address, Host and Origin checks above.
        """
        upgrade = headers.get(b"upgrade", b"").lower() == b"websocket"
        lines = [b"GET " + target.encode("ascii") + b" HTTP/1.1"]
        lines += [name + b": " + headers[name] for name in TTY_HEADERS if name in headers]
        lines.append(b"Connection: Upgrade\r\nUpgrade: websocket" if upgrade else b"Connection: close")
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.settimeout(5)
        try:
            up.connect(self.server.tty_socket)
            up.sendall(b"\r\n".join(lines) + b"\r\n\r\n")
        except OSError:
            up.close()
            return self._send(502, b"The terminal service is not running. Start it with ./install-terminal.sh\n",
                              close=True)
        client = self.request
        client.settimeout(None)
        up.settimeout(None)
        idle = 120 if upgrade else 20        # the service pings open terminals every few seconds
        watching = [client, up]
        try:
            while up in watching:
                ready, _, _ = select.select(watching, [], [], idle)
                if not ready:
                    break
                for src in ready:
                    try:
                        data = src.recv(65536)
                    except OSError:
                        data = b""
                    if src is up:
                        if not data:
                            watching.remove(up)           # the service is done: so are we
                            break
                        client.sendall(data)
                    elif data:
                        up.sendall(data)
                    else:
                        # The browser has finished sending; let the reply still in flight arrive.
                        watching.remove(client)
                        try:
                            up.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
        except OSError:
            pass
        finally:
            up.close()
        return False

    def _send(self, status, body, keep=False, close=False, ctype=b"text/plain; charset=utf-8", extra=b""):
        keep = keep and not close
        head = b"HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: %s\r\n" % (
            status, REASONS.get(status, b"OK"), ctype, len(body), b"keep-alive" if keep else b"close")
        try:
            self.wfile.write(head + SECURITY_HEADERS + extra + b"\r\n" + body)
            self.wfile.flush()
        except OSError:
            return False
        return keep


def make_server(cfg, monitor):
    host = "0.0.0.0" if cfg["listen"] == "lan" else "127.0.0.1"
    return Server((host, cfg["port"]), cfg, monitor)
