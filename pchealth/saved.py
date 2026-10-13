"""Saved terminals: which tmux sessions to bring back after a reboot, and with which conversation in each.

Saving only reads (tmux through tmux.py, /proc and Claude Code's session files) and then writes one small file
in PC Health's own state folder. Bringing the terminals back is the terminal service's job
(terminal/attach.sh --restore): nothing in this package starts a session or types into one.

The file is kept in two ways. The monitor keeps it in step with the terminals by itself (Keeper, below), by
rules that never forget a terminal merely because it is gone. Save in Settings writes it afresh: exactly what
is running now, which is how terminals that are not coming back are taken off the list.

The file is tab-separated text, because that script reads it too:

    pch-saved-terminals <TAB> 1 <TAB> when (seconds)
    name <TAB> conversation id, or - <TAB> model, or - <TAB> folder

The script is the one that acts on these fields, so it checks every one of them itself; parse() below applies
the same rules, so the page never shows as "saved" something the script would refuse.
"""

import os
import re
import stat
import time

from . import agents_claude, config, history, procfs, proctable, tmux

FILE_NAME = "saved-terminals.tsv"
PATH = config.STATE_DIR + "/" + FILE_NAME
MAGIC = "pch-saved-terminals"
MAX_ROWS = 32
MAX_BYTES = 64 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")           # the names the terminal service is willing to give
CONVERSATION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MODEL = re.compile(r"^[A-Za-z0-9._\[\]:-]{1,48}$")              # what agents_claude accepts as a model name
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def valid_folder(folder):
    """An absolute path that is safe to hand to tmux as a start folder ('#' begins a tmux format)."""
    return (isinstance(folder, str) and folder.startswith("/") and len(folder) <= 1024
            and "#" not in folder and not _CTRL.search(folder))


def valid_row(row):
    # fullmatch: "$" alone would also accept a line break at the end.
    return bool(NAME.fullmatch(row.get("name") or "") and not tmux.EMDASH_NAME.search(row["name"])
                and (not row.get("sid") or CONVERSATION.fullmatch(row["sid"]))
                and (not row.get("model") or MODEL.fullmatch(row["model"]))
                and valid_folder(row.get("folder")))


def dump(rows, ts):
    """Rows ({"name", "sid", "model", "folder"}) -> the file's bytes. Rows that break a rule are left out."""
    lines = ["%s\t1\t%d" % (MAGIC, int(ts))]
    for row in rows[:MAX_ROWS]:
        if valid_row(row):
            lines.append("\t".join((row["name"], row.get("sid") or "-", row.get("model") or "-", row["folder"])))
    return ("\n".join(lines) + "\n").encode()


def parse(raw):
    """The file's bytes -> {"ts", "rows"}, or None if it is not a saved-terminals file. Bad rows are left out.

    A line is cut into fields the way the script's `read` cuts it: tabs next to each other count as one, and
    tabs at either end do not count. So a line means the same to both, also in a file edited by hand."""
    try:
        lines = raw.decode("utf-8").split("\n")
    except (UnicodeDecodeError, AttributeError):
        return None
    fields = lambda line: [f for f in line.split("\t") if f]
    head = fields(lines[0])
    if len(head) != 3 or head[0] != MAGIC or head[1] != "1" or not re.fullmatch(r"[0-9]{1,12}", head[2]):
        return None
    rows, names = [], set()
    for line in lines[1:1 + MAX_ROWS]:
        f = fields(line)
        if len(f) != 4:
            continue
        row = {"name": f[0], "sid": "" if f[1] == "-" else f[1], "model": "" if f[2] == "-" else f[2], "folder": f[3]}
        if valid_row(row) and row["name"] not in names:
            names.add(row["name"])
            rows.append(row)
    return {"ts": int(head[2]), "rows": rows}


def load(path=PATH):
    """The saved terminals, or None. Only a plain file of our own is read (never a link to something else)."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_BYTES:
        return None
    raw = procfs.read(path, MAX_BYTES)
    return parse(raw) if raw else None


def _default_socket(uid):
    """The tmux server the terminal service talks to: the default one, if it is this user's own socket."""
    path = "%s/tmux-%d/default" % (os.environ.get("TMUX_TMPDIR") or "/tmp", uid)
    try:
        st = os.stat(path)
    except OSError:
        return None
    return path if stat.S_ISSOCK(st.st_mode) and st.st_uid == uid else None


def find_conversations(panes, procs, uid, read_session=agents_claude.read_session, argv_of=procfs.pid_cmdline,
                       uid_of=procfs.pid_uid, odd=None):
    """{tmux session name: {"sid", "folder", "model"}} for the sessions that have a Claude Code conversation.

    A conversation belongs to a session when its `claude` process has one of the session's panes as its own
    terminal. A `claude` started by another `claude` is part of that one's work, not a conversation to resume.
    The id and the folder come from Claude Code's own file for that process: the folder is where the
    conversation was started, which is where it has to be resumed from.
    `odd`, if given, is a set that collects the sessions whose conversation cannot be saved because its
    folder's name could not be handed to tmux safely.
    """
    def nested(proc):
        for _ in range(16):
            proc = procs.get(proc.ppid)
            if proc is None:
                return False
            if proc.comm == "claude":
                return True
        return False

    claudes = [p for p in procs.values() if p.comm == "claude" and p.tty and uid_of(p.pid) == uid and not nested(p)]
    out = {}
    # The pane you would land on when joining the session is looked at first.
    for pane in sorted(panes.values(), key=lambda p: (p["session"], not p["active"], p["window"], p["index"])):
        if pane["session"] in out:
            continue
        for proc in claudes:
            if not tmux.same_tty(pane["tty"], proc.tty):
                continue
            sess = read_session(proc.pid, proc.start) or {}
            sid, folder = str(sess.get("sessionId") or ""), sess.get("cwd") or ""
            if CONVERSATION.fullmatch(sid) and valid_folder(folder):
                out[pane["session"]] = {"sid": sid, "folder": folder,
                                        "model": agents_claude.model_from_args(argv_of(proc.pid)) or ""}
                break
            if CONVERSATION.fullmatch(sid) and odd is not None:
                odd.add(pane["session"])
    if odd is not None:
        odd.difference_update(out)
    return out


def rows_for(panes, conversations, home, left=None):
    """One row per tmux session that can be saved, by name. A session with no conversation is saved as a
    terminal that starts in the home folder, which is where the page's "New terminal" starts them too.
    `left`, if given, is a list that collects the sessions that are not saved at all."""
    rows = []
    for name in sorted({p["session"] for p in panes.values()}, key=str.lower):
        talk = conversations.get(name) or {}
        row = {"name": name, "sid": talk.get("sid", ""), "model": talk.get("model", ""), "folder": talk.get("folder") or home}
        if valid_row(row):
            rows.append(row)
        elif left is not None:
            left.append(name)
    if left is not None:
        left.extend(r["name"] for r in rows[MAX_ROWS:])
    return rows[:MAX_ROWS]


def collect(notes=None):
    """The rows for the tmux sessions running right now. `notes`, if given, is a dict that gets what could not be
    saved in full: "odd" (saved without their conversation, see find_conversations) and "left" (not saved)."""
    uid = os.getuid()
    sock = _default_socket(uid)
    panes = tmux.list_panes(sock) if sock else None
    if not panes:
        return []
    odd, left = set(), []
    rows = rows_for(panes, find_conversations(panes, proctable.scan(), uid, odd=odd), os.path.expanduser("~"), left)
    if notes is not None:
        notes.update(odd=sorted(odd & {r["name"] for r in rows}), left=left)
    return rows


def save(path=PATH, rows=None, notes=None):
    """Write the terminals running now to the file. -> (worked, one line to show).

    A save replaces the one before it. Where that lets go of something, the line says so: a terminal that
    was saved with a conversation and has none in it now (Claude was closed there, or has not come up yet)
    is saved as an empty terminal."""
    notes = dict(notes or {})
    if rows is None:
        rows = collect(notes)
    if not rows:
        return False, "No terminals are running, so there is nothing to save. What was saved before is kept."
    before = load(path)
    had_talk = {r["name"] for r in before["rows"] if r["sid"]} if before else set()
    if not history.atomic_write(path, dump(rows, time.time())):
        return False, "The file could not be written (is the disk full?)."
    talks = sum(1 for r in rows if r["sid"])
    said = "%d %s saved, %d with a conversation" % (len(rows), "terminal" if len(rows) == 1 else "terminals", talks)
    emptied = [r["name"] for r in rows if not r["sid"] and r["name"] in had_talk]
    if emptied:
        said += ". Saved without the conversation it had before: " + ", ".join(emptied)
    # A success that leaves something out says what, so the count is not the only hint.
    odd = [name for name in notes.get("odd", ()) if name not in emptied]
    if odd:
        said += ". Saved without its conversation, whose folder has a character in its name that tmux would misread (such as #): " + ", ".join(odd)
    if notes.get("left"):
        said += ". Not saved (a name that cannot be given back to tmux, or more than %d terminals): %s" % (MAX_ROWS, ", ".join(notes["left"]))
    return True, said


def forget(name, path=PATH):
    """Take one terminal off the saved list: it was closed on purpose. -> whether the list changed."""
    kept = load(path)
    rows = [r for r in kept["rows"] if r["name"] != name] if kept else []
    if not kept or len(rows) == len(kept["rows"]):
        return False
    return history.atomic_write(path, dump(rows, time.time()))


SETTLE_S = 20      # a terminal counts once it has been seen for this long...
FORGET_S = 20      # ...and its closing counts once it has been gone this long while tmux itself kept running


class Keeper:
    """Keeps the saved list in step with the terminals as they come and go, without anyone pressing Save.
    The monitor asks it each time it looks at tmux; it only ever answers with rows.

    What must never happen is forgetting terminals because they are gone: after a restart of the PC, or a
    crash of tmux, nothing is running, and that is exactly when the list is needed. So:
    - a row is dropped only when its terminal was closed while the tmux server kept running, and stayed closed
      for a little while (at shutdown the sessions can be seen ending a moment before tmux itself does). When
      the server is gone, or is another one than before, every row stays;
    - a conversation is replaced by the one running in its terminal now. While none runs there (Claude was
      closed, or is not up yet after a restore) the saved one stays;
    - a conversation is followed where it goes: a renamed terminal, or one resumed in another terminal;
    - a new terminal is added once it has been there for a little while.
    """

    def __init__(self):
        self.server = None      # the tmux server these notes are about
        self.seen = {}          # tmux's id of a session -> [first seen, last seen, its name then]
        self.gone = {}          # name -> since when its session is missing while the server runs

    def rows(self, kept, running, ids, server, now):
        """`kept`: the rows in the file. `running`: the rows for the terminals running now (rows_for).
        `ids`: {name: tmux's id of that session}. `server`: which tmux server answered, None if none did.
        `now` only measures how long, so any steady clock will do. -> the rows the file should hold."""
        if server != self.server:
            self.server, self.seen, self.gone = server, {}, {}
        if server is None:
            return list(kept)
        by_name = {r["name"]: r for r in kept}
        names = {r["name"] for r in running}
        here = {ids[name]: name for name in names if name in ids}
        for key, name in here.items():
            note = self.seen.get(key)
            if note is None:
                self.seen[key] = [now, now, name]
                continue
            if note[2] != name and note[2] in by_name and name not in by_name:        # renamed: its row goes along
                by_name[name] = dict(by_name.pop(note[2]), name=name)
            note[1], note[2] = now, name
        for key in [k for k in self.seen if k not in here]:
            first, last, name = self.seen.pop(key)
            if last - first >= SETTLE_S:
                self.gone.setdefault(name, now)
        for name in names:
            self.gone.pop(name, None)
        closed = {name for name, since in self.gone.items() if now - since >= FORGET_S}
        for name in closed:
            del self.gone[name]
        settled = {name for first, last, name in self.seen.values() if last - first >= SETTLE_S}
        live = {r["sid"] for r in running if r["sid"]}
        known = {r["sid"] for r in kept if r["sid"]}
        out = []
        for row in running:
            name, old = row["name"], by_name.get(row["name"])
            if row["sid"]:
                if old or row["sid"] in known or name in settled:
                    out.append(row)
            elif old:
                out.append(row if old["sid"] in live else old)       # its conversation runs elsewhere now, or waits here
            elif name in settled:
                out.append(row)
        have = {r["name"] for r in out}
        for name, row in by_name.items():
            if name not in have and name not in closed and not (row["sid"] and row["sid"] in live):
                out.append(row)
        return out[:MAX_ROWS]
