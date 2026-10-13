"""NVIDIA GPU readings via short-lived `nvidia-smi` calls.

A call costs about 10 ms of CPU. Spawning per sample (rather than keeping `nvidia-smi -l`
resident) keeps ~28 MB out of the monitor's footprint and lets the cadence follow viewers.
"""

import os
import subprocess

SMI = "/usr/bin/nvidia-smi"
MIB = 1024 * 1024
_GPU_ARGS = ["--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,fan.speed",
             "--format=csv,noheader,nounits"]
_PMON_ARGS = ["pmon", "-c", "1", "-s", "m"]


def available():
    return os.access(SMI, os.X_OK)


def _run(args, timeout=3.0):
    try:
        r = subprocess.run([SMI] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def parse_gpu(raw):
    """First GPU line of the --query-gpu CSV -> dict, or None."""
    for line in raw.decode("ascii", "replace").splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 6:
            continue
        util, used, total, temp, power, fan = (_num(x) for x in f[:6])
        if used is None or total is None:
            continue
        return {
            "util_pct": util,
            "vram_used_b": int(used * MIB),
            "vram_total_b": int(total * MIB),
            "temp_c": temp,
            "power_w": power,
            "fan_pct": fan,
        }
    return None


def parse_pmon(raw):
    """`nvidia-smi pmon -s m` -> {pid: (name, vram_bytes)}; includes graphics-only processes."""
    out = {}
    cols = None
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("#"):
            names = line[1:].split()
            if "pid" in names and "fb" in names:
                cols = (names.index("pid"), names.index("fb"), names.index("command"))
            continue
        f = line.split()
        if cols is None or len(f) <= cols[2]:
            continue
        try:
            pid = int(f[cols[0]])
            fb = int(f[cols[1]])
        except ValueError:
            continue
        name, prev = f[cols[2]], out.get(pid)
        out[pid] = (name, fb * MIB + (prev[1] if prev else 0))
    return out


def query_gpu():
    raw = _run(_GPU_ARGS)
    return parse_gpu(raw) if raw else None


def query_procs():
    raw = _run(_PMON_ARGS)
    return parse_pmon(raw) if raw is not None else None
