"""Leftovers: helper processes an agent started that outlived or detached from it.

Nothing is reported without evidence. Evidence is either the marker Claude Code puts in the
environment of everything it starts, or (weaker, "possibly left over") a headless-browser or
dev-server shape on a process that was handed to systemd inside a group that hosted an agent.
"""

import os

from . import procfs

GB = 1 << 30
GRACE_S = 60                 # an agent's children may linger briefly after it exits
IDLE_FLAG_S = 2 * 3600       # helpers of a live agent are flagged once it has idled this long
SUSPECT_AGE_S = 30 * 60
SUSPECT_COMMS = frozenset((
    "chrome", "chromium", "chromium-browse", "headless_shell", "chrome-headless-",
    "python", "python3", "node", "bun", "deno", "xtask", "vite", "http-server", "serve", "php", "ruby",
))
_BROWSERS = ("chrome", "chromium", "headless_shell", "msedge", "brave")


def what(comm, argv):
    """A short, safe description derived from the command line. The command line itself is never exported."""
    args = argv[1:40]
    base = os.path.basename(argv[0]) if argv else comm
    if any(b in base or b in comm for b in _BROWSERS):
        if any(a == "--headless" or a.startswith("--headless=") for a in args):
            return "Headless browser"
        return "Browser"
    for i, a in enumerate(args):
        if a == "-m" and i + 1 < len(args) and args[i + 1] == "http.server":
            return "Python web server"
    if "serve" in args and ("xtask" in base or "xtask" in comm):
        return "Dev web server (xtask)"
    if base in ("vite", "http-server", "serve") or any(
            os.path.basename(a) in ("vite", "vite.js", "http-server", "serve") for a in args[:3]):
        return "Dev web server"
    if comm in ("UnrealEditor", "UnrealEditor-Cm"):
        return "Unreal Editor"
    if comm.startswith("blender"):
        return "Blender"
    return comm


def is_suspect_shape(label):
    return label in ("Headless browser", "Python web server", "Dev web server (xtask)", "Dev web server")


def ports_of(pids, listening=None, cap=24):
    """Listening TCP ports held by these processes."""
    listening = procfs.listen_ports() if listening is None else listening
    if not listening:
        return []
    found = set()
    for pid in pids[:cap]:
        for ino in procfs.pid_socket_inodes(pid):
            port = listening.get(ino)
            if port:
                found.add(port)
    return sorted(found)[:6]


def kill_command(root_pids):
    """The command the page offers to copy. Digits only; PC-health itself never runs it."""
    return "kill " + " ".join(str(int(p)) for p in root_pids)


def level(kind, age_s, cores_avg, mem_b, vram_b, thresholds):
    """-> 'info' | 'warn' | 'crit' for one leftover group."""
    if kind == "suspect":
        return "info"
    t = thresholds
    if (cores_avg >= t["leftover_cores"][1] or mem_b >= t["leftover_mem_gb"][1] * GB
            or vram_b >= t["leftover_vram_gb"][1] * GB):
        return "crit" if age_s >= 600 else "warn"
    return "warn" if age_s >= 600 else "info"
