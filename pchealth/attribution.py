"""Which process belongs to which agent, and what each agent (and each leftover) is using.

Runs on every scan and touches no files: it works from the process table plus facts the
slow worker has already gathered (`Enrich`).
"""

import math
import os
from collections import deque

from . import agents_claude, leftovers, procfs
from .agents_claude import clean

AGENT_COMMS = {"claude": "claude", "codex": "codex"}
# Plumbing that says nothing about what an agent is doing.
QUIET_COMMS = frozenset(("bash", "sh", "zsh", "fish", "dash", "timeout", "sleep", "env", "tee", "cat",
                         "tail", "head", "setsid", "nohup", "xargs", "sudo"))
STATE_ORDER = {"waiting": 0, "working": 1, "unknown": 2, "idle": 3}
HOME = os.path.expanduser("~")
_branches = {}                  # folder -> (when read, branch): looked at again every 15 s at most


def git_branch(folder, now):
    """The branch checked out in a folder ('' if it is not a git checkout; a short commit id when detached)."""
    hit = _branches.get(folder)
    if hit and now - hit[0] < 15:
        return hit[1]
    head = procfs.read(folder + "/.git/HEAD", 256)
    if head is None:                              # a worktree: ".git" is a file that points at its git folder
        pointer = procfs.read(folder + "/.git", 512)
        if pointer and pointer.startswith(b"gitdir:"):
            head = procfs.read(os.path.join(folder, pointer[7:].strip().decode("utf-8", "replace"), "HEAD"), 256)
    text = (head or b"").decode("utf-8", "replace").strip()
    branch = text[16:] if text.startswith("ref: refs/heads/") else text[:8] if text and " " not in text else ""
    if len(_branches) > 256:
        _branches.clear()
    _branches[folder] = (now, clean(branch, 60))
    return _branches[folder][1]
MISSING = object()


def order_key(state, state_since, name):
    """Sort key shared by the Agents table and the Terminals cards.

    Waiting for the user first, then working, then the rest. Idle sessions go most recently
    idle first (the one that just finished is the one most likely to need a look); the other
    groups go by name. A row only moves when its state changes, never because a number flickered.
    """
    if state != "idle":
        return (STATE_ORDER.get(state, 9), 0, 0, name.lower())
    # An idle session whose idle time is not known goes after the ones where it is.
    return (STATE_ORDER["idle"], 0 if state_since else 1, -(state_since or 0), name.lower())
MB = 1 << 20


class Enrich:
    """Facts gathered by the slow worker thread. The sampler only reads them.

    Every value is replaced or set whole, so a reader never sees a half-built entry.
    """

    def __init__(self):
        self.env = {}        # ident -> whitelisted vars | {} (nothing relevant / not ours) | None (unreadable)
        self.sess = {}       # agent ident -> whitelisted session-file fields | None
        self.root = {}       # agent ident -> {"cwd", "model", "mode"}
        self.codex = {}      # agent ident -> {"state", "text", "model", "since", "title"}
        self.mem = {}        # agent ident -> {"mem_b", "swap_b", "write_bps", "written_1h_b"}
        self.group = {}      # leftover-group ident -> {"what", "ports", "mem_b", "swap_b"}
        self.suspect = {}    # ident -> label | None, for unmarked processes with a leftover shape
        self.scratch = {}    # session id -> (bytes, partial)
        self.gpu_procs = {}  # pid -> (name, vram bytes)
        self.tokens = {}     # session id -> {"in", "cached", "out", "total", "ctx"}
        self.tmux = {}       # agent ident -> {"socket", "pane", "session", "window", "w", "h"}
        self.sessions = {}   # tmux session name -> its active pane's info, plus "socket" and "pane"
        self.saved = None    # the terminals last saved to be brought back (saved.py): {"ts", "rows"} | None
        self.subagents = {}  # session id -> the sub-agents that session is running now (subagents.py)


class Agent:
    def __init__(self, ident, kind, now):
        self.ident = ident
        self.kind = kind
        self.first_seen = now
        self.parent = None
        self.pids = []
        self.helpers = []
        self.cores = 0.0
        self.root_cores = 0.0
        self.window = deque()        # (boot seconds, descendant ticks) for the last ~30 s
        self.bg_active = False
        self.doing = []
        self.vram_b = 0
        self.label = kind
        self.title = ""
        self.host = "terminal"
        self.model = None
        self.sid = None
        self.cwd = ""
        self.state = "unknown"
        self.state_text = "Starting"
        self.state_since = None
        self.started = now
        self.hidden = False          # a process named like an agent that is not (yet) an agent session

    @property
    def id(self):
        return "%s:%d:%d" % (self.kind, self.ident[0], self.ident[1])


class Group:
    """Processes an agent started that are no longer underneath it."""

    def __init__(self, ident, now):
        self.ident = ident
        self.first_seen = now
        self.key = None
        self.pids = []
        self.cores = 0.0
        self.cores_avg = 0.0
        self.agent = None


class Attribution:
    def __init__(self, enrich, thresholds, ignore=()):
        self.e = enrich
        self.thresholds = thresholds
        self.ignore = frozenset(ignore)
        self.agents = {}             # ident -> Agent, live
        self.groups = {}             # ident -> Group
        self.sticky = {}             # process ident -> agent ident it was first seen under
        self.owner = {}              # pid -> root pid (0 = no agent) for the latest scan
        self.sid_agent = {}          # session id -> live agent ident
        self.known = {}              # agent ident or ("sid", id) -> {"label", "ended"}; survives restarts
        self.ended = deque(maxlen=40)
        self.pub = {"roots": {}, "members": {}, "groups": {}, "env_wanted": [], "suspect_wanted": []}
        self.dirty = False           # registry changed since last save

    # ---- per-scan update -------------------------------------------------

    def update(self, table, now):
        procs = table.procs
        e = self.e
        boot_now = table.boot_s
        tck = procfs.CLK_TCK

        cand_roots = {pid for pid, p in procs.items() if p.comm in AGENT_COMMS and p.state != "Z"}
        roots = set()
        for pid in cand_roots:
            p = procs[pid]
            nested = False
            a, hops = p.ppid, 0
            while a in procs and hops < 64:
                if a in cand_roots:
                    nested = True
                    break
                a, hops = procs[a].ppid, hops + 1
            # A `claude` under another agent is its own agent only once a session file confirms it;
            # until then it is just a helper process of its parent.
            if not nested or p.comm == "codex" or e.sess.get((pid, p.start)):
                roots.add(pid)

        owner = {pid: pid for pid in roots}
        for pid in procs:
            chain = []
            cur = pid
            while True:
                o = owner.get(cur)
                if o is not None:
                    break
                p = procs.get(cur)
                if p is None or len(chain) > 128:
                    o = 0
                    break
                chain.append(cur)
                cur = p.ppid
            for c in chain:
                owner[c] = o
        self.owner = owner
        root_ident = {pid: (pid, procs[pid].start) for pid in roots}

        live = {}
        for pid in roots:
            ident = root_ident[pid]
            ag = self.agents.get(ident)
            if ag is None:
                ag = Agent(ident, AGENT_COMMS[procs[pid].comm], now)
                ag.started = now - (boot_now - ident[1] / tck)
            par = owner.get(procs[pid].ppid, 0)
            ag.parent = root_ident[par] if par else None
            ag.helpers = []
            live[ident] = ag
        for ident, ag in self.agents.items():
            if ident not in live:
                self._ended(ag, now)
        self.agents = live

        members = {ident: [] for ident in live}
        sticky = {}
        unattached = []
        for pid, p in procs.items():
            o = owner[pid]
            if o:
                ri = root_ident[o]
                members[ri].append(pid)
                sticky[(pid, p.start)] = ri
            else:
                unattached.append(p)

        init_pids = {1} | {pid for pid, p in procs.items() if p.comm == "systemd" and p.ppid == 1}
        cand = {}
        env_wanted = []
        suspect_wanted = []
        for p in unattached:
            ident = (p.pid, p.start)
            key = self.sticky.get(ident)
            if key is not None:
                sticky[ident] = key
            elif p.comm not in self.ignore:
                env = e.env.get(ident, MISSING)
                age = boot_now - p.start / tck
                if env is MISSING:
                    if age >= 10:
                        env_wanted.append(ident)
                    continue
                if env and env.get("CLAUDECODE") and env.get("CLAUDE_CODE_SESSION_ID"):
                    sid = env["CLAUDE_CODE_SESSION_ID"]
                    key = self.sid_agent.get(sid) or ("sid", sid)
                elif env == {} and p.ppid in init_pids and p.tty == 0 and p.comm in leftovers.SUSPECT_COMMS \
                        and age >= leftovers.SUSPECT_AGE_S:
                    s = e.suspect.get(ident, MISSING)
                    if s is MISSING:
                        suspect_wanted.append(ident)
                    elif s:
                        key = ("suspect", ident)
            if key is not None:
                cand[p.pid] = key
        # Anything underneath a candidate belongs with it (e.g. a headless browser's renderers).
        for p in unattached:
            if p.pid in cand:
                continue
            a, hops = p.ppid, 0
            while a in procs and hops < 32:
                k = cand.get(a)
                if k is not None:
                    cand[p.pid] = k
                    break
                if a in init_pids:
                    break
                a, hops = procs[a].ppid, hops + 1
        self.sticky = sticky

        grouped = {}
        for pid, key in cand.items():
            top, hops = pid, 0
            while hops < 64 and cand.get(procs[top].ppid) == key:
                top, hops = procs[top].ppid, hops + 1
            g = grouped.get(top)
            if g is None:
                grouped[top] = (key, [pid])
            else:
                g[1].append(pid)

        alpha = 1.0 - math.exp(-table.dt / 300.0) if table.dt > 0 else 0.0
        groups = {}
        for top, (key, pids) in grouped.items():
            gi = (top, procs[top].start)
            g = self.groups.get(gi) or Group(gi, now)
            g.key, g.pids = key, pids
            g.cores = table.cores(sum(procs[x].d_tree for x in pids))
            g.cores_avg += (g.cores - g.cores_avg) * alpha
            if key[0] == "sid":
                key = self.sid_agent.get(key[1], key)
            g.agent = live.get(key)
            if g.agent is not None:
                g.agent.helpers.append(g)
            groups[gi] = g
        self.groups = groups

        gpu_procs = e.gpu_procs
        for ident, ag in live.items():
            self._describe(ag, now)
            pids = members[ident] + [x for g in ag.helpers for x in g.pids]
            ag.pids = pids
            root_pid = ident[0]
            ticks = 0
            by = {}
            vram = 0
            young = False
            for x in pids:
                p = procs[x]
                ticks += p.d_tree
                gp = gpu_procs.get(x)
                if gp:
                    vram += gp[1]
                if x != root_pid:
                    c = by.get(p.comm)
                    if c is None:
                        by[p.comm] = [1, p.d_tree]
                    else:
                        c[0] += 1
                        c[1] += p.d_tree
                    if 3 <= boot_now - p.start / tck <= 30:
                        young = True
            root_ticks = procs[root_pid].d_own
            ag.cores = table.cores(ticks)
            ag.root_cores = table.cores(root_ticks)
            ag.vram_b = vram
            win = ag.window
            win.append((boot_now, ticks - root_ticks))
            while win and boot_now - win[0][0] > 30:
                win.popleft()
            span = max(boot_now - win[0][0] + table.dt, 1.0)
            desc_cores = sum(t for _, t in win) / tck / span
            ag.bg_active = young or desc_cores >= 0.02
            items = sorted(by.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))
            busy = [kv for kv in items if kv[1][1] > 0]
            loud = [kv for kv in busy if kv[0] not in QUIET_COMMS] or busy
            ag.doing = [{"name": clean(n, 24), "n": c[0], "cpu_cores": round(table.cores(c[1]), 2)}
                        for n, c in loud[:3]]
            self._state(ag, now)

        self.pub = {
            "roots": {(pid, procs[pid].start): AGENT_COMMS[procs[pid].comm] for pid in cand_roots},
            "members": {ident: list(ag.pids) for ident, ag in live.items()},
            "groups": {gi: list(g.pids) for gi, g in groups.items()},
            "env_wanted": env_wanted[:300],
            "suspect_wanted": suspect_wanted[:50],
        }

    def _describe(self, ag, now):
        e = self.e
        sess = e.sess.get(ag.ident)
        env = e.env.get(ag.ident) or {}
        info = e.root.get(ag.ident) or {}
        ag.cwd = (sess or {}).get("cwd") or info.get("cwd") or ag.cwd
        if ag.kind == "claude":
            ag.label, ag.title, ag.host = agents_claude.describe(sess, env, ag.cwd)
            ag.model = info.get("model") or ag.model
            sid = (sess or {}).get("sessionId")
            if sid and sid != ag.sid:
                ag.sid = sid
                self.sid_agent[sid] = ag.ident
        else:
            cx = e.codex.get(ag.ident) or {}
            # Hidden until we know what it is, and for daemons/one-off commands with no session open.
            ag.hidden = not info or (info.get("mode") in ("daemon", "other") and not cx.get("has_rollout"))
            project = os.path.basename(ag.cwd.rstrip("/")) if ag.cwd else "codex"
            ag.label, ag.title = clean(project), cx.get("title") or ""
            ag.host = "emdash" if env.get("EMDASH_TASK_NAME") else "terminal"
            ag.model = cx.get("model") or ag.model
        if ag.parent is None and env.get("CLAUDE_PID", "").isdigit():
            for ident in self.agents:
                if ident[0] == int(env["CLAUDE_PID"]) and ident != ag.ident:
                    ag.parent = ident
        rec = {"label": ag.label, "ended": None}
        if self.known.get(ag.ident) != rec:
            self.known[ag.ident] = rec
            if ag.sid:
                self.known[("sid", ag.sid)] = rec
            self.dirty = True

    def _state(self, ag, now):
        if ag.kind == "claude":
            sess = self.e.sess.get(ag.ident)
            if not sess and now - ag.first_seen < 20:
                ag.state, ag.state_text, ag.state_since = "unknown", "Starting", None
                return
            ag.state, ag.state_text, ag.state_since = agents_claude.state(
                sess, ag.bg_active, ag.root_cores >= 0.02, now)
        else:
            cx = self.e.codex.get(ag.ident)
            if cx:
                ag.state, ag.state_text, ag.state_since = cx["state"], cx["text"], cx.get("since")
            else:
                ag.state, ag.state_text = ("working", "Working") if ag.bg_active else ("unknown", "Running")

    def _ended(self, ag, now):
        if not ag.hidden and (ag.sid or now - ag.started >= 30):   # one-shots and daemons are not news
            self.ended.appendleft({"id": ag.id, "kind": ag.kind, "label": ag.label, "title": ag.title,
                                   "ended": int(now), "ran_s": int(now - ag.started)})
        rec = {"label": ag.label, "ended": int(now)}
        self.known[ag.ident] = rec
        if ag.sid:
            self.known[("sid", ag.sid)] = rec
            if self.sid_agent.get(ag.sid) == ag.ident:
                del self.sid_agent[ag.sid]
        self.dirty = True

    # ---- views for the snapshot -----------------------------------------

    def export_agents(self, now):
        e = self.e
        out = []
        for ag in self.agents.values():
            if ag.hidden:
                continue
            if ag.kind == "claude" and not e.sess.get(ag.ident) and now - ag.started < 6:
                continue          # a `claude --version` style one-shot: gone before it is worth a row
            mem = e.mem.get(ag.ident) or {}
            scratch = e.scratch.get(ag.sid) if ag.sid else None
            pane = e.tmux.get(ag.ident)
            out.append({
                "id": ag.id, "kind": ag.kind, "pid": ag.ident[0], "label": ag.label, "title": ag.title,
                "host": ag.host, "parent": self.agents[ag.parent].id if ag.parent in self.agents else None,
                "state": ag.state, "state_text": ag.state_text,
                "state_since": int(ag.state_since) if ag.state_since else None,
                "bg_active": ag.bg_active, "started": int(ag.started),
                "cpu_cores": round(ag.cores, 2), "mem_b": mem.get("mem_b"), "swap_b": mem.get("swap_b"),
                "vram_b": ag.vram_b, "tmp_b": scratch[0] if scratch else None,
                "write_bps": mem.get("write_bps"), "written_1h_b": mem.get("written_1h_b"),
                "procs": len(ag.pids), "helpers": len(ag.helpers), "doing": ag.doing, "model": ag.model,
                # Where it works, for the terminal's summary: the folder (home shortened to ~) and its git branch.
                "folder": clean("~" + ag.cwd[len(HOME):] if ag.cwd.startswith(HOME + "/") or ag.cwd == HOME else ag.cwd, 120),
                "branch": git_branch(ag.cwd, now) if ag.cwd else "",
                "tokens": (e.tokens.get(ag.sid) if ag.sid else None) if ag.kind == "claude"
                else (e.codex.get(ag.ident) or {}).get("tokens"),
                # Present when the agent runs inside tmux: its screen can be viewed (never typed into).
                "term": {"session": clean(pane["session"], 40), "window": pane["window"]} if pane else None,
            })
        out.sort(key=lambda a: order_key(a["state"], a["state_since"], a["label"]) + (a["started"], a["id"]))
        # Keep nested agents directly under the agent that started them.
        by_parent = {}
        for a in out:
            by_parent.setdefault(a["parent"], []).append(a)
        ordered = []
        ids = {a["id"] for a in out}

        def place(a):
            ordered.append(a)
            for child in by_parent.get(a["id"], ()):
                place(child)
        for a in out:
            if a["parent"] not in ids:
                a["parent"] = None
                place(a)
        return ordered

    def export_leftovers(self, table, now):
        e = self.e
        procs = table.procs
        boot_now = table.boot_s
        out = []
        for gi, g in self.groups.items():
            root = procs.get(gi[0])
            if root is None:
                continue
            ag = g.agent
            age = boot_now - gi[1] / procfs.CLK_TCK
            if g.key[0] == "suspect":
                kind, origin, from_state, since = "suspect", "", "unknown", g.first_seen
            elif ag is not None:
                idle_for = now - ag.state_since if ag.state == "idle" and ag.state_since else 0
                if idle_for < leftovers.IDLE_FLAG_S:
                    continue                      # a live agent's helper: counted on its row instead
                kind, origin, from_state, since = "detached", ag.label, ag.state, ag.state_since
            else:
                rec = self.known.get(g.key) or {}
                ended = rec.get("ended")
                since = max(g.first_seen, ended) if ended else now - age
                if now - since < leftovers.GRACE_S:
                    continue
                kind, origin, from_state = "orphan", rec.get("label") or "", "ended"
            d = e.group.get(gi) or {}
            mem_b = d.get("mem_b") or root.rss * procfs.PAGE
            vram = sum(e.gpu_procs[x][1] for x in g.pids if x in e.gpu_procs)
            out.append({
                "id": "%d:%d" % gi, "pid": gi[0], "kind": kind,
                "what": d.get("what") or clean(root.comm, 24), "n_procs": len(g.pids),
                "from": origin, "from_state": from_state,
                "from_ended": (self.known.get(g.key) or {}).get("ended") if kind == "orphan" else None,
                "age_s": int(age), "cpu_cores": round(g.cores, 2), "cpu_avg_cores": round(g.cores_avg, 2),
                "mem_b": mem_b, "vram_b": vram, "ports": d.get("ports") or [],
                "level": leftovers.level(kind, now - since, g.cores_avg, mem_b, vram, self.thresholds),
                "cmd": leftovers.kill_command([gi[0]]),
            })
        out.sort(key=lambda r: ({"crit": 0, "warn": 1, "info": 2}[r["level"]], -r["cpu_avg_cores"], -r["mem_b"]))
        return out

    def export_ended(self, now, window_s=24 * 3600):
        return [x for x in self.ended if now - x["ended"] <= window_s]

    def label_of(self, pid):
        """Label of the agent a process belongs to, or None."""
        o = self.owner.get(pid)
        if not o:
            return None
        for ident, ag in self.agents.items():
            if ident[0] == o:
                return ag.label
        return None

    # ---- registry persistence -------------------------------------------

    def forget_old(self, now, keep_s=14 * 86400):
        """Drop what we remember about agents that ended long ago (called from the worker thread)."""
        for key, rec in list(self.known.items()):
            if rec["ended"] and now - rec["ended"] > keep_s:
                self.known.pop(key, None)

    def dump_registry(self):
        known = []
        for key, rec in list(self.known.items()):       # the sampler thread adds entries meanwhile
            if key[0] == "sid":
                known.append({"sid": key[1], "label": rec["label"], "ended": rec["ended"]})
        known.sort(key=lambda r: r["ended"] or 1 << 62)
        return {"sessions": known[-300:], "ended": list(self.ended)}

    def load_registry(self, data, now):
        try:
            for r in data.get("sessions", []):
                # Anything "live" in a saved registry ended while the monitor was not running.
                self.known[("sid", str(r["sid"]))] = {"label": clean(str(r["label"])),
                                                      "ended": r.get("ended") or int(now)}
            for x in data.get("ended", []):
                if isinstance(x, dict) and {"id", "label", "ended"} <= x.keys():
                    self.ended.append({"id": str(x["id"]), "kind": str(x.get("kind", "claude")),
                                       "label": clean(str(x["label"])), "title": clean(str(x.get("title", ""))),
                                       "ended": int(x["ended"]), "ran_s": int(x.get("ran_s", 0))})
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
