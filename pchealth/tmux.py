"""Read-only access to tmux: which pane an agent lives in, and what that pane shows.

Only four tmux commands are ever run: list-panes, list-clients, capture-pane and display-message.
None of them changes anything or sends input, so the page can show a terminal but never type into it.
(Starting, renaming and closing sessions is done by the terminal service, terminal/attach.sh, not here.)
"""

import os
import re
import stat
import subprocess

TMUX = "/usr/bin/tmux"
_PANE = re.compile(r"^%[0-9]{1,6}$")
# Session names that can be opened from the page: plain enough to travel in an address unchanged.
SESSION_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# The last three say which session and which tmux server this is, whatever the session is called at the moment:
# tmux's own id for the session (it survives a rename), and the server's process id and start time.
_LIST_FIELDS = ("pane_id", "pane_tty", "session_name", "window_index", "pane_index", "pane_width", "pane_height",
                "session_attached", "session_windows", "window_active", "pane_active", "pane_current_command",
                "session_id", "pid", "start_time")
_LIST_FORMAT = "\t".join("#{%s}" % f for f in _LIST_FIELDS)
# A record separator marks the info line, so it cannot be confused with screen content.
_INFO_FORMAT = "\x1e" + "\t".join("#{%s}" % f for f in ("pane_width", "pane_height", "cursor_x", "cursor_y",
                                                        "cursor_flag"))


# The Emdash app names the tmux sessions it starts "<project>-<ten hex digits>" and finds them again by that
# name. The same test is in terminal/attach.sh, which is what refuses to rename them.
EMDASH_NAME = re.compile(r"-[0-9a-f]{10}$")


def available():
    return os.access(TMUX, os.X_OK)


def socket_from_env(value, uid):
    """$TMUX is 'socket-path,server-pid,session-index'. -> the path, if it is this user's own tmux socket."""
    path = (value or "").split(",", 1)[0]
    if not path or "\0" in path:
        return None
    path = os.path.normpath(path)
    if not path.startswith(("/tmp/tmux-%d/" % uid, "/run/user/%d/" % uid)):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return path if stat.S_ISSOCK(st.st_mode) and st.st_uid == uid else None


def _run(socket, args, timeout=2.0):
    try:
        r = subprocess.run([TMUX, "-S", socket] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def parse_panes(raw):
    """list-panes output -> {pane id: {"tty", "session", "window", "index", "w", "h", "attached", "windows",
    "active", "cmd", "id", "server"}}. "active" marks the pane a person attaching to the session would land on."""
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        f = line.split("\t")
        if len(f) != len(_LIST_FIELDS) or not _PANE.match(f[0]):
            continue
        try:
            out[f[0]] = {"tty": f[1], "session": f[2], "window": int(f[3]), "index": int(f[4]),
                         "w": int(f[5]), "h": int(f[6]), "attached": int(f[7]), "windows": int(f[8]),
                         "active": f[9] == "1" and f[10] == "1", "cmd": f[11][:32],
                         "id": f[12][:16], "server": "%d:%d" % (int(f[13]), int(f[14]))}
        except ValueError:
            continue
    return out


def sessions_from(panes):
    """{session name: its active pane's info plus "pane" (the pane id)}."""
    out = {}
    for pane_id, p in panes.items():
        cur = out.get(p["session"])
        if cur is None or (p["active"] and not cur["active"]):
            out[p["session"]] = dict(p, pane=pane_id)
    return out


def list_panes(socket):
    raw = _run(socket, ["list-panes", "-a", "-F", _LIST_FORMAT])
    return parse_panes(raw) if raw is not None else None


def parse_clients(raw):
    """list-clients output -> {session name: [pid of each attached client]}."""
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        pid, sep, session = line.partition("\t")
        if sep and pid.isdigit():
            out.setdefault(session, []).append(int(pid))
    return out


def list_clients(socket):
    raw = _run(socket, ["list-clients", "-F", "#{client_pid}\t#{session_name}"])
    return parse_clients(raw) if raw is not None else {}


def same_tty(pane_tty, proc_tty_nr):
    """Is the terminal of a pane the controlling terminal of a process (tty_nr from /proc/<pid>/stat)?"""
    if not proc_tty_nr or not pane_tty.startswith("/dev/pts/"):
        return False
    major = (proc_tty_nr >> 8) & 0xFFF
    minor = (proc_tty_nr & 0xFF) | ((proc_tty_nr >> 12) & 0xFFF00)
    try:
        return os.stat(pane_tty).st_rdev == os.makedev(major, minor)
    except OSError:
        return False


def parse_capture(raw):
    """capture-pane followed by the info line -> {"text", "w", "h", "cx", "cy", "cursor"} or None."""
    text = raw.decode("utf-8", "replace")
    cut = text.rfind("\x1e")
    if cut < 0:
        return None
    info = text[cut + 1:].strip().split("\t")
    try:
        w, h, cx, cy, flag = (int(x) for x in info[:5])
    except ValueError:
        return None
    screen = text[:cut]
    if screen.endswith("\n"):
        screen = screen[:-1]
    return {"text": screen[:400_000], "w": w, "h": h, "cx": cx, "cy": cy, "cursor": bool(flag)}


def capture(socket, pane):
    """What a pane shows right now, colours included. Reads only; the pane is not attached or resized."""
    if not _PANE.match(pane or ""):
        return None
    raw = _run(socket, ["capture-pane", "-p", "-e", "-t", pane, ";",
                        "display-message", "-p", "-t", pane, _INFO_FORMAT])
    return parse_capture(raw) if raw else None
