"""OpenAI Codex CLI sessions: find the rollout log for a running `codex` and read its state.

Built from the file shapes found on this machine; the process side could not be checked
against a live Codex session, so every step has a fallback and "unknown" is a valid answer.
"""

import json
import os
import re
import time

from . import procfs
from .agents_claude import clean

HOME = os.path.expanduser("~")
CODEX_HOME = HOME + "/.codex"
SESSIONS = CODEX_HOME + "/sessions"
_UUID = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})\.jsonl$")
_CWD = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.){1,400})"')
_MODEL = re.compile(rb'"model"\s*:\s*"([A-Za-z0-9._:/-]{1,48})"')
_MARKS = ((b'"task_started"', "working"), (b'"task_complete"', "idle"), (b'"turn_aborted"', "idle"))
_TOTAL_USAGE = b'"total_token_usage":'
_LAST_USAGE = b'"last_token_usage":'
_decode = json.JSONDecoder().raw_decode
STALL_S = 90


# Sub-commands that are not an agent session.
_NOT_A_SESSION = frozenset(("mcp", "mcp-server", "login", "logout", "completion", "help", "--help", "-h",
                            "--version", "-V", "features", "sandbox", "debug", "apply", "cloud", "update"))


def mode_from_args(argv):
    """tui | exec | resume | review for agent sessions; daemon | other for everything else.

    `codex app-server` is a background daemon (kept alive by systemd, and used by desktop clients).
    It is only an agent while it actually has a session file open.
    """
    if (len(argv) > 1 and argv[1] == "app-server") or (argv and "/app-server-daemon/" in argv[0]):
        return "daemon"
    for a in argv[1:3]:
        if a in ("exec", "resume", "review"):
            return a
    if len(argv) > 1 and argv[1] in _NOT_A_SESSION:
        return "other"
    return "tui"


def open_rollout(pid):
    base = "/proc/%d/fd" % pid
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names[:512]:
        target = procfs.readlink(base + "/" + name)
        if target and target.startswith(SESSIONS) and target.endswith(".jsonl"):
            return target
    return None


def _recent_rollouts(since_epoch, days=3):
    """Rollout files modified at or after since_epoch, newest first, looking back a few days."""
    out = []
    t = time.time()
    for d in range(days):
        day = time.localtime(t - d * 86400)
        folder = "%s/%04d/%02d/%02d" % (SESSIONS, day.tm_year, day.tm_mon, day.tm_mday)
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if e.name.startswith("rollout-") and e.name.endswith(".jsonl"):
                        try:
                            m = e.stat().st_mtime
                        except OSError:
                            continue
                        if m >= since_epoch - 5:
                            out.append((m, e.path))
        except OSError:
            continue
    out.sort(reverse=True)
    return [p for _, p in out]


def find_rollout(pid, cwd, start_epoch):
    """The rollout file of a running codex process: an open fd first, else newest with the same folder."""
    path = open_rollout(pid)
    if path:
        return path
    for cand in _recent_rollouts(start_epoch):
        head = procfs.read1(cand, 1 << 16)
        m = _CWD.search(head or b"")
        if m:
            try:
                if json.loads(b'"' + m.group(1) + b'"') == cwd:
                    return cand
            except ValueError:
                continue
    return None


def _object_after(tail, key):
    i = tail.rfind(key)
    if i < 0:
        return None
    try:
        obj, _ = _decode(tail[i + len(key):i + len(key) + 2000].decode("utf-8", "replace").lstrip())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def rollout_tokens(tail):
    """Running totals Codex writes into its rollout -> the same shape used for Claude sessions, or None."""
    total = _object_after(tail, _TOTAL_USAGE)
    if not total:
        return None
    try:
        inp = int(total.get("input_tokens") or 0)
        cached = int(total.get("cached_input_tokens") or 0)
        out = int(total.get("output_tokens") or 0)
        last = _object_after(tail, _LAST_USAGE) or {}
        return {"in": max(0, inp - cached), "cached": cached, "out": out, "total": inp + out,
                "ctx": int(last.get("input_tokens") or 0)}
    except (TypeError, ValueError):
        return None


def rollout_state(path, bg_active, now):
    """-> dict(state, text, model, since, tokens) from the tail of a rollout file."""
    got = procfs.read_tail(path, 1 << 18)
    if not got:
        return {"state": "unknown", "text": "Running", "model": None, "since": None}
    st = _rollout_state(got[0], got[2], bg_active, now)
    st["tokens"] = rollout_tokens(got[0])
    return st


def _rollout_state(tail, mtime, bg_active, now):
    best = (-1, "unknown")
    for mark, st in _MARKS:
        i = tail.rfind(mark)
        if i > best[0]:
            best = (i, st)
    models = _MODEL.findall(tail)
    model = models[-1].decode() if models else None
    st = best[1]
    if st == "working":
        if not bg_active and now - mtime > STALL_S:
            return {"state": "unknown", "text": "Possibly waiting for you", "model": model, "since": mtime}
        return {"state": "working", "text": "Working", "model": model, "since": mtime}
    if st == "idle":
        return {"state": "idle", "text": "Idle", "model": model, "since": mtime}
    return {"state": "working" if bg_active else "unknown", "text": "Working" if bg_active else "Running",
            "model": model, "since": mtime}


def thread_title(rollout_path):
    """Human name of the thread from ~/.codex/session_index.jsonl, or ''."""
    m = _UUID.search(rollout_path or "")
    if not m:
        return ""
    tid = m.group(1).encode()
    raw = procfs.read(CODEX_HOME + "/session_index.jsonl", 1 << 19)
    if not raw:
        return ""
    for line in reversed(raw.split(b"\n")):
        if tid in line:
            try:
                return clean(json.loads(line).get("thread_name") or "")
            except ValueError:
                return ""
    return ""
