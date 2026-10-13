"""Size folders without hurting the machine.

Importable (size_of) for small RAM-backed folders, and runnable as a separate lowest-priority
process for big ones:  python3 -m pchealth.dirscan  < JSON list of paths  > one JSON line per path
"""

import json
import os
import sys
import time


def size_of(path, budget_s=120.0, max_entries=3_000_000, duty=1.0):
    """Disk space used under path -> (bytes, entries, partial) or None if unreadable.

    Stays on one filesystem, never follows symlinks, counts hard-linked files once.
    duty < 1 inserts sleeps so the walk uses about that share of one core.
    budget_s is working time (the sleeps do not count against it).
    partial=True means a budget ran out and the size is a lower bound.
    """
    try:
        top = os.lstat(path)
    except OSError:
        return None
    dev = top.st_dev
    total = top.st_blocks * 512
    n = 0
    partial = False
    linked = set()
    stack = [path]
    worked = 0.0
    mark = time.monotonic()
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                n += 1
                if st.st_dev != dev:
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif st.st_nlink > 1:
                    if st.st_ino in linked:
                        continue
                    if len(linked) < 1_000_000:      # bounds this worker's memory on huge trees
                        linked.add(st.st_ino)
                total += st.st_blocks * 512
                if n % 2000 == 0:
                    spent = time.monotonic() - mark
                    worked += spent
                    if worked > budget_s or n >= max_entries:
                        partial = True
                        stack.clear()
                        break
                    if duty < 1.0:
                        time.sleep(min(2.0, spent * (1.0 / duty - 1.0)))
                    mark = time.monotonic()
    return total, n, partial


def main():
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        job = json.load(sys.stdin)
        paths = [p for p in job["paths"] if isinstance(p, str) and p.startswith("/")]
        duty = float(job.get("duty", 0.15))
        budget = float(job.get("budget_s", 120))
    except (ValueError, KeyError, TypeError):
        return 2
    for path in paths:
        t0 = time.monotonic()
        r = size_of(path, budget_s=budget, duty=duty)
        if r is None:
            continue
        sys.stdout.write(json.dumps({"path": path, "b": r[0], "n": r[1], "partial": r[2],
                                     "s": round(time.monotonic() - t0, 1)}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
