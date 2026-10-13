"""Health rules: turn the numbers into a short list of plain-language findings.

`findings()` is pure (snapshot in, candidate findings out). `Engine` adds time hysteresis:
a finding must persist for its hold time before it is shown, and must stay away for a while
before it clears, so values hovering at a threshold do not flap.
"""

import time

GB = 1 << 30
MB = 1 << 20
RANK = {"ok": 0, "info": 0, "warn": 1, "crit": 2}
CLEAR_S = 45


def gb(b, digits=None):
    v = b / GB
    if digits is None:
        digits = 0 if v >= 20 else 1
    return ("%.*f GB" % (digits, v)).replace(".0 GB", " GB")


def size(b):
    return gb(b) if b >= GB else "%d MB" % (b / MB)


def dur(s):
    s = int(s)
    if s < 90:
        return "%d seconds" % s
    if s < 5400:
        return "%d minutes" % round(s / 60)
    if s < 36 * 3600:
        return "%d hours" % round(s / 3600)
    return "%d days" % round(s / 86400)


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def cores(c):
    return ("%.1f" % c).rstrip("0").rstrip(".")


def _two(threshold, value, below=False):
    """(warn, crit) thresholds -> 'crit' | 'warn' | None. A threshold of 0 disables that level."""
    w, c = threshold
    if below:
        if c and value < c:
            return "crit"
        return "warn" if w and value < w else None
    if c and value >= c:
        return "crit"
    return "warn" if w and value >= w else None


def _names(items, n=3):
    return ", ".join(items[:n])


def findings(s, th):
    """Snapshot dict -> [{"id","level","msg","hold_s","section","who"?,"cmd"?}]."""
    out = []

    def add(fid, level, msg, hold_s=0, section="system", **extra):
        if level:
            out.append(dict(id=fid, level=level, msg=msg, hold_s=hold_s, section=section, **extra))

    folders = [f for f in s.get("folders", {}).get("items", []) if f.get("main")]
    biggest = _names(["%s (%s)" % (f["path"], gb(f["b"])) for f in folders])
    writers = sorted((a for a in s.get("agents", []) if (a.get("write_bps") or 0) > 2 * MB),
                     key=lambda a: -a["write_bps"])
    writing = _names([a["label"] for a in writers], 2)

    for d in s.get("disks", []):
        if d.get("is_ram"):
            continue
        mnt = d["mount"]
        if mnt == "/":
            lvl = _two(th["main_disk_free_gb"], d["avail_b"] / GB, below=True)
            msg = "Main disk has %s left (%d%% full)." % (gb(d["avail_b"]), round(d["used_pct"]))
            if biggest:
                msg += " Biggest folders: %s." % biggest
            add("disk_low:/", lvl, msg, 10, "disks")
            if d.get("eta_s"):
                hours = d["eta_s"] / 3600.0
                lvl = _two(th["disk_eta_hours"], hours, below=True)
                msg = "At this pace the main disk is full in about %s" % dur(d["eta_s"])
                if d.get("loss_bps"):
                    msg += " (%s per hour)" % size(d["loss_bps"] * 3600)
                msg += "."
                if writing:
                    msg += " Writing most: %s." % writing
                add("disk_eta:/", lvl, msg, 0, "disks")
            drop = d.get("change_10m_b")
            if drop is not None and drop < 0:
                lvl = _two(th["disk_drop_gb_10min"], -drop / GB)
                msg = "Main disk lost %s in 10 minutes." % gb(-drop)
                if writing:
                    msg += " Writing most: %s." % writing
                add("disk_drop:/", lvl, msg, 0, "disks")
        elif d["total_b"]:
            free_pct = 100.0 * d["avail_b"] / d["total_b"]
            add("disk_low:" + mnt, _two(th["other_disk_free_pct"], free_pct, below=True),
                "%s is almost full (%s left)." % (d["label"], gb(d["avail_b"])), 120, "disks")

    crash = s.get("crash") or {}
    if crash.get("core_b"):
        newest = crash.get("newest_core")
        n = crash.get("cores", 0)
        msg = "Crash dumps are using %s of the main disk" % gb(crash["core_b"])
        if newest:
            msg += " (%d file%s, newest: %s on %s)" % (n, "" if n == 1 else "s", newest["program"],
                                                      time.strftime("%b %d at %H:%M", time.localtime(newest["ts"])))
        add("crash_dumps", _two(th["crash_dumps_gb"], crash["core_b"] / GB),
            msg + ". They are only useful for debugging those crashes.", 0, "disks", cmd=crash.get("cmd"))

    mem = s.get("mem") or {}
    top_mem = _names(["%s (%s)" % (p["name"], gb(p["mem_b"])) for p in s.get("top", {}).get("mem", [])])
    if mem.get("total_b"):
        add("mem_low", _two(th["mem_available_gb"], mem["avail_b"] / GB, below=True),
            "Only %s of memory is still available.%s" % (gb(mem["avail_b"], 1),
                                                         " Biggest users: %s." % top_mem if top_mem else ""), 30, "memory")
        stall = mem.get("user_stall_pct")
        if stall is not None:
            add("mem_pressure", _two(th["mem_pressure_pct"], stall),
                "Programs are stalling because memory is short (%d%% of the time). At 50%% Ubuntu stops the "
                "biggest group of apps to recover." % round(stall), 10, "memory")
        ram_files = mem.get("tmpfs_b") or 0
        who = _names(["%s (%s)" % (x["label"], gb(x["b"])) for x in s.get("scratch", [])[:2]])
        add("ram_files", _two(th["ram_files_gb"], ram_files / GB),
            "Temporary files are occupying %s of memory.%s" % (gb(ram_files), " Biggest: %s." % who if who else ""),
            60, "memory")

    swap = s.get("swap") or {}
    if swap.get("total_b"):
        pct = 100.0 * swap["used_b"] / swap["total_b"]
        add("swap_high", _two(th["swap_used_pct"], pct),
            "Swap (overflow memory on disk) is %d%% full. The next big job may run out of memory." % round(pct),
            60, "memory")
        if swap.get("in_bps", 0) >= 20 * MB:
            add("swap_in", "warn", "The PC is reading memory back from disk (%s per second), so it will feel slow."
                % size(swap["in_bps"]), 60, "memory")

    cpu = s.get("cpu") or {}
    if cpu.get("wait60_pct") is not None:
        top_cpu = _names(["%s (%s cores)" % (p["name"], cores(p["cpu_cores"]))
                          for p in s.get("top", {}).get("cpu", []) if p["cpu_cores"] >= 0.5])
        msg = "Programs are waiting for the processor %d%% of the time: about %d jobs want to run and it can do %d at once." % (
            round(cpu["wait60_pct"]), round(cpu.get("load1", 0)), cpu.get("threads", 1))
        if top_cpu:
            msg += " Using most: %s." % top_cpu
        add("cpu_wait", _two(th["cpu_wait_pct"], cpu["wait60_pct"]), msg, 60, "cpu")
    io = s.get("io") or {}
    if (io.get("stall60_pct") or 0) >= 20:
        add("io_wait", "warn", "Programs are waiting for the disk %d%% of the time." % round(io["stall60_pct"]), 120, "disks")

    temps = {t["id"]: t for t in s.get("temps", [])}
    for tid, key, name in (("cpu", "cpu_temp_c", "Processor"), ("gpu", "gpu_temp_c", "Graphics card"),
                           ("ssd", "ssd_temp_c", "NVMe SSD"), ("coolant", "coolant_temp_c", "Liquid cooler")):
        t = temps.get(tid)
        if t and t.get("c") is not None:
            add("temp:" + tid, _two(th[key], t["c"]), "%s is hot (%d °C)." % (name, round(t["c"])), 120, "temps")
    pump = next((f for f in s.get("fans", []) if f["id"] == "pump"), None)
    if pump and pump["rpm"] < 500 and temps.get("cpu", {}).get("c", 0) > 60:
        add("pump", "crit", "The liquid cooler's pump has stopped (%d rpm)." % pump["rpm"], 30, "temps")

    gpu = s.get("gpu") or {}
    if gpu.get("ok") and gpu.get("vram_total_b"):
        pct = 100.0 * gpu["vram_used_b"] / gpu["vram_total_b"]
        who = _names(["%s %s%s" % (p["name"], gb(p["vram_b"], 1), " (%s)" % p["agent"] if p.get("agent") else "")
                      for p in gpu.get("procs", []) if p["vram_b"] >= 300 * MB])
        add("vram", _two(th["vram_used_pct"], pct),
            "Graphics memory is %d%% full.%s" % (round(pct), " Using it: %s." % who if who else ""), 60, "gpu")
    n_oom = gpu.get("oom_15m") or 0
    if n_oom:
        add("gpu_oom", _two(th["gpu_oom_15min"], n_oom),
            "The graphics card ran out of memory %d time%s in the last 15 minutes." % (n_oom, "" if n_oom == 1 else "s"),
            0, "gpu")
    if gpu.get("failing"):
        add("gpu_unreadable", "warn", "Can't read the graphics card (nvidia-smi is not answering).", 0, "gpu")

    kill = s.get("last_oom_kill")
    if kill and s["ts"] - kill["ts"] < 3600:
        add("oom_kill", "crit", "Linux stopped %s at %s because memory ran out." % (kill["name"] or "a program",
                                                                                      clock(kill["ts"])), 0, "memory")

    for lo in s.get("leftovers", []):
        if lo["level"] not in ("warn", "crit"):
            continue
        origin = lo["from"] or "an agent session"
        if lo["kind"] == "orphan":
            when = " (ended %s ago)" % dur(s["ts"] - lo["from_ended"]) if lo.get("from_ended") else " (ended)"
            lead = "Left over from %s%s" % (origin, when)
        else:
            lead = "%s has been idle for a long time but left this running" % origin
        use = []
        if lo["cpu_avg_cores"] >= 0.1:
            use.append("%s of %d cores" % (cores(lo["cpu_avg_cores"]), s["cpu"]["threads"]))
        use.append(size(lo["mem_b"]) + " of memory")
        add("leftover:" + lo["id"], lo["level"],
            "%s: %s, running for %s, using %s." % (lead, lo["what"].lower(), dur(lo["age_s"]), " and ".join(use)),
            0, "leftovers", cmd=lo["cmd"])

    for a in s.get("agents", []):
        if a["state"] in ("idle", "waiting") and a.get("state_since") and s["ts"] - a["state_since"] >= 1800 \
                and a["cpu_cores"] >= th["idle_agent_cores"][0]:
            doing = _names([d["name"] for d in a.get("doing", [])], 2)
            add("idle_busy:" + a["id"], "warn",
                "%s is %s but its programs still use %s cores%s." % (
                    a["label"], "idle" if a["state"] == "idle" else "waiting for you", cores(a["cpu_cores"]),
                    " (%s)" % doing if doing else ""), 600, "agents")
    return out


class Engine:
    def __init__(self):
        self.state = {}     # id -> {"level", "since", "pending", "pending_since", "last_seen", "f"}
        self.started = None

    def evaluate(self, now, found, events=None):
        """Apply hold/clear timing. Returns active alerts, most severe first."""
        if self.started is None:
            self.started = now
        settling = now - self.started < 25      # just started: show the current picture at once
        seen = set()
        for f in found:
            fid = f["id"]
            seen.add(fid)
            st = self.state.get(fid)
            if st is None:
                st = self.state[fid] = {"level": None, "since": None, "pending": None, "pending_since": now}
            st["last_seen"] = now
            st["f"] = f
            want = f["level"]
            if st["level"] == want:
                st["pending"] = None
                continue
            if st["pending"] != want:
                st["pending"], st["pending_since"] = want, now
            lower = st["level"] is not None and RANK[want] < RANK[st["level"]]
            wait = CLEAR_S if lower else (0 if settling else f["hold_s"])
            if now - st["pending_since"] >= wait:
                if st["level"] is None:
                    st["since"] = now
                st["level"], st["pending"] = want, None
        active = []
        for fid in list(self.state):
            st = self.state[fid]
            if fid not in seen and now - st["last_seen"] >= CLEAR_S:
                if st["level"] is not None and events is not None:
                    events.close(fid, now)
                del self.state[fid]
                continue
            if st["level"] is None:
                continue
            f = st["f"]
            if events is not None:
                events.open(fid, st["level"], f["msg"], st["since"])
            a = {"id": fid, "level": st["level"], "since": int(st["since"]), "msg": f["msg"], "section": f["section"]}
            if f.get("cmd"):
                a["cmd"] = f["cmd"]
            active.append(a)
        active.sort(key=lambda a: (-RANK[a["level"]], a["since"]))
        return active


def headline(active):
    if not active:
        return "ok", "Everything looks healthy"
    level = "crit" if any(a["level"] == "crit" for a in active) else "warn"
    n = len(active)
    return level, "1 thing needs attention" if n == 1 else "%d things need attention" % n
