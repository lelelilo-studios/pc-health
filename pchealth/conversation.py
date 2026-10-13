"""Earlier turns of a Claude Code conversation, read from the session's own record.

Claude Code draws on the terminal's alternate screen, so tmux keeps no scrollback for it and a copy
of the screen can only ever show the latest part. The whole conversation is in the session's
transcript under ~/.claude/projects/. This module reads pages of it, newest first, when the
view-only page asks, and never otherwise.

Only what a person would call the conversation is returned: what was typed, what the agent
answered, and one line per action it took. Tool output, attachments, images and the model's
private reasoning are never read out. The file format is internal to Claude Code and may change;
whatever is not recognised is skipped.
"""

import json
import os

CHUNK = 1 << 20             # read this much per step
BUDGET = 24 << 20           # and at most this much for one request
MAX_LINE = 256 << 10        # a longer line holds an image or bulky tool output: skipped unread
WANT = 60                   # a page is full once it has this many entries...
WANT_CHARS = 300_000        # ...or this much text
MAX_TEXT = 6000             # characters kept of one message
_TOOL_DETAIL = ("description", "command", "file_path", "path", "pattern", "query", "url", "prompt", "skill", "subject")
# Text the program adds on its own in the person's name: not something they typed.
_NOT_TYPED = ("<local-command", "<system-reminder", "<task-notification", "<command-message", "<bash-",
              "<user-prompt-submit-hook", "[Image: source:")


def _cut(text, limit=MAX_TEXT):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _typed(text):
    """What the person typed, or "" for text the program added on its own."""
    text = text.strip()
    if text.startswith("<command-name>"):               # a slash command: show it as it was typed
        name = text[14:].split("</command-name>", 1)[0].strip()
        args = text.split("<command-args>", 1)[1].split("</command-args>", 1)[0].strip() if "<command-args>" in text else ""
        return (name + " " + args).strip()
    if text.startswith(_NOT_TYPED):
        return ""
    while "<system-reminder>" in text:                  # a note appended to a real message
        head, _, rest = text.partition("<system-reminder>")
        text = (head + rest.partition("</system-reminder>")[2]).strip()
    return text


def _action(block):
    """One line for something the agent did: the tool and what it was pointed at. Never its output."""
    name = str(block.get("name") or "tool").rsplit("__", 1)[-1]
    inp = block.get("input")
    detail = ""
    if isinstance(inp, dict):
        for key in _TOOL_DETAIL:
            if isinstance(inp.get(key), str) and inp[key].strip():
                detail = " ".join(inp[key].split())
                break
    return _cut(name + (": " + detail if detail else ""), 200)


def entries(line):
    """One transcript line -> [{"r": "you" | "agent" | "did", "t": text, "at": timestamp}], oldest first."""
    if len(line) > MAX_LINE or b'"type":"tool_result"' in line:
        return []
    if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
        return []
    try:
        o = json.loads(line)
    except ValueError:
        return []
    if not isinstance(o, dict) or o.get("isSidechain") or o.get("isMeta"):
        return []
    kind = o.get("type")
    message = o.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    at = o.get("timestamp")
    at = at[:40] if isinstance(at, str) else ""
    out = []
    if kind == "user":
        parts = [content] if isinstance(content, str) else [
            b.get("text") if b.get("type") == "text" else "[image]" if b.get("type") == "image" else ""
            for b in (content if isinstance(content, list) else []) if isinstance(b, dict)]
        typed = [t for t in (_typed(p) for p in parts if isinstance(p, str)) if t]
        if any(t != "[image]" for t in typed):
            out.append({"r": "you", "t": _cut("\n".join(typed)), "at": at})
    elif kind == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                out.append({"r": "agent", "t": _cut(b["text"]), "at": at})
            elif b.get("type") == "tool_use":
                out.append({"r": "did", "t": _action(b), "at": at})
    return out


def _newer(path, size, after):
    """Entries added after byte `after`, oldest first."""
    pos = min(max(0, after), size)
    with open(path, "rb") as f:
        f.seek(max(0, pos - 1))
        data = f.read(min(BUDGET, size - pos) + (1 if pos else 0))
    if pos:
        if data[:1] != b"\n":                       # not at the start of a line: move on to the next one
            nl = data.find(b"\n")
            if nl < 0:
                return {"start": None, "end": pos, "items": []}
            pos += nl
            data = data[nl:]
        data = data[1:]
    last = data.rfind(b"\n")                        # a line still being written is left for next time
    items = []
    if last >= 0:
        for line in data[:last].split(b"\n"):
            items.extend(entries(line))
        pos += last + 1
    return {"start": None, "end": pos, "items": items[-4 * WANT:]}


def page(path, before=None, after=None):
    """A page of the conversation in `path`.

    No argument: the newest page. `before`: the page ending at that byte offset (the "start" of an
    earlier answer). `after`: what was added since that offset (the "end" of an earlier answer).
    -> {"start": offset of the oldest line read (0 = the beginning), "end": offset to ask "after", "items": [...]}
    or None if the file cannot be read. Offsets that fall inside a line only cost that line.
    """
    try:
        size = os.path.getsize(path)
        if after is not None:
            return _newer(path, size, after)
        end = size if before is None else min(max(0, before), size)
        pos = first = end
        aligned = None                    # just past the last complete line, for a later "after"
        found, chars, spent = [], 0, 0    # newest first while collecting
        carry, long_carry = b"", False    # the start of the line that began in an earlier chunk
        with open(path, "rb") as f:
            while pos > 0 and spent < BUDGET and len(found) < WANT and chars < WANT_CHARS:
                start = max(0, pos - CHUNK)
                f.seek(start)
                data = f.read(pos - start)
                spent += pos - start
                pos = start
                if aligned is None:
                    cut = data.rfind(b"\n")
                    aligned = start + cut + 1 if cut >= 0 else None
                nl = data.find(b"\n") if start > 0 else -1
                if start > 0 and nl < 0:              # the whole chunk lies inside one long line
                    long_carry = long_carry or len(data) + len(carry) > MAX_LINE
                    carry = b"" if long_carry else data + carry
                    first = start                     # so the next page moves on even if this one found nothing
                    continue
                lines = (data[nl + 1:] if start > 0 else data).split(b"\n")
                if long_carry:
                    lines[-1] = b""                   # the tail of a line too long to keep
                else:
                    lines[-1] += carry
                if start > 0:
                    carry, long_carry = data[:nl], nl > MAX_LINE
                    first = start + nl + 1
                else:
                    carry, long_carry, first = b"", False, 0
                for line in reversed(lines):
                    got = entries(line)
                    for item in reversed(got):
                        found.append(item)
                        chars += len(item["t"])
        found.reverse()
        return {"start": first, "end": aligned if aligned is not None else end, "items": found}
    except OSError:
        return None
