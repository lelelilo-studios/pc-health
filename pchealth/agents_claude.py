"""Claude Code sessions: the per-PID status file, labels, state mapping, scratch folders.

~/.claude/sessions/<pid>.json is an internal Claude Code file, so everything here is tolerant:
missing keys and unknown values degrade to "unknown", never to an error.
"""

import json
import os
import re

from . import procfs

HOME = os.path.expanduser("~")
SESSIONS_DIR = HOME + "/.claude/sessions"
# Only these keys are ever copied out of a session file (it also holds a messaging socket path).
SESSION_KEYS = ("sessionId", "cwd", "startedAt", "procStart", "status", "waitingFor",
                "statusUpdatedAt", "name", "kind")
# Only these variables are ever read from a process environment (which also holds tokens).
ENV_KEYS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID",
            "EMDASH_TASK_NAME", "EMDASH_ROOT_PATH", "TERM_PROGRAM", "TMUX", "TMUX_PANE")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MODEL = re.compile(r"^[A-Za-z0-9._\[\]:-]{1,48}$")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
WAIT_SETTLE_S = 20
QUIET_S = 600


def clean(text, cap=80):
    """Make a string safe to show: no control characters, bounded length."""
    if not isinstance(text, str):
        return ""
    text = _CTRL.sub(" ", text).strip()
    return text if len(text) <= cap else text[:cap - 1] + "…"


def read_session(pid, start_ticks, directory=SESSIONS_DIR):
    """Whitelisted fields of the session file for this exact process, or None.

    procStart must equal the process start time, otherwise the file belongs to an
    earlier process that had the same pid.
    """
    raw = procfs.read1("%s/%d.json" % (directory, pid), 16384)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or str(d.get("procStart")) != str(start_ticks):
        return None
    return {k: d[k] for k in SESSION_KEYS if k in d}


def describe(session, env, cwd):
    """-> (label, title, host). label names the project (and Emdash task); title is the session's own name."""
    env = env or {}
    session = session or {}
    folder = session.get("cwd") or cwd or ""
    task = env.get("EMDASH_TASK_NAME")
    root = env.get("EMDASH_ROOT_PATH")
    host = "emdash" if (task or env.get("TERM_PROGRAM") == "emdash") else "terminal"
    project = os.path.basename((root or folder).rstrip("/")) or "session"
    label = "%s / %s" % (project, task) if task and task != project else project
    return clean(label), clean(session.get("name") or ""), host


def model_from_args(argv):
    for i, a in enumerate(argv[:-1]):
        if a == "--model" and _MODEL.match(argv[i + 1]):
            return argv[i + 1]
    return None


def state(session, bg_active, root_busy, now):
    """-> (state, text, since_epoch). state is working | waiting | idle | unknown."""
    if not session:
        return ("working", "Working", None) if bg_active else ("unknown", "Running (state unknown)", None)
    status = session.get("status")
    since = session.get("statusUpdatedAt")
    since = since / 1000.0 if isinstance(since, (int, float)) else None
    age = now - since if since else 0
    if status == "busy":
        if not bg_active and not root_busy and age >= QUIET_S:
            return "working", "Working, quiet for %d min" % (age // 60), since
        return "working", "Working", since
    if status == "waiting":
        if since and age < WAIT_SETTLE_S:
            return "working", "Working", since       # brief prompts that resolve themselves
        why = clean(session.get("waitingFor") or "", 40)
        text = "Waiting for you" + (": " + why if why else "")
        if bg_active:
            text += " (background work still running)"
        return "waiting", text, since
    if status == "idle":
        return "idle", "Idle" + (", background work still running" if bg_active else ""), since
    return "unknown", clean(str(status), 30) or "Running (state unknown)", since


def scratch_dir(uid, cwd, session_id):
    """The session's temp folder under /tmp (RAM-backed on this PC), or None if it does not exist."""
    if not cwd or not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return None
    base = "/tmp/claude-%d" % uid
    path = "%s/%s/%s" % (base, _NON_ALNUM.sub("-", cwd), session_id)
    if os.path.isdir(path):
        return path
    try:
        for proj in os.listdir(base):
            alt = "%s/%s/%s" % (base, proj, session_id)
            if os.path.isdir(alt):
                return alt
    except OSError:
        pass
    return None
