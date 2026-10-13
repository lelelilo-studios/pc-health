"""Bounded polls of the system journal for the few lines that matter:
GPU out-of-memory errors and programs killed because memory ran out."""

import re
import subprocess

JOURNALCTL = "/usr/bin/journalctl"
PATTERN = r"NV_ERR_NO_MEMORY|Out of memory: Killed process|Killed .+ due to memory"
_KILLED = re.compile(r"Killed process \d+ \(([^)]{1,64})\)")
_OOMD = re.compile(r"Killed (\S{1,200}) due to memory")
_SCOPE = re.compile(r"([^/]+?)(?:-[0-9a-f-]{3,})?\.(?:scope|service)$")


def parse(raw):
    """journalctl -o short-unix output -> [(ts, kind, detail)], kind in gpu_oom | oom_kill."""
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        ts_txt, _, rest = line.partition(" ")
        try:
            ts = float(ts_txt)
        except ValueError:
            continue
        if "NV_ERR_NO_MEMORY" in rest:
            out.append((ts, "gpu_oom", ""))
            continue
        m = _KILLED.search(rest)
        if m:
            out.append((ts, "oom_kill", m.group(1)))
            continue
        m = _OOMD.search(rest)
        if m:
            unit = m.group(1).rsplit("/", 1)[-1]
            s = _SCOPE.search(unit)
            out.append((ts, "oom_kill", (s.group(1) if s else unit)[:64]))
    return out


def poll(since_ts, timeout=5.0):
    """Matching journal lines newer than since_ts (epoch seconds). None if the journal is unreadable."""
    args = [JOURNALCTL, "-q", "--no-pager", "-o", "short-unix", "-t", "kernel", "-t", "systemd-oomd",
            "--since", "@%d" % int(since_ts), "-g", PATTERN, "-n", "500"]
    try:
        r = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    # journalctl exits 1 when -g matches nothing; that is "no events", not a failure.
    if r.returncode not in (0, 1):
        return None
    return [e for e in parse(r.stdout) if e[0] > since_ts]
