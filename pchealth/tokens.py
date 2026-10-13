"""Token use per Claude Code session, added up from its transcript files.

Claude Code does not keep a running total for a live session, so this reads the per-message
`usage` records. Each file is read incrementally (only the bytes appended since last time), and
a message that spans several transcript lines (one per content block, each repeating the same
usage) is counted once.
"""

import json
import os
import re

HOME = os.path.expanduser("~")
PROJECTS = HOME + "/.claude/projects"
CHUNK = 4 << 20
KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
_USAGE = re.compile(rb'"usage":\{')
_MSG_ID = re.compile(rb'"id":"(msg_[A-Za-z0-9_-]{1,80})"')
_SID = re.compile(r"[0-9a-fA-F-]{8,64}")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_decode = json.JSONDecoder().raw_decode


class FileState:
    """Progress through one transcript file."""
    __slots__ = ("offset", "ino", "sums", "last_id", "last_vals", "ctx")

    def __init__(self):
        self.offset = 0
        self.ino = 0
        self.sums = [0, 0, 0, 0]          # input, cache write, cache read, output
        self.last_id = b""
        self.last_vals = (0, 0, 0, 0)
        self.ctx = 0                      # input side of the newest message = tokens now in context

    def dump(self):
        return [self.offset, self.ino, self.sums, self.last_id.decode("ascii", "replace"), list(self.last_vals), self.ctx]

    @classmethod
    def load(cls, data):
        st = cls()
        st.offset, st.ino = int(data[0]), int(data[1])
        st.sums = [int(x) for x in data[2]][:4]
        st.last_id = str(data[3]).encode()
        st.last_vals = tuple(int(x) for x in data[4])[:4]
        st.ctx = int(data[5])
        if len(st.sums) != 4 or len(st.last_vals) != 4:
            raise ValueError("bad token state")
        return st


def _usage_at(data, pos, line_end):
    """Decode the JSON object after '"usage":' at pos, if it is a model usage record."""
    for limit in (pos + 8 + 6000, line_end):
        try:
            obj, _ = _decode(data[pos + 8:min(limit, line_end)].decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict) and "input_tokens" in obj and "output_tokens" in obj:
            return obj
        return None
    return None


def parse_region(data, st):
    """Fold every complete transcript line in `data` into st. Returns the number of bytes consumed."""
    end = data.rfind(b"\n") + 1
    if end == 0:
        return 0
    done_line = -1
    for m in _USAGE.finditer(data, 0, end):
        pos = m.start()
        line_start = data.rfind(b"\n", 0, pos) + 1
        if line_start == done_line:
            continue                      # this line's usage is already counted
        line_end = data.find(b"\n", pos, end)
        if data.find(b'"type":"assistant"', line_start, line_end) < 0:
            continue
        mid = _MSG_ID.search(data, line_start, line_end)
        if mid is None:
            continue                      # not a model reply (e.g. a tool result quoting one)
        usage = _usage_at(data, pos, line_end)
        if usage is None:
            continue
        done_line = line_start
        try:
            vals = tuple(int(usage.get(k) or 0) for k in KEYS)
        except (TypeError, ValueError):
            continue
        mid = mid.group(1)
        if mid == st.last_id:
            for i in range(4):
                st.sums[i] += vals[i] - st.last_vals[i]
        else:
            for i in range(4):
                st.sums[i] += vals[i]
        st.last_id, st.last_vals = mid, vals
        st.ctx = vals[0] + vals[1] + vals[2]
    return end


def scan_file(path, st, max_bytes=1 << 31):
    """Read what was appended to path since last time. Returns bytes read, or -1 if unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return -1
    try:
        info = os.fstat(fd)
        if info.st_ino != st.ino or info.st_size < st.offset:
            st.__init__()                 # a different or rewritten file: start over
            st.ino = info.st_ino
        if info.st_size == st.offset:
            return 0
        os.lseek(fd, st.offset, os.SEEK_SET)
        read = 0
        carry = b""
        while read < max_bytes:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            read += len(chunk)
            data = carry + chunk if carry else chunk
            used = parse_region(data, st)
            st.offset += used
            carry = data[used:]
            if len(carry) > 64 << 20:
                # One line longer than 64 MB carries no usage we can trust; skip past it.
                st.offset += len(carry)
                carry = b""
        return read
    except OSError:
        return -1
    finally:
        os.close(fd)


def transcript_path(cwd, session_id, projects=PROJECTS):
    """The main transcript of a session, or None. The folder is named after the session's start folder."""
    if not session_id or not _SID.fullmatch(str(session_id)):
        return None
    if cwd:
        path = "%s/%s/%s.jsonl" % (projects, _NON_ALNUM.sub("-", cwd), session_id)
        if os.path.isfile(path):
            return path
    try:
        for name in os.listdir(projects):
            path = "%s/%s/%s.jsonl" % (projects, name, session_id)
            if os.path.isfile(path):
                return path
    except OSError:
        pass
    return None


def subagent_files(transcript):
    """Transcripts of the subagents a session started (they spend tokens on its behalf)."""
    base = transcript[:-6] + "/subagents"
    found = []
    stack = [(base, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if e.name.endswith(".jsonl"):
                        found.append(e.path)
                    elif depth < 3 and e.is_dir(follow_symlinks=False):
                        stack.append((e.path, depth + 1))
        except OSError:
            continue
    return found


class Session:
    __slots__ = ("path", "files", "seen")

    def __init__(self, path):
        self.path = path
        self.files = {}                   # file path -> FileState
        self.seen = 0.0

    def backlog(self):
        """Bytes of the main transcript not read yet."""
        st = self.files.get(self.path)
        try:
            return max(0, os.stat(self.path).st_size - (st.offset if st else 0))
        except OSError:
            return 0

    def totals(self):
        t = [0, 0, 0, 0]
        for st in self.files.values():
            for i in range(4):
                t[i] += st.sums[i]
        main = self.files.get(self.path)
        return {"in": t[0] + t[1], "cached": t[2], "out": t[3], "total": sum(t),
                "ctx": main.ctx if main else 0}


class Counter:
    """Token totals for the sessions that are running, kept up to date a little at a time."""

    def __init__(self, projects=PROJECTS):
        self.projects = projects
        self.sessions = {}                # session id -> Session
        self.dirty = False

    def update(self, live, now, budget_bytes=600 << 20):
        """live: {session id: start folder}. Reads new transcript bytes, up to budget_bytes per call.
        Returns {session id: totals dict} for the sessions whose files are fully read."""
        out = {}
        todo = []
        for sid, cwd in live.items():
            s = self.sessions.get(sid)
            if s is None:
                path = transcript_path(cwd, sid, self.projects)
                if path is None:
                    continue
                s = self.sessions[sid] = Session(path)
            s.seen = now
            todo.append((s.backlog(), sid, s))
        todo.sort(key=lambda t: t[0])         # least unread first: one huge transcript must not hold up the rest
        for _, sid, s in todo:
            complete = True
            for path in [s.path] + subagent_files(s.path):
                st = s.files.get(path)
                if st is None:
                    st = s.files[path] = FileState()
                if budget_bytes <= 0:
                    complete = False              # the rest is read on the next call
                    break
                n = scan_file(path, st, budget_bytes)
                if n > 0:
                    budget_bytes -= n
                    self.dirty = True
            if complete:
                out[sid] = s.totals()
        return out

    def forget(self, now, keep_s=3 * 86400):
        for sid in [k for k, s in self.sessions.items() if now - s.seen > keep_s]:
            del self.sessions[sid]

    def dump(self):
        return {sid: {"path": s.path, "seen": int(s.seen),
                      "files": {p: st.dump() for p, st in s.files.items()}}
                for sid, s in self.sessions.items()}

    def load(self, data):
        try:
            for sid, rec in data.items():
                s = Session(str(rec["path"]))
                s.seen = float(rec.get("seen", 0))
                for p, raw in rec["files"].items():
                    s.files[str(p)] = FileState.load(raw)
                self.sessions[str(sid)] = s
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            self.sessions = {}
