"""One pass over /proc -> process table with per-process CPU deltas that survive short-lived children.

Identity is (pid, starttime): a recycled pid never matches an old record.
"""

import os

from . import procfs


class Proc:
    __slots__ = ("pid", "comm", "state", "ppid", "tty", "own", "child", "start", "rss",
                 "d_own", "d_tree")

    def __init__(self, pid, comm, state, ppid, tty, own, child, start, rss):
        self.pid = pid
        self.comm = comm
        self.state = state
        self.ppid = ppid
        self.tty = tty
        self.own = own          # utime+stime, ticks
        self.child = child      # cutime+cstime of reaped descendants, ticks
        self.start = start      # ticks since boot
        self.rss = rss          # pages
        self.d_own = 0          # ticks this process itself used since the previous scan
        self.d_tree = 0         # d_own + time of descendants that exited since the previous scan

    @property
    def ident(self):
        return (self.pid, self.start)


def scan(root="/proc"):
    procs = {}
    for name in os.listdir(root):
        if not name.isdigit():
            continue
        raw = procfs.read1(root + "/" + name + "/stat")
        if not raw:
            continue
        f = procfs.parse_pid_stat(raw)
        if f is None:
            continue
        pid = int(name)
        procs[pid] = Proc(pid, *f)
    return procs


def compute_deltas(prev, new, prev_boot_ticks):
    """Fill d_own / d_tree on every process in `new`.

    Sampling utime+stime alone misses processes that start and finish between two scans
    (a compiler storm is mostly those). When a child is reaped the kernel folds its lifetime
    total into the parent's cutime/cstime, so a parent's child-time jump, minus what we had
    already counted for those children while they were alive, is exactly the missed time.
    """
    # What we had already seen from processes that have since vanished, credited to their
    # nearest ancestor (in the previous tree) that is still alive.
    seen = {}
    for pid, p in prev.items():
        q = new.get(pid)
        if q is not None and q.start == p.start:
            continue
        a = p.ppid
        for _ in range(64):
            pa = prev.get(a)
            if pa is None:
                break
            qa = new.get(a)
            if qa is not None and qa.start == pa.start:
                seen[a] = seen.get(a, 0) + p.own + p.child
                break
            a = pa.ppid
    for pid, q in new.items():
        p = prev.get(pid)
        if p is not None and p.start == q.start:
            d_own = q.own - p.own
            if d_own < 0:
                d_own = 0
            d_child = q.child - p.child - seen.get(pid, 0)
            q.d_own = d_own
            q.d_tree = d_own + (d_child if d_child > 0 else 0)
        elif prev and q.start >= prev_boot_ticks:
            # Born since the previous scan: all of its time is new.
            q.d_own = q.own
            q.d_tree = q.own + q.child
        # else: first sight of an older process; this scan only sets its baseline.


class Table:
    """Holds the latest scan and the interval it covers."""

    def __init__(self):
        self.procs = {}
        self.boot_s = 0.0
        self.dt = 0.0

    def update(self):
        now = procfs.boot_seconds()
        new = scan()
        if self.procs:
            compute_deltas(self.procs, new, int(self.boot_s * procfs.CLK_TCK))
            self.dt = now - self.boot_s
        self.procs = new
        self.boot_s = now
        return new

    def cores(self, ticks):
        """Ticks used during the last interval -> average cores busy (1.0 = one hardware thread)."""
        if self.dt <= 0:
            return 0.0
        c = ticks / procfs.CLK_TCK / self.dt
        return c if c < procfs.NCPU else float(procfs.NCPU)
