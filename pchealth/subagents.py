"""Sub-agents an agent is running right now, read from the records Claude Code keeps of them.

Next to its own transcript, a Claude Code session keeps one transcript per sub-agent it starts, and a small
note about each:

    <session id>/subagents/agent-<id>.jsonl        what the sub-agent said and did, line by line
    <session id>/subagents/agent-<id>.meta.json    what kind it is and, in a few words, what it was asked

A sub-agent runs inside the agent's own process, so there is no process to look at. Whether one is still
running is told from the end of its transcript (a finished one ends with its answer, or with handing its work
back) and from how lately the file was written. Only the end of a file is read, only for files written lately,
and only again when the file has changed. Nothing a sub-agent read or wrote is taken from it: the page gets the
note's few words and the name of the tool the sub-agent is using.
"""

import json
import os
import re

from . import procfs, tokens
from .agents_claude import clean

TAIL = 256 * 1024
ACTIVE_S = 15 * 60          # a record not written for this long is not counted as running: we could not tell
MAX_SHOWN = 12
# A sub-agent that calls one of these is giving its result back: that is the last thing it does.
FINISHING = ("SubagentHandback", "StructuredOutput")
_FILE = re.compile(r"^agent-([A-Za-z0-9_-]{1,64})\.jsonl$")
_TOOL = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")


def _context(msg):
    """Tokens in the sub-agent's own conversation as of this reply (what went in to produce it), or 0."""
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return 0
    try:
        return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    except (TypeError, ValueError):
        return 0


def last_step(raw, whole=True):
    """The end of a sub-agent's transcript -> (has it finished?, the tool it is waiting on, or "")."""
    return read_end(raw, whole)[:2]


def read_end(raw, whole=True):
    """The end of a sub-agent's transcript -> (has it finished?, the tool it is waiting on or "", its context).

    `whole` says whether `raw` starts at the start of the file; if not, its first line is a piece of one.
    A sub-agent has finished when the last thing it did was give its answer (a reply that ends its turn) or
    give its result back through a tool made for that. Anything else at the end means it is still at it:
    waiting on a tool, or thinking.
    """
    lines = raw.split(b"\n")
    if not whole:
        lines = lines[1:]
    answered = False                     # a tool's answer (or a new message) came after what we are looking at
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("assistant", "user"):
            continue
        if rec["type"] == "user":
            answered = True
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        used = [b.get("name") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] \
            if isinstance(content, list) else []
        ctx = _context(msg)
        if used:
            name = str(used[-1])
            if name in FINISHING:
                return True, "", ctx
            return False, "" if answered or not _TOOL.match(name) else name, ctx
        if answered:
            return False, "", ctx        # it was told something more after its last words: at it again
        stop = msg.get("stop_reason")
        return bool(stop) and stop != "tool_use", "", ctx
    return False, "", 0


def note(path):
    """The few words Claude Code keeps about a sub-agent -> {"kind", "what", "depth", "started"}."""
    out = {"kind": "", "what": "", "depth": 1, "started": 0}
    try:
        out["started"] = int(os.stat(path).st_mtime)          # the note is written once, when the sub-agent starts
    except OSError:
        return out
    try:
        d = json.loads(procfs.read1(path, 8192) or b"")
    except ValueError:
        return out
    if isinstance(d, dict):
        out["kind"] = clean(d.get("agentType") or "", 32)
        out["what"] = clean(d.get("description") or "", 80)
        depth = d.get("spawnDepth")
        out["depth"] = depth if isinstance(depth, int) and 1 <= depth <= 9 else 1
    return out


def running(transcript, started, now, cache):
    """The sub-agents of the session with this transcript that are running now, oldest first.

    `started` is when the agent's own process started: a record last written before that belongs to an
    earlier run of the conversation and cannot be running in this one. `cache` is a dict kept between calls.
    Each entry: {"id", "kind", "what", "depth", "started", "active", "doing", "ctx"}.
    """
    out = []
    for path in tokens.subagent_files(transcript):
        m = _FILE.match(os.path.basename(path))
        if not m:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < now - ACTIVE_S or st.st_mtime < started - 5:
            cache.pop(path, None)
            continue
        mark = (st.st_size, st.st_mtime_ns)
        hit = cache.get(path)
        if hit is None or hit[0] != mark:
            got = procfs.read_tail(path, TAIL)
            if got is None:
                continue
            finished, doing, ctx = read_end(got[0], got[1] <= TAIL)
            if len(cache) > 400:
                cache.clear()
            hit = cache[path] = (mark, finished, doing, note(path[:-6] + ".meta.json"), ctx)
        if hit[1]:
            continue
        about = hit[3]
        out.append({"id": m.group(1)[:40], "kind": about["kind"], "what": about["what"], "depth": about["depth"],
                    "started": about["started"], "active": int(st.st_mtime), "doing": hit[2], "ctx": hit[4]})
    out.sort(key=lambda x: (x["started"], x["id"]))
    return out
