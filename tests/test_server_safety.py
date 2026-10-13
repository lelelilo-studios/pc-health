"""The page is exposed on the home network, so the server's refusals and the "never leak a
secret, never change anything" properties are tested, not assumed."""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unittest

from pchealth import config, httpd
from pchealth.sampler import Monitor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "pchealth")


class FakeMonitor:
    seq = 7
    snapshot = {"ts": 0}
    snapshot_bytes = b'{"seq":7}'

    def __init__(self):
        self.viewers = []

    def viewed(self, client):
        self.viewers.append(client)

    def rescan(self):
        self.rescans = getattr(self, "rescans", 0) + 1

    def history_bytes(self, rng):
        return json.dumps({"range": rng}).encode()

    def terminal(self, session):
        self.asked = session
        return b'{"text":"screen"}' if session == "work" else None

    def conversation(self, session, before=None, after=None):
        self.asked_talk = (session, before, after)
        return b'{"items":[]}' if session == "work" else None


def start(cfg_over=None):
    cfg = dict(config.DEFAULTS, port=0, listen="local", **(cfg_over or {}))
    mon = FakeMonitor()
    srv = httpd.Server(("127.0.0.1", 0), cfg, mon)
    srv.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, mon


def raw(srv, data, read=65536):
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(read)
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks)


def get(srv, path, headers=(), host=None):
    host = host or "127.0.0.1:%d" % srv.port
    req = "GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n" % (path, host)
    req += "".join(h + "\r\n" for h in headers) + "\r\n"
    resp = raw(srv, req.encode())
    head, _, body = resp.partition(b"\r\n\r\n")
    return int(head.split()[1]), head.decode("latin1"), body


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.mon = start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_page_and_api(self):
        status, head, body = get(self.srv, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>PC Health</title>", body)
        self.assertIn("Content-Security-Policy: default-src 'self'", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)
        self.assertNotIn("Access-Control-Allow-Origin", head)
        status, head, body = get(self.srv, "/api/snapshot")
        self.assertEqual((status, body), (200, b'{"seq":7}'))
        self.assertIn("Cache-Control: no-store", head)
        self.assertEqual(self.mon.viewers[-1], "127.0.0.1")
        seen = len(self.mon.viewers)
        self.assertEqual(get(self.srv, "/api/snapshot?peek=1")[0], 200)      # a look that does not count as watching
        self.assertEqual(len(self.mon.viewers), seen)
        self.assertEqual(get(self.srv, "/api/snapshot?rescan=1")[0], 200)    # "look at the tmux sessions again now"
        self.assertEqual(self.mon.rescans, 1)
        self.assertEqual(json.loads(get(self.srv, "/api/history?range=7d")[2])["range"], "7d")
        self.assertEqual(json.loads(get(self.srv, "/api/history?range=../../etc")[2])["range"], "1h")
        self.assertEqual(get(self.srv, "/healthz")[0], 200)
        status, head, body = get(self.srv, "/look.js")             # sets the look before the page is drawn
        self.assertEqual(status, 200)
        self.assertIn("Content-Type: text/javascript", head)
        self.assertIn(b"pch-look", body)
        status, head, body = get(self.srv, "/nav.js")              # the bar of terminals, on the terminal page
        self.assertEqual(status, 200)
        self.assertIn("Content-Type: text/javascript", head)
        for page in ("/terminal", "/watch"):                       # loaded before the page's own script, which calls it
            html = get(self.srv, page)[2]
            self.assertLess(html.index(b'<script src="/nav.js" defer>'), html.index(b".js\" defer></script>\n</head>"), page)
        self.assertNotIn(b"nav.js", get(self.srv, "/")[2])         # the main page has no such bar: it has the Terminals section

    def test_static_is_cached_by_etag(self):
        _, head, _ = get(self.srv, "/app.js")
        etag = re.search(r"ETag: (\S+)", head).group(1)
        self.assertEqual(get(self.srv, "/app.js", ["If-None-Match: " + etag])[0], 304)

    def test_only_get(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            resp = raw(self.srv, ("%s /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 0\r\n\r\n"
                                  % (method, self.srv.port)).encode())
            self.assertTrue(resp.startswith(b"HTTP/1.1 405"), method)

    def test_unknown_paths_never_reach_files(self):
        for path in ("/../../etc/passwd", "/app.js/../../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/static/app.js",
                     "/pchealth/httpd.py", "//etc/passwd", "/api/snapshot/", "/API/SNAPSHOT"):
            status, _, body = get(self.srv, path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"root:", body)

    def test_foreign_host_or_origin_is_refused(self):
        self.assertEqual(get(self.srv, "/", host="evil.example")[0], 403)                    # DNS rebinding
        self.assertEqual(get(self.srv, "/", host="127.0.0.1:1")[0], 403)
        self.assertEqual(get(self.srv, "/api/snapshot", ["Origin: http://evil.example"])[0], 403)
        self.assertEqual(get(self.srv, "/api/snapshot", ["Sec-Fetch-Site: cross-site"])[0], 403)
        self.assertEqual(get(self.srv, "/api/snapshot", ["Origin: http://localhost:%d" % self.srv.port])[0], 200)
        self.assertEqual(get(self.srv, "/", host="localhost:%d" % self.srv.port)[0], 200)

    def test_malformed_requests(self):
        for data in (b"\x00\x01\x02\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"GET /" + b"a" * 5000 + b" HTTP/1.1\r\n\r\n",
                     b"GET /<script> HTTP/1.1\r\nHost: x\r\n\r\n"):
            resp = raw(self.srv, data)
            self.assertTrue(resp.startswith(b"HTTP/1.1 400") or resp == b"", data[:20])
        big = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n" + b"X-Pad: " + b"a" * 9000 + b"\r\n\r\n"
        self.assertTrue(raw(self.srv, big).startswith(b"HTTP/1.1 431"))

    def test_idle_connections_do_not_block_the_page(self):
        idle = [socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) for _ in range(40)]
        try:
            time.sleep(0.2)
            for s in idle[:10]:
                s.close()
            time.sleep(0.2)
            self.assertEqual(get(self.srv, "/healthz")[0], 200)
        finally:
            for s in idle:
                s.close()


class Terminals(unittest.TestCase):
    """Terminals are the sensitive part of the page, so who may reach them is tested, not assumed."""

    def serve(self, **cfg):
        srv, mon = start(cfg)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, mon

    def other_device(self, srv, cookie=False):
        """A handler as it would be for a request from another device on the home network."""
        import types
        sent = {}
        h = types.SimpleNamespace(server=srv, client_address=("192.168.68.77", 5555),
                                  _authorised=lambda headers: cookie,
                                  _send=lambda status, body, **kw: sent.update(status=status, body=body))
        h._may = lambda mode, headers: httpd.Handler._may(h, mode, headers)
        h._terminal = lambda q, hd, k: httpd.Handler._terminal(h, q, hd, k)
        h._conversation = lambda q, hd, k, gz: httpd.Handler._conversation(h, q, hd, k, gz)
        h._proxy_tty = lambda target, hd: sent.update(status=200, proxied=target)
        return h, sent

    def test_this_pc_can_watch_a_session(self):
        srv, mon = self.serve()
        status, head, body = get(srv, "/api/terminal?session=work")
        self.assertEqual((status, body), (200, b'{"text":"screen"}'))
        self.assertIn("no-store", head)
        self.assertEqual(get(srv, "/api/terminal?session=gone")[0], 404)

    def test_only_plain_session_names_reach_the_monitor(self):
        srv, mon = self.serve()
        mon.asked = None
        for bad in ("", "%250", "a%3Bb", "..%2F..%2Fetc", "a+b", "x" * 200):
            self.assertEqual(get(srv, "/api/terminal?session=" + bad)[0], 404, bad)
        self.assertIsNone(mon.asked)

    def test_earlier_conversation_only_takes_a_name_and_a_place(self):
        srv, mon = self.serve()
        self.assertEqual(get(srv, "/api/conversation?session=work")[0::2], (200, b'{"items":[]}'))
        self.assertEqual(mon.asked_talk, ("work", None, None))
        get(srv, "/api/conversation?session=work&before=4096")
        self.assertEqual(mon.asked_talk, ("work", 4096, None))
        get(srv, "/api/conversation?session=work&after=77&before=x&path=%2Fetc%2Fpasswd")
        self.assertEqual(mon.asked_talk, ("work", None, 77))            # anything that is not a plain number is ignored
        self.assertEqual(get(srv, "/api/conversation?session=gone")[0], 404)
        mon.asked_talk = None
        for bad in ("", "..%2F..%2Fetc", "a%3Bb", "x" * 200):
            self.assertEqual(get(srv, "/api/conversation?session=" + bad)[0], 404, bad)
        self.assertIsNone(mon.asked_talk)

    def test_the_view_only_page_follows_the_watch_switch(self):
        srv, _ = self.serve(terminal_view="local", terminal_type="off")          # looking allowed, typing not
        status, _, page = get(srv, "/watch")
        self.assertEqual(status, 200)
        self.assertIn(b"tp-viewnote", page)
        self.assertEqual(get(srv, "/terminal.js")[0], 200)                       # the page's script
        self.assertEqual(get(srv, "/terminal")[0], 404)
        srv, _ = self.serve(terminal_view="off", terminal_type="local")
        self.assertEqual(get(srv, "/watch")[0], 404)
        self.assertEqual(get(srv, "/api/conversation?session=work")[0], 404)
        self.assertEqual(get(srv, "/terminal")[0], 200)

    def test_switched_off(self):
        srv, _ = self.serve(terminal_view="off", terminal_type="off")
        self.assertEqual(get(srv, "/api/terminal?session=work")[0], 404)
        self.assertEqual(get(srv, "/api/conversation?session=work")[0], 404)
        self.assertEqual(get(srv, "/tty/")[0], 404)
        self.assertEqual(get(srv, "/terminal")[0], 404)
        self.assertEqual(get(srv, "/watch")[0], 404)
        self.assertEqual(get(srv, "/terminal.js")[0], 404)
        self.assertEqual(json.loads(get(srv, "/api/me")[2]), {"local": True, "watch": 404, "type": 404})

    def test_what_another_device_gets(self):
        # (setting, access code, has entered it) -> status for a device that is not this PC
        cases = (("local", "", False, 403), ("local", "c0de", True, 403), ("off", "", False, 404),
                 ("lan", "", False, 200),                 # the owner chose "no code": straight in
                 ("lan", "c0de", False, 401), ("lan", "c0de", True, 200))
        for mode, code, cookie, want in cases:
            srv, _ = self.serve(terminal_view=mode, terminal_type=mode, access_code=code)
            h, sent = self.other_device(srv, cookie)
            httpd.Handler._terminal(h, "session=work", {}, False)
            self.assertEqual(sent["status"], want, ("watch", mode, code, cookie))
            h, sent = self.other_device(srv, cookie)
            httpd.Handler._conversation(h, "session=work", {}, False, False)
            self.assertEqual(sent["status"], want, ("earlier conversation", mode, code, cookie))
            h, sent = self.other_device(srv, cookie)
            httpd.Handler._route(h, "/tty/", "arg=work", {}, False)
            self.assertEqual(sent["status"], want, ("type", mode, code, cookie))
            if want == 200:
                self.assertEqual(sent["proxied"], "/tty/?arg=work")

    def test_watch_and_type_are_separate_switches(self):
        srv, _ = self.serve(terminal_view="lan", terminal_type="local")
        h, sent = self.other_device(srv)
        httpd.Handler._terminal(h, "session=work", {}, False)
        self.assertEqual(sent["status"], 200)
        h, sent = self.other_device(srv)
        httpd.Handler._route(h, "/tty/", "", {}, False)
        self.assertEqual(sent["status"], 403)

    def test_code_does_not_lock_the_dashboard_unless_asked(self):
        for protect, want in (("terminals", 200), ("page", 401)):
            srv, _ = self.serve(access_code="c0de", protect=protect)
            h, sent = self.other_device(srv)
            httpd.Handler._route(h, "/api/snapshot", "", {}, False)
            self.assertEqual(sent["status"], want, protect)


class TerminalPassThrough(unittest.TestCase):
    """/tty/ is carried, unchanged, to the terminal service on its private socket."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ttyd.sock")
        self.upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.upstream.bind(self.path)
        self.upstream.listen(4)
        self.addCleanup(self.upstream.close)
        self.seen = []
        threading.Thread(target=self.serve_upstream, daemon=True).start()
        self.srv, _ = start({"terminal_type": "local", "terminal_socket": self.path})
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def serve_upstream(self):
        while True:
            try:
                conn, _ = self.upstream.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                head += chunk
            self.seen.append(head)
            if b"upgrade: websocket" in head.lower():
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
                while True:                                   # echo, as a stand-in for a terminal
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(b"echo:" + data)
            else:
                body = b"<html>terminal page</html>"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            conn.close()

    def test_page_request_is_passed_on_with_only_known_headers(self):
        status, _, body = get(self.srv, "/tty/?arg=work", ["Cookie: secret=1", "X-Evil: 1", "User-Agent: test"])
        self.assertEqual((status, body), (200, b"<html>terminal page</html>"))
        sent = self.seen[-1].lower()
        self.assertTrue(sent.startswith(b"get /tty/?arg=work http/1.1"))
        self.assertIn(b"user-agent: test", sent)
        self.assertNotIn(b"cookie", sent)
        self.assertNotIn(b"x-evil", sent)

    def test_typing_travels_both_ways(self):
        with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) as s:
            s.sendall(("GET /tty/ws?arg=work HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                       "Connection: Upgrade\r\nOrigin: http://127.0.0.1:%d\r\nSec-WebSocket-Key: abc\r\n\r\n"
                       % (self.srv.port, self.srv.port)).encode())
            got = b""
            while b"\r\n\r\n" not in got:
                got += s.recv(4096)
            self.assertTrue(got.startswith(b"HTTP/1.1 101"))
            s.sendall(b"ls\r")
            self.assertEqual(s.recv(4096), b"echo:ls\r")

    def test_another_website_cannot_use_the_terminal(self):
        port = self.srv.port
        self.assertEqual(get(self.srv, "/tty/ws", ["Upgrade: websocket", "Origin: http://evil.example"])[0], 403)
        self.assertEqual(get(self.srv, "/tty/", host="evil.example:%d" % port)[0], 403)          # DNS rebinding
        self.assertEqual(get(self.srv, "/tty/", ["Sec-Fetch-Site: cross-site"])[0], 403)

    def test_service_not_running(self):
        srv, _ = start({"terminal_type": "local", "terminal_socket": self.path + ".missing"})
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(get(srv, "/tty/")[0], 502)


class AccessCode(unittest.TestCase):
    def test_code_is_not_needed_on_this_pc(self):
        srv, _ = start({"access_code": "open-sesame"})
        try:
            self.assertEqual(get(srv, "/api/snapshot")[0], 200)
            status, head, _ = get(srv, "/login?code=open-sesame")
            self.assertEqual(status, 303)
            self.assertIn("HttpOnly; SameSite=Strict", head)
            self.assertNotIn("open-sesame", head)                 # the cookie is derived, not the code itself
        finally:
            srv.shutdown()
            srv.server_close()


class ReadOnly(unittest.TestCase):
    """PC-health must never stop, delete or change anything. These checks fail if code that could is added."""

    FORBIDDEN = (r"\bos\.kill\b", r"\bos\.killpg\b", r"\bsignal\.pthread_kill\b", r"\bos\.remove\b", r"\bos\.unlink\b",
                 r"\bos\.rmdir\b", r"\bshutil\b", r"\bos\.system\b", r"\bos\.popen\b", r"shell\s*=\s*True", r"\beval\(",
                 r"\bexec\(", r"\bos\.chmod\b", r"\bos\.rename\b", r"\bos\.truncate\b")

    def sources(self):
        for name in sorted(os.listdir(PKG)):
            if name.endswith(".py"):
                with open(os.path.join(PKG, name)) as f:
                    yield name, f.read()

    def test_no_destructive_calls(self):
        for name, text in self.sources():
            for pat in self.FORBIDDEN:
                self.assertIsNone(re.search(pat, text), "%s contains %s" % (name, pat))

    def test_only_known_programs_are_started(self):
        started = set()
        for name, text in self.sources():
            if "subprocess." in text:
                started.add(name)
        self.assertEqual(started, {"gpu.py", "klog.py", "folders.py", "tmux.py"})
        with open(os.path.join(PKG, "folders.py")) as f:
            self.assertEqual(f.read().count("subprocess.Popen("), 1)      # its own folder-sizing worker

    def test_tmux_is_only_ever_read(self):
        with open(os.path.join(PKG, "tmux.py")) as f:
            text = f.read()
        used = set(re.findall(r'"([a-z]+-[a-z]+)"', text))
        self.assertEqual(used, {"list-panes", "list-clients", "capture-pane", "display-message"})
        for verb in ("send-keys", "send-prefix", "kill-session", "kill-server", "kill-pane", "kill-window",
                     "new-session", "new-window", "respawn-pane", "run-shell", "pipe-pane", "attach-session", "attach",
                     "set-option", "resize-pane", "resize-window", "paste-buffer", "load-buffer", "source-file"):
            self.assertNotIn('"%s"' % verb, text)          # as a command word passed to tmux

    def test_the_terminal_service_does_only_these_things_to_tmux(self):
        # Starting, renaming and closing sessions is the terminal service's job, not PC Health's. Its script
        # is the only place those tmux commands may appear, and it may use no others.
        with open(os.path.join(ROOT, "terminal", "attach.sh")) as f:
            script = f.read()
        code = [line.strip() for line in script.splitlines() if not line.lstrip().startswith("#")]
        used = set(re.findall(r"\btmux ([a-z]+-[a-z]+)", script))
        self.assertEqual(used, {"has-session", "list-sessions", "list-clients", "new-session", "rename-session",
                                "kill-session", "attach-session", "send-keys"})
        # It types in one place only: when bringing saved terminals back, into a session it has just created,
        # and only this one line (letter by letter, never read as key names), then Enter.
        self.assertEqual([line for line in code if "send-keys" in line],
                         ['if ! tmux send-keys -t "=$name:" -l -- "$line" < /dev/null 2>/dev/null ||',
                          '! tmux send-keys -t "=$name:" Enter < /dev/null 2>/dev/null; then'])
        self.assertEqual([line for line in code if re.match(r"line\+?=", line) or " line+=" in line],
                         ['line="claude --resume $id"', '[[ "$model" != "-" ]] && line+=" --model \'$model\'"'])
        # The first tmux server is started by the login session, not inside this service: one line, and what it
        # starts is tmux with the same checked words the plain way uses.
        self.assertEqual([line for line in code if "systemd-run" in line and not line.startswith("if ")],
                         ['systemd-run --user --quiet --collect --expand-environment=no --service-type=forking --unit="pc-health-tmux-$$" "${same[@]}" '
                          '-- "$(command -v tmux)" new-session -d -E -s "$1" -c "$2" 2>/dev/null && return 0'])
        # Joining or making a session never changes what the session knows about the desktop (-E).
        self.assertEqual([line for line in code if "attach-session" in line], ['exec tmux attach-session -E -t "=$name"'])
        self.assertEqual([line for line in code if line.startswith("tmux new-session")],
                         ['tmux new-session -d -E -s "$1" -c "$2" 2>/dev/null      # also the way out if systemd would not do it'])
        # Two things it hands to PC Health's own code, which only reads and writes its own state file: saving, and
        # taking a terminal it has just closed off the saved list.
        self.assertEqual([line for line in code if "python" in line],
                         ["exec /usr/bin/python3 -E -s -m pchealth save-terminals",
                          '(cd "$HERE/.." 2>/dev/null && /usr/bin/python3 -E -s -m pchealth forget-terminal "$name") > /dev/null 2>&1 || true'])
        # The one file it ever removes or empties is its own marker for "bring the terminals back at login".
        self.assertEqual([line for line in code if re.search(r"\brm\b|: >", line)],
                         ['on) mkdir -p "${AT_LOGIN%/*}" 2>/dev/null && : > "$AT_LOGIN" || fail "The setting could not be written."; ok "on" ;;',
                          'off) rm -f -- "$AT_LOGIN" 2>/dev/null || fail "The setting could not be written."; ok "off" ;;'])
        self.assertIn('AT_LOGIN="$HOME/.local/state/pc-health/restore-at-login"', script)
        for word in ("kill-server", "run-shell", "eval ", "bash -c", "sh -c", "$(cat", "source "):
            self.assertNotIn(word, script)
        for name, text in self.sources():
            for verb in ("new-session", "rename-session", "kill-session", "send-keys"):
                self.assertNotIn(verb, text, name)                     # never from the monitor or its web server

    def test_at_login_only_the_restore_is_run_and_only_when_switched_on(self):
        # The unit that acts at login runs the terminal service's script with --restore and nothing else, and
        # only while the marker exists. The marker's name is the same in the unit, the script and the monitor.
        from pchealth import config
        with open(os.path.join(ROOT, "pc-health-restore.service")) as f:
            unit = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        self.assertEqual([line for line in unit if line.startswith("Exec")], ["ExecStart=-@DIR@/terminal/attach.sh --restore"])
        self.assertIn("ConditionPathExists=%h/.local/state/pc-health/restore-at-login", unit)
        self.assertIn("Type=oneshot", unit)
        self.assertTrue(config.AT_LOGIN_FLAG.endswith("/.local/state/pc-health/restore-at-login"))
        self.assertEqual(os.path.basename(config.AT_LOGIN_UNIT), "pc-health-restore.service")

    def test_files_are_written_only_through_the_state_helper(self):
        for name, text in self.sources():
            opens = re.findall(r"os\.open\([^)]*O_WRONLY", text) + re.findall(r"open\([^)]*['\"][wa]b?['\"]", text)
            if name != "history.py":
                self.assertEqual(opens, [], name)

    def test_page_inserts_text_only_and_only_reads(self):
        for name in ("app.js", "terminal.js", "look.js", "nav.js"):
            with open(os.path.join(PKG, "static", name)) as f:
                js = f.read()
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(bad, js, name)
            self.assertEqual(re.findall(r'method:\s*"(\w+)"', js), [], name)       # every fetch is a plain GET

    def test_pages_keep_to_the_content_security_policy(self):
        # default-src 'self' silently drops inline styles and scripts, so none may be written.
        for name in ("index.html", "terminal.html"):
            with open(os.path.join(PKG, "static", name)) as f:
                page = f.read()
            self.assertNotIn("<style", page, name)
            self.assertIsNone(re.search(r"\sstyle\s*=", page), name)
            self.assertIsNone(re.search(r"\son[a-z]+\s*=", page), name)          # no inline event handlers
            self.assertEqual(re.findall(r"<script(?![^>]*\ssrc=)", page), [], name)
            self.assertNotIn("data:", page, name)
        with open(os.path.join(PKG, "static", "app.css")) as f:
            css = f.read()
        self.assertNotIn("data:", css)
        self.assertEqual(re.findall(r"@import|url\(\s*['\"]?(?:https?:)?//", css), [])     # nothing fetched from elsewhere

    def test_nothing_on_the_page_animates_forever(self):
        # The page stays open all day on the PC it watches: motion only ever answers a change or a touch.
        with open(os.path.join(PKG, "static", "app.css")) as f:
            self.assertNotIn("infinite", f.read())


@unittest.skipUnless(os.access("/usr/bin/tmux", os.X_OK), "tmux is not installed")
class TerminalService(unittest.TestCase):
    """terminal/attach.sh starts, renames and closes tmux sessions. Tried here against a tmux server of its own
    and with a home folder of its own, in a temporary folder, so neither the sessions on this PC nor its saved
    terminals are ever touched."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = os.path.realpath(self.dir.name)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(HOME=self.home, TMUX_TMPDIR=self.home)
        self.state = self.home + "/.local/state/pc-health"
        self.addCleanup(lambda: subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True))

    def saved_names(self):
        try:
            with open(self.state + "/saved-terminals.tsv") as f:
                return [line.split("\t")[0] for line in f.read().splitlines()[1:]]
        except OSError:
            return None

    def ask(self, *args):
        r = subprocess.run(["bash", os.path.join(ROOT, "terminal", "attach.sh")] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout.strip()

    def sessions(self):
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"], env=self.env,
                           capture_output=True, text=True)
        return dict(line.split("\t") for line in r.stdout.splitlines())

    def test_new_needs_a_usable_name_and_starts_at_home(self):
        for bad in ("", "two words", "-dash", "a;b", "$(id)", "x" * 65, "../up", "a.b", "v1.2"):
            code, said = self.ask("--new", bad)
            self.assertEqual((code, said[:9]), (1, "PCH-ERROR"), bad)
        self.assertEqual(self.sessions(), {})
        self.assertEqual(self.ask("--new", "work"), (0, "PCH-OK work"))
        self.assertEqual(self.sessions(), {"work": self.home})
        code, said = self.ask("--new", "work")                              # the name is taken
        self.assertEqual((code, said[:9]), (1, "PCH-ERROR"))
        self.assertEqual(list(self.sessions()), ["work"])

    def test_rename(self):
        self.ask("--new", "work")
        self.ask("--new", "other")
        self.assertEqual(self.ask("--rename", "work", "notes"), (0, "PCH-OK notes"))
        self.assertEqual(sorted(self.sessions()), ["notes", "other"])
        for old, new in (("notes", "other"), ("notes", "two words"), ("gone", "x"), ("notes", ""), ("notes", "-x"), ("notes", "no.tes")):
            code, said = self.ask("--rename", old, new)
            self.assertEqual((code, said[:9]), (1, "PCH-ERROR"), (old, new))
        self.assertEqual(sorted(self.sessions()), ["notes", "other"])

    def test_emdash_terminals_are_left_alone(self):
        self.ask("--new", "web-store-3f9c2e7a1b")                           # the kind of name Emdash gives
        self.ask("--new", "mine")
        for request in (("--rename", "web-store-3f9c2e7a1b", "other"), ("--close", "web-store-3f9c2e7a1b")):
            code, said = self.ask(*request)
            self.assertEqual(code, 1, request)
            self.assertIn("Emdash", said)
        self.assertEqual(sorted(self.sessions()), ["mine", "web-store-3f9c2e7a1b"])
        self.assertEqual(self.ask("--close", "mine")[0], 0)                 # a terminal of your own still closes
        self.assertEqual(list(self.sessions()), ["web-store-3f9c2e7a1b"])

    def test_close_ends_only_the_one_named(self):
        self.ask("--new", "work")
        self.ask("--new", "keep")
        self.assertEqual(self.ask("--close", "work"), (0, "PCH-OK work"))
        self.assertEqual(list(self.sessions()), ["keep"])
        for bad in ("work", "", "ke", "keep;x", "*"):
            self.assertEqual(self.ask("--close", bad)[0], 1, bad)
        self.assertEqual(list(self.sessions()), ["keep"])
        self.assertEqual(self.ask("--anything-else", "keep")[0], 1)
        self.assertEqual(list(self.sessions()), ["keep"])

    def write_saved(self, *names):
        os.makedirs(self.state, exist_ok=True)
        with open(self.state + "/saved-terminals.tsv", "w") as f:
            f.write("pch-saved-terminals\t1\t1791330000\n" + "".join("%s\t-\t-\t%s\n" % (n, self.home) for n in names))

    def test_a_terminal_closed_from_the_page_does_not_come_back(self):
        for name in ("work", "keep", "third"):
            self.ask("--new", name)
        self.write_saved("keep", "third", "work")
        self.assertEqual(self.ask("--close", "work"), (0, "PCH-OK work"))
        self.assertEqual(self.saved_names(), ["keep", "third"])               # off the saved list: closed for good
        self.assertEqual(self.ask("--close", "gone")[0], 1)                   # nothing closed: nothing forgotten
        self.assertEqual(self.saved_names(), ["keep", "third"])
        # Where the list is only saved by hand (terminal_autosave = false), closing leaves it as it was saved.
        os.makedirs(self.home + "/.config/pc-health")
        with open(self.home + "/.config/pc-health/config.toml", "w") as f:
            f.write("terminal_autosave = false\n")
        self.assertEqual(self.ask("--close", "third"), (0, "PCH-OK third"))
        self.assertEqual(self.saved_names(), ["keep", "third"])
        self.assertEqual(list(self.sessions()), ["keep"])

    def test_the_at_login_switch_makes_and_removes_one_marker(self):
        marker = self.state + "/restore-at-login"
        self.ask("--new", "work")
        self.write_saved("work", "other")
        for bad in ((), ("maybe",), ("on; touch x",), ("../on",)):
            self.assertEqual(self.ask("--at-login", *bad)[0], 1, bad)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.ask("--at-login", "on"), (0, "PCH-OK on"))
        self.assertEqual(os.path.getsize(marker), 0)
        self.assertEqual(self.ask("--at-login", "on"), (0, "PCH-OK on"))        # twice is the same as once
        self.assertEqual(self.ask("--at-login", "off"), (0, "PCH-OK off"))
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.ask("--at-login", "off"), (0, "PCH-OK off"))
        # Nothing else was started, closed or changed by any of it.
        self.assertEqual(list(self.sessions()), ["work"])
        self.assertEqual(self.saved_names(), ["work", "other"])
        self.assertEqual(sorted(os.listdir(self.state)), ["saved-terminals.tsv"])


@unittest.skipUnless(os.access("/usr/bin/tmux", os.X_OK), "tmux is not installed")
class SavedTerminals(unittest.TestCase):
    """Saving the terminals and bringing them back (attach.sh --save / --restore). Tried against a tmux server
    of its own, with a home folder of its own and a stand-in for the `claude` program, all in a temporary
    folder: none of your terminals, conversations or saved files is read, started or typed into."""

    A = "0a1b2c3d-1111-4222-8333-444455556666"
    B = "9f8e7d6c-aaaa-4bbb-8ccc-ddddeeeeffff"
    # A script named "claude", so that its process is called claude. It notes how it was started, writes the
    # small file Claude Code keeps for each running conversation, and waits.
    STUB = '''#!/usr/bin/python3
import json, os, sys, time
home = os.environ["HOME"]
args = sys.argv[1:]
sid = args[args.index("--resume") + 1]
start = open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19]
os.makedirs(home + "/.claude/sessions", exist_ok=True)
with open("%s/.claude/sessions/%d.json" % (home, os.getpid()), "w") as f:
    json.dump({"pid": os.getpid(), "sessionId": sid, "cwd": os.getcwd(), "procStart": start}, f)
with open(home + "/started.log", "a") as f:
    f.write(json.dumps([os.getcwd(), args]) + "\\n")
time.sleep(120)
'''

    def setUp(self):
        import tempfile
        # What ran in the tmux server (the stand-in claude, or a shell saving its history) can still be writing its
        # files in the moment after that server is ended (seen on a busy PC), so a folder that will not empty at
        # once is left to the system rather than failing the test.
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.dir.cleanup)
        self.home = os.path.realpath(self.dir.name)
        os.makedirs(self.home + "/bin")
        os.makedirs(self.home + "/proj")
        with open(self.home + "/bin/claude", "w") as f:
            f.write(self.STUB)
        os.chmod(self.home + "/bin/claude", 0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(HOME=self.home, TMUX_TMPDIR=self.home, SHELL="/bin/bash",
                        PATH=self.home + "/bin:" + os.environ.get("PATH", "/usr/bin:/bin"))
        self.file = self.home + "/.local/state/pc-health/saved-terminals.tsv"
        self.addCleanup(lambda: subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True))

    def ask(self, *args):
        r = subprocess.run(["bash", os.path.join(ROOT, "terminal", "attach.sh")] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip()

    def tmux(self, *args):
        return subprocess.run(["tmux"] + list(args), env=self.env, capture_output=True, text=True).stdout

    def sessions(self):
        return dict(line.split("\t") for line in self.tmux("list-sessions", "-F", "#{session_name}\t#{session_path}").splitlines())

    def started(self, n):
        """Wait until the stand-in claude has been started n times. -> [[folder, arguments], ...]"""
        got = []
        for _ in range(100):
            try:
                with open(self.home + "/started.log") as f:
                    got = [json.loads(line) for line in f]
            except OSError:
                got = []
            if len(got) >= n:
                break
            time.sleep(0.1)
        return got

    def write(self, *rows):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "w") as f:
            f.write("pch-saved-terminals\t1\t1791330000\n" + "".join("\t".join(r) + "\n" for r in rows))

    def test_save_then_bring_back_after_everything_is_gone(self):
        self.tmux("new-session", "-d", "-s", "work", "-c", self.home + "/proj", "claude --resume " + self.A)
        self.tmux("new-session", "-d", "-s", "plain", "-c", self.home)
        self.assertEqual(self.started(1), [[self.home + "/proj", ["--resume", self.A]]])
        self.assertEqual(self.ask("--save"), (0, "PCH-OK 2 terminals saved, 1 with a conversation"))
        with open(self.file) as f:
            lines = f.read().splitlines()
        self.assertRegex(lines[0], r"^pch-saved-terminals\t1\t[0-9]+$")
        self.assertEqual(lines[1:], ["plain\t-\t-\t" + self.home, "work\t%s\t-\t%s/proj" % (self.A, self.home)])
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 0 started, 2 already open."))      # all still running
        self.tmux("kill-server")                                                              # "the reboot"
        self.assertEqual(self.sessions(), {})
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 2 started."))
        self.assertEqual(self.sessions(), {"plain": self.home, "work": self.home + "/proj"})
        # The conversation was resumed from its own folder, by exactly the one line.
        self.assertEqual(self.started(2)[1], [self.home + "/proj", ["--resume", self.A]])
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 0 started, 2 already open."))      # a second press does nothing
        self.assertEqual(len(self.started(2)), 2)
        self.assertEqual(sorted(self.sessions()), ["plain", "work"])

    def test_a_model_is_passed_on_as_one_quoted_word(self):
        self.write(["research", self.A, "opus[1m]", self.home + "/proj"])
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 1 started."))
        self.assertEqual(self.started(1), [[self.home + "/proj", ["--resume", self.A, "--model", "opus[1m]"]]])

    def test_nothing_in_the_file_can_run_a_command(self):
        mark = self.home + "/MARK"
        hit = "touch " + mark
        self.write(["$(%s)" % hit, "-", "-", self.home], ["a;" + hit, "-", "-", self.home], ["-n", "-", "-", self.home],
                   ["two words", "-", "-", self.home], ["ok1", "'; %s; '" % hit, "-", self.home],
                   ["ok2", self.A + ";" + hit, "-", self.home], ["ok3", self.A, "m'; %s; '" % hit, self.home],
                   ["ok4", self.A, "$(%s)" % hit, self.home], ["ok5", "-", "-", "proj"],
                   ["ok6", "-", "-", self.home + "/#(%s)" % hit], ["ok7", "-", "-", self.home, "extra"],
                   ["emdash-3f9c2e7a1b", "-", "-", self.home])
        code, said = self.ask("--restore")
        self.assertEqual((code, said), (1, "PCH-ERROR Nothing was started. Damaged entries ignored: 12."))
        self.assertEqual(self.sessions(), {})
        time.sleep(0.3)
        self.assertFalse(os.path.exists(mark))
        self.assertEqual(self.started(0), [])

    def test_a_running_terminal_is_never_typed_into(self):
        self.tmux("new-session", "-d", "-s", "work", "-c", self.home, "cat")          # shows anything typed into it
        self.write(["work", self.A, "-", self.home + "/proj"], ["gone", "-", "-", self.home + "/nowhere"])
        code, said = self.ask("--restore")
        self.assertEqual((code, said), (1, "PCH-ERROR Nothing was started. Not started: work (the name is in use), "
                                           "gone (its folder is gone)."))
        time.sleep(0.3)
        self.assertEqual(self.tmux("capture-pane", "-p", "-t", "=work:").strip(), "")
        self.assertEqual(list(self.sessions()), ["work"])

    def test_a_conversation_that_is_open_elsewhere_is_not_opened_twice(self):
        self.tmux("new-session", "-d", "-s", "renamed", "-c", self.home + "/proj", "claude --resume " + self.A)
        self.started(1)
        self.write(["work", self.A, "-", self.home + "/proj"], ["two", self.B, "-", self.home + "/proj"])
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 1 started, 1 already open."))
        self.assertEqual(sorted(self.sessions()), ["renamed", "two"])
        self.assertEqual(self.started(2)[1], [self.home + "/proj", ["--resume", self.B]])

    def test_a_file_edited_by_hand_is_read_the_same_way_as_the_page_reads_it(self):
        from pchealth import saved
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        text = ("pch-saved-terminals\t1\t1791330000\n"
                "broken\t\t\t%s\n"                       # tabs next to each other count as one: two fields, not a terminal
                "\tspaced\t\t-\t-\t%s\t\n"               # ...and tabs at the ends do not count: a terminal
                "last\t-\t-\t%s" % (self.home, self.home, self.home))      # no line break after the last line: a terminal
        with open(self.file, "w") as f:
            f.write(text)
        self.assertEqual([r["name"] for r in saved.parse(text.encode())["rows"]], ["spaced", "last"])
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 2 started. Damaged entries ignored: 1."))
        self.assertEqual(sorted(self.sessions()), ["last", "spaced"])

    def test_one_conversation_listed_twice_is_opened_once(self):
        self.write(["one", self.A, "-", self.home + "/proj"], ["two", self.A, "-", self.home + "/proj"])
        self.assertEqual(self.ask("--restore"), (0, "PCH-OK 1 started, 1 already open."))
        self.assertEqual(list(self.sessions()), ["one"])

    def test_saving_with_nothing_running_keeps_the_earlier_save(self):
        self.write(["work", self.A, "-", self.home + "/proj"])
        with open(self.file) as f:
            before = f.read()
        code, said = self.ask("--save")
        self.assertEqual((code, said[:9]), (1, "PCH-ERROR"))
        with open(self.file) as f:
            self.assertEqual(f.read(), before)

    def test_only_its_own_plain_file_is_trusted(self):
        self.assertEqual(self.ask("--restore")[0], 1)                       # nothing saved yet
        os.makedirs(os.path.dirname(self.file))
        with open(self.home + "/elsewhere", "w") as f:
            f.write("pch-saved-terminals\t1\t1\nwork\t-\t-\t%s\n" % self.home)
        os.symlink(self.home + "/elsewhere", self.file)                     # a link to some other file: not read
        self.assertEqual(self.ask("--restore")[0], 1)
        os.unlink(self.file)
        with open(self.file, "w") as f:
            f.write("something else\nwork\t-\t-\t%s\n" % self.home)      # not a saved-terminals file
        self.assertEqual(self.ask("--restore"), (1, "PCH-ERROR The saved file is damaged."))
        self.assertEqual(self.sessions(), {})


@unittest.skipUnless(os.access("/usr/bin/tmux", os.X_OK), "tmux is not installed")
class FirstTmuxServer(unittest.TestCase):
    """When no tmux server is running, the terminal service does not start one inside itself (terminals would
    inherit its limits: no sudo, no snap programs; and they would end with the service). It asks the login
    session to start it. Tried with a stand-in for systemd-run, a tmux server of its own and a home of its own."""

    # Notes what it was asked, one word per line, then does what systemd would: runs the command (or refuses).
    STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$@" "" >> "$PCH_TEST_LOG"
[[ -n "$PCH_TEST_REFUSE" ]] && exit 1
while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
shift
exec "$@"
'''

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = os.path.realpath(self.dir.name)
        os.makedirs(self.home + "/bin")
        with open(self.home + "/bin/systemd-run", "w") as f:
            f.write(self.STUB)
        os.chmod(self.home + "/bin/systemd-run", 0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "PCH_AS_SERVICE", "PCH_TEST_REFUSE")}
        self.env.update(HOME=self.home, TMUX_TMPDIR=self.home, SHELL="/bin/bash", PCH_TEST_LOG=self.home + "/asked.log",
                        PATH=self.home + "/bin:" + os.environ.get("PATH", "/usr/bin:/bin"))
        self.addCleanup(lambda: subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True))

    def ask(self, *args, **more):
        r = subprocess.run(["bash", os.path.join(ROOT, "terminal", "attach.sh")] + list(args), env=dict(self.env, **more),
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip()

    def asked(self):
        try:
            with open(self.home + "/asked.log") as f:
                return [chunk.split("\n") for chunk in f.read().split("\n\n") if chunk]
        except OSError:
            return []

    def sessions(self):
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], env=self.env, capture_output=True, text=True)
        return r.stdout.split()

    def test_the_first_terminal_is_started_by_the_login_session(self):
        import shutil
        self.assertEqual(self.ask("--new", "work", PCH_AS_SERVICE="1"), (0, "PCH-OK work"))
        self.assertEqual(self.sessions(), ["work"])
        (words,) = self.asked()
        self.assertRegex(words[5], r"^--unit=pc-health-tmux-[0-9]+$")
        self.assertEqual(words[:5] + words[6:],
                         ["--user", "--quiet", "--collect", "--expand-environment=no", "--service-type=forking",
                          "--setenv=TMUX_TMPDIR=" + self.home, "--",
                          shutil.which("tmux", path=self.env["PATH"]), "new-session", "-d", "-E", "-s", "work", "-c", self.home])
        # A server is running now: the next terminal is just one more session in it.
        self.assertEqual(self.ask("--new", "second", PCH_AS_SERVICE="1"), (0, "PCH-OK second"))
        self.assertEqual(sorted(self.sessions()), ["second", "work"])
        self.assertEqual(len(self.asked()), 1)

    def test_bringing_terminals_back_starts_the_server_the_same_way(self):
        os.makedirs(self.home + "/.local/state/pc-health")
        with open(self.home + "/.local/state/pc-health/saved-terminals.tsv", "w") as f:
            f.write("pch-saved-terminals\t1\t1791330000\none\t-\t-\t%s\ntwo\t-\t-\t%s\n" % (self.home, self.home))
        self.assertEqual(self.ask("--restore", PCH_AS_SERVICE="1"), (0, "PCH-OK 2 started."))
        self.assertEqual(sorted(self.sessions()), ["one", "two"])
        self.assertEqual([words[-5:] for words in self.asked()], [["-E", "-s", "one", "-c", self.home]])

    def test_a_terminal_made_from_the_page_can_still_open_a_window(self):
        # The tmux server was started with the desktop's DISPLAY. The page's service has none: a terminal it makes
        # must not lose the server's.
        bare = {k: v for k, v in self.env.items() if k not in ("DISPLAY", "XAUTHORITY")}
        r = subprocess.run(["bash", os.path.join(ROOT, "terminal", "attach.sh"), "--new", "first"],
                           env=dict(bare, DISPLAY=":7", XAUTHORITY="/run/x"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "PCH-OK first")
        r = subprocess.run(["bash", os.path.join(ROOT, "terminal", "attach.sh"), "--new", "second"],
                           env=bare, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "PCH-OK second")
        tmux = lambda *a: subprocess.run(["tmux"] + list(a), env=bare, capture_output=True, text=True).stdout.split()
        self.assertIn("DISPLAY=:7", tmux("show-environment", "-g"))
        for name in ("first", "second"):
            marks = tmux("show-environment", "-t", "=" + name)
            self.assertNotIn("-DISPLAY", marks, name)             # "-DISPLAY" is tmux's note "take it away in here"
            self.assertNotIn("-XAUTHORITY", marks, name)

    def test_if_the_login_session_refuses_the_terminal_still_starts(self):
        self.assertEqual(self.ask("--new", "work", PCH_AS_SERVICE="1", PCH_TEST_REFUSE="1"), (0, "PCH-OK work"))
        self.assertEqual(self.sessions(), ["work"])
        self.assertEqual(len(self.asked()), 1)

    def test_a_bad_name_never_reaches_it(self):
        for bad in ("", "two words", "-dash", "a;b", "$(id)", "--setenv=X=1"):
            self.assertEqual(self.ask("--new", bad, PCH_AS_SERVICE="1")[0], 1, bad)
        self.assertEqual(self.asked(), [])
        self.assertEqual(self.sessions(), [])

    @unittest.skipIf(open("/proc/self/cgroup").read().strip().endswith(".service"), "these tests are themselves running inside a service")
    def test_outside_a_service_tmux_is_started_directly(self):
        self.assertEqual(self.ask("--new", "work"), (0, "PCH-OK work"))
        self.assertEqual(self.sessions(), ["work"])
        self.assertEqual(self.asked(), [])


class Footprint(unittest.TestCase):
    def test_import_budget(self):
        code = ("import sys, pchealth.sampler, pchealth.httpd, pchealth.__main__;"
                "print(' '.join(sorted(m for m in sys.modules)))")
        out = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        loaded = set(out.stdout.split())
        for heavy in ("logging", "argparse", "asyncio", "sqlite3", "http.server", "http.client", "email", "xml",
                      "urllib.request", "ssl", "tomllib", "hashlib", "ipaddress", "shutil", "tempfile"):
            self.assertNotIn(heavy, loaded)


class Secrets(unittest.TestCase):
    def test_nothing_secret_reaches_the_snapshot(self):
        canary_env = "CANARYENV7f3a91"
        canary_arg = "CANARYARG5d2c44"
        env = dict(os.environ, EMDASH_HOOK_TOKEN=canary_env, CLAUDE_CODE_MESSAGING_TOKEN=canary_env,
                   MY_API_KEY=canary_env)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--token=" + canary_arg], env=env)
        try:
            from pchealth import procfs
            from pchealth.__main__ import _warm
            for _ in range(100):
                if b"time.sleep" in (procfs.read1("/proc/%d/cmdline" % child.pid) or b""):
                    break
                time.sleep(0.02)
            mon = Monitor(config.load("/nonexistent"), persist=False)
            mon.tick()
            ident = (child.pid, procfs.pid_stat(child.pid)[6])
            # Force the paths that read a process's environment and command line onto the canary.
            mon.attr.pub["env_wanted"].append(ident)
            mon.attr.pub["groups"][ident] = [child.pid]
            mon._job_env(time.time())
            mon._job_groups(time.time())
            self.assertEqual(mon.enrich.env[ident].get("CLAUDECODE", ""), os.environ.get("CLAUDECODE", ""))
            self.assertNotIn(canary_env, json.dumps(mon.enrich.env[ident]))
            self.assertNotIn(canary_arg, json.dumps(mon.enrich.group[ident]))
            _warm(mon, 0.3)
            blob = mon.snapshot_bytes + mon.history_bytes("1h")
        finally:
            child.terminate()
            child.wait()
        self.assertGreater(len(blob), 500)
        self.assertNotIn(canary_env.encode(), blob)
        self.assertNotIn(canary_arg.encode(), blob)
        for word in (b"TOKEN", b"NONCE", b"messagingSocketPath", b"cc-socks"):
            self.assertNotIn(word, blob)
        snap = json.loads(mon.snapshot_bytes)
        self.assertEqual(snap["v"], 1)
        self.assertIn(snap["health"]["level"], ("ok", "warn", "crit"))

    def test_a_saved_conversation_is_never_named_to_the_page(self):
        # Which terminals are saved is shown on the page; the id that would resume a conversation is not.
        talk = "0a1b2c3d-1111-4222-8333-444455556666"
        mon = Monitor(config.load("/nonexistent"), persist=False)
        mon.enrich.saved = {"ts": 1791330000, "rows": [{"name": "work", "sid": talk, "model": "", "folder": "/secret/place"}]}
        mon.enrich.sessions = {"work": {"attached": 0, "windows": 1, "w": 80, "h": 24, "cmd": "bash", "socket": "", "pane": "%1"}}
        mon.tick()
        snap = json.loads(mon.snapshot_bytes)
        self.assertEqual(snap["terminals"]["saved"], {"ts": 1791330000, "names": ["work"], "gone": []})
        self.assertEqual([(x["name"], x["saved"]) for x in snap["terminals"]["sessions"]], [("work", "stopped")])
        self.assertNotIn(talk.encode(), mon.snapshot_bytes)
        self.assertNotIn(b"/secret/place", mon.snapshot_bytes)


if __name__ == "__main__":
    unittest.main()
