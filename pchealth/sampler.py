"""The monitor: a sampler thread that only reads /proc, and a worker thread for everything
that can block (nvidia-smi, the journal, drive stats, file reads).

The sampler builds one snapshot per tick and serialises it once; HTTP handlers hand out those
bytes. With a viewer the sampler ticks every 2 s; with nobody watching it drops to 10 s.
"""

import json
import os
import re
import socket
import threading
import time
from collections import deque

from . import (agents_claude, agents_codex, alerts, attribution, config, conversation, dirscan, disks, folders, gpu,
               history, klog, leftovers, procfs, proctable, saved, subagents, sysfs, tmux, tokens, __version__)
from .agents_claude import clean

MB = 1 << 20
GB = 1 << 30
FAST_S = 2.0
IDLE_S = 10.0
VIEW_WINDOW_S = 12.0
EXIT_RUNAWAY = 75
_WHOLE_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|mmcblk\d+)$")
_VIRT_NET = ("docker", "veth", "br-", "virbr", "lxc")
_MODEL_TAIL = re.compile(rb'"model"\s*:\s*"(claude-[A-Za-z0-9._-]{1,40})"')

# (job, seconds between runs with a viewer, without one)
JOBS = (
    ("env", 1, 5), ("sessions", 3, 10), ("gpu", 4, 30), ("gpu_procs", 10, 60), ("disks", 10, 15),
    ("temps", 10, 15), ("mem", 10, 30), ("groups", 10, 30), ("codex", 10, 30), ("tmux", 10, 60),
    ("suspects", 30, 60),
    ("scratch", 60, 60), ("crash", 60, 60), ("klog", 60, 60), ("models", 60, 120), ("tokens", 30, 60),
    ("subagents", 5, 30),
    ("folders", 60, 60),
    ("prune", 120, 120), ("persist", 60, 60),
)


def sd_notify(message):
    """Tell systemd we are ready / still alive. A no-op when not run as a service."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.sendto(message.encode(), addr)
    except OSError:
        pass


class Monitor:
    def __init__(self, cfg, state_dir=config.STATE_DIR, persist=True):
        self.cfg = cfg
        self.th = cfg["thresholds"]
        self.state_dir = state_dir
        self.persist_enabled = persist
        # Only the monitor that runs as the service keeps the saved terminals; a one-off look (--once) writes nothing.
        self.autosave = bool(persist and cfg.get("terminal_autosave", True) and cfg["terminal_type"] != "off")
        self.at_login_flag = state_dir + "/" + os.path.basename(config.AT_LOGIN_FLAG)
        self.at_login_unit = config.AT_LOGIN_UNIT
        self.uid = os.getuid()
        self.host = clean(socket.gethostname(), 40)
        self.table = proctable.Table()
        self.enrich = attribution.Enrich()
        self.attr = attribution.Attribution(self.enrich, self.th, cfg["ignore_leftovers"])
        self.hist = history.History()
        self.events = history.Events()
        self.engine = alerts.Engine()
        self.scanner = folders.Scanner(cfg["project_dirs"], cfg["watch_extra"])
        self.has_tmux = tmux.available()
        self._term_cache = {}                # (socket, pane) -> (monotonic time, response bytes)
        self._term_lock = threading.Lock()
        self._talk_lock = threading.Lock()   # one conversation page is read at a time
        self._emdash_seen = set()            # tmux sessions an Emdash window was once attached to
        self._saved_mark = None              # (mtime, size) of the saved-terminals file as last read
        self.keeper = saved.Keeper()         # what it has seen of the terminals, to keep that file in step
        self._sub_cache = {}                 # sub-agent transcript -> what its end said, until the file changes
        self._rescan_at = 0.0
        self.token_counter = tokens.Counter()
        self._tokens_saved = time.monotonic()
        self._tokens_behind = False          # some transcript is not fully read yet
        self.trends = {}                     # mount -> DiskTrend
        self.eta = {}                        # mount -> (eta_s, loss_bps)
        self.eta_due = 0.0
        self.sensors = sysfs.discover_sensors()
        self.has_gpu = gpu.available()

        self.w = {"gpu": None, "gpu_ts": 0.0, "gpu_fail": 0, "disks": [], "temps": {}, "fans": [],
                  "crash": {}, "scratch_top": [], "mounts": [], "mounts_ts": 0.0}
        self.sources = {}                    # job -> "ok" | short error text
        self.oom = deque(maxlen=600)         # (ts, kind, detail) from the journal
        self.klog_since = time.time() - 24 * 3600
        self.klog_now = False
        self.agent_cgroups = set()
        self._io = {}                        # agent ident -> (ts, total write bytes)
        self._io_hist = {}                   # agent ident -> deque[(ts, bytes written in that step)]
        self._rollouts = {}                  # codex ident -> rollout path
        self._seen_crash = None

        self._prev = {}                      # previous raw counters for rates
        self._prev_t = 0.0
        self._prev_oom_kill = None
        self.seq = 0
        self.snapshot = {}
        self.snapshot_bytes = b"{}"
        self._hist_cache = {}                # range -> (ts, bytes)
        self.views = {}                      # client address -> monotonic time of last poll
        self.wake = threading.Event()
        self.kick = threading.Event()         # sampler -> worker: something new appeared, look now
        self.first_tick = threading.Event()
        self._known_agents = set()
        self.stop = threading.Event()
        self.exit_code = 0
        self.throttled = False
        self._cpu_marks = deque()            # (monotonic, process cpu seconds) for the self-governor
        self.self_cpu_pct = 0.0
        self.errors = 0
        self._hist_saved = time.monotonic()
        self._settle_at = time.monotonic() + 90
        self._watchdog = 0.0
        self._threads = []

    # ------------------------------------------------------------------ lifecycle

    def load_state(self):
        if not self.persist_enabled:
            return
        raw = procfs.read(self.state_dir + "/history.bin", 4 << 20)
        if raw:
            self.hist.load(raw)
        raw = procfs.read(self.state_dir + "/events.json", 1 << 20)
        if raw:
            self.events.load(raw)
        raw = procfs.read(self.state_dir + "/registry.json", 2 << 20)
        if raw:
            try:
                reg = json.loads(raw)
                self.attr.load_registry(reg.get("agents", {}), time.time())
                self.scanner.load(reg.get("folders", {}))
                self._seen_crash = set(reg.get("crashes", [])) or None
            except (ValueError, AttributeError):
                pass
        raw = procfs.read(self.state_dir + "/tokens.json", 4 << 20)
        if raw:
            try:
                self.token_counter.load(json.loads(raw))
            except ValueError:
                pass

    def save_state(self, force=False):
        if not self.persist_enabled:
            return
        ok = True
        if force or self.events.dirty:
            ok &= history.atomic_write(self.state_dir + "/events.json", self.events.dump())
            self.events.dirty = False
        if force or self.attr.dirty:
            reg = {"agents": self.attr.dump_registry(), "folders": self.scanner.dump(),
                   "crashes": sorted(self._seen_crash or ())[-200:]}
            ok &= history.atomic_write(self.state_dir + "/registry.json", json.dumps(reg).encode())
            self.attr.dirty = False
        if self.token_counter.dirty and (force or time.monotonic() - self._tokens_saved >= 600):
            # Progress through the transcripts, so a restart does not read them all again.
            ok &= history.atomic_write(self.state_dir + "/tokens.json", json.dumps(self.token_counter.dump()).encode())
            self.token_counter.dirty = False
            self._tokens_saved = time.monotonic()
        if force or time.monotonic() - self._hist_saved >= 900:
            ok &= history.atomic_write(self.state_dir + "/history.bin", self.hist.dump())
            self._hist_saved = time.monotonic()
        self.sources["saving"] = "ok" if ok else "cannot write state (disk full?)"

    def start(self):
        self.load_state()
        for name, fn in (("sampler", self._sampler_loop), ("worker", self._worker_loop)):
            t = threading.Thread(target=fn, name="pch-" + name, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        self.kick.set()
        for t in self._threads:
            t.join(timeout=3)
        if self.scanner.proc is not None:
            self.scanner.proc.terminate()
        self.save_state(force=True)

    # ------------------------------------------------------------------ viewers

    def viewed(self, client):
        """Called by the HTTP server on each snapshot request."""
        now = time.monotonic()
        idle = not self.has_viewer(now)
        self.views[client] = now
        if idle:
            self.wake.set()              # someone just opened the page: sample now, not in 10 s
            self.kick.set()              # ...and refresh agent states and tmux sessions right away

    def has_viewer(self, now=None):
        now = time.monotonic() if now is None else now
        # list(): HTTP threads add entries while this runs
        return any(now - t < VIEW_WINDOW_S for t in list(self.views.values()))

    def viewer_count(self):
        now = time.monotonic()
        views = list(self.views.items())
        for k, t in views:
            if now - t >= 60:
                self.views.pop(k, None)
        return sum(1 for _, t in views if now - t < VIEW_WINDOW_S)

    # ------------------------------------------------------------------ sampler thread

    def _sampler_loop(self):
        sd_notify("READY=1")
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as e:        # a monitor must outlive a surprise in one sample
                self.errors += 1
                self.sources["sampler"] = "error: " + type(e).__name__
            else:
                self.sources["sampler"] = "ok"
            self.first_tick.set()
            self._govern(t0)
            if t0 - self._watchdog >= 30:
                sd_notify("WATCHDOG=1")
                self._watchdog = t0
            fast = self.has_viewer() and not self.throttled
            interval = FAST_S if fast else IDLE_S
            self.wake.wait(timeout=max(0.2, interval - (time.monotonic() - t0)))
            self.wake.clear()

    def _govern(self, now):
        """Keep the monitor honest about its own cost: slow down above 5% of a core, quit above 25%."""
        marks = self._cpu_marks
        marks.append((now, time.process_time()))
        while len(marks) > 2 and now - marks[0][0] > 300:
            marks.popleft()
        span = now - marks[0][0]
        if span < 60:
            return
        pct = 100.0 * (marks[-1][1] - marks[0][1]) / span
        self.self_cpu_pct = pct
        if span >= 240 and pct > 25:
            self.exit_code = EXIT_RUNAWAY
            self.stop.set()
        self.throttled = pct > 5 or (self.throttled and pct > 3)

    def tick(self):
        now = time.time()
        mono = time.monotonic()
        dt = mono - self._prev_t if self._prev_t else 0.0
        self._prev_t = mono
        prev = self._prev
        cur = {}

        # ---- CPU
        cpus, _ = procfs.parse_cpu_stat(procfs.read("/proc/stat", 1 << 15) or b"")
        cur["cpu"] = cpus
        busy_pct = None
        per_core = []
        pc = prev.get("cpu")
        if pc and "cpu" in cpus and "cpu" in pc:
            d_total = cpus["cpu"][1] - pc["cpu"][1]
            busy_pct = 100.0 * (cpus["cpu"][0] - pc["cpu"][0]) / d_total if d_total > 0 else 0.0
            for i in range(procfs.NCPU):
                a, b = cpus.get("cpu%d" % i), pc.get("cpu%d" % i)
                if a and b and a[1] > b[1]:
                    per_core.append(round(100.0 * (a[0] - b[0]) / (a[1] - b[1])))
                else:
                    per_core.append(0)
        load = (procfs.read1("/proc/loadavg") or b"0 0 0").split()
        psi_cpu = procfs.parse_pressure(procfs.read1("/proc/pressure/cpu") or b"").get("some", {})
        psi_mem = procfs.parse_pressure(procfs.read1("/proc/pressure/memory") or b"").get("some", {})
        psi_io = procfs.parse_pressure(procfs.read1("/proc/pressure/io") or b"").get("full", {})

        # ---- memory and swap
        mi = procfs.parse_kv_kb(procfs.read("/proc/meminfo", 1 << 14) or b"")
        vm = procfs.parse_counters(procfs.read("/proc/vmstat", 1 << 15) or b"", ("pswpin", "pswpout", "oom_kill"))
        cur["vm"] = vm
        pv = prev.get("vm") or {}

        def rate(key, scale):
            if dt <= 0 or key not in vm or key not in pv:
                return 0
            return max(0, int((vm[key] - pv[key]) * scale / dt))
        total_b = mi.get("MemTotal", 0) * 1024
        avail_b = mi.get("MemAvailable", 0) * 1024
        swap_total = mi.get("SwapTotal", 0) * 1024
        swap_used = swap_total - mi.get("SwapFree", 0) * 1024
        kills = vm.get("oom_kill")
        if kills is not None and self._prev_oom_kill is not None and kills > self._prev_oom_kill:
            self.oom.append((now, "oom_kill", ""))
            self.klog_now = True          # ask the worker for the name of what was killed
        self._prev_oom_kill = kills

        # ---- disk and network rates
        ds = procfs.parse_diskstats(procfs.read("/proc/diskstats", 1 << 16) or b"")
        nd = procfs.parse_netdev(procfs.read("/proc/net/dev", 1 << 14) or b"")
        cur["ds"], cur["nd"] = ds, nd
        pds, pnd = prev.get("ds") or {}, prev.get("nd") or {}
        dev_write = {}
        read_bps = write_bps = rx_bps = tx_bps = 0
        if dt > 0:
            for dev, (r, wr) in ds.items():
                p = pds.get(dev)
                if not p:
                    continue
                wbps = max(0, int((wr - p[1]) / dt))
                dev_write[dev] = wbps
                if _WHOLE_DISK.match(dev):
                    read_bps += max(0, int((r - p[0]) / dt))
                    write_bps += wbps
            for iface, (rx, tx) in nd.items():
                p = pnd.get(iface)
                if p and not iface.startswith(_VIRT_NET):
                    rx_bps += max(0, int((rx - p[0]) / dt))
                    tx_bps += max(0, int((tx - p[1]) / dt))
        self._prev = cur

        # ---- processes and agents
        self.table.update()
        self.attr.update(self.table, now)
        roots = set(self.attr.pub["roots"])
        if roots - self._known_agents:
            self.kick.set()               # a new agent: fetch its name and state now, not in 10 s
        self._known_agents = roots
        agents = self._with_extras(self.attr.export_agents(now))
        left = self.attr.export_leftovers(self.table, now)

        w = self.w
        g = w["gpu"]
        gpu_part = {"ok": False}
        if self.has_gpu:
            cutoff = now - 900
            n_oom = sum(1 for ts, kind, _ in self.oom if kind == "gpu_oom" and ts >= cutoff)
            last_oom = max((ts for ts, kind, _ in self.oom if kind == "gpu_oom"), default=None)
            gp = []
            for pid, (name, vram) in sorted(self.enrich.gpu_procs.items(), key=lambda kv: -kv[1][1])[:6]:
                gp.append({"pid": pid, "name": clean(name, 24), "vram_b": vram, "agent": self.attr.label_of(pid)})
            gpu_part = {"ok": g is not None, "failing": w["gpu_fail"] >= 3, "age_s": round(now - w["gpu_ts"], 1),
                        "procs": gp, "oom_15m": n_oom, "last_oom_ts": int(last_oom) if last_oom else None}
            if g:
                gpu_part.update(g)

        temps = [{"id": tid, "label": label, "c": c} for tid, (label, c) in w["temps"].items()]
        if g and g.get("temp_c") is not None:
            temps.append({"id": "gpu", "label": "Graphics card", "c": g["temp_c"]})
        limits = {"cpu": "cpu_temp_c", "gpu": "gpu_temp_c", "ssd": "ssd_temp_c", "coolant": "coolant_temp_c"}
        for t in temps:
            lim = self.th.get(limits.get(t["id"], ""))
            if lim:
                t["warn_c"], t["crit_c"] = lim
        cpu_temp = w["temps"].get("cpu", (None, None))[1]

        # ---- drives
        hist = self.hist
        ram_files_b = 0
        disk_rows = []
        for d in w["disks"]:
            row = dict(d)
            dev = row.pop("dev", "")
            if d["is_ram"]:
                ram_files_b += d["used_b"]
            else:
                name = "disk_avail_mb:" + d["mount"]
                hist.push(now, name, d["avail_b"] / MB)
                row["change_10m_b"] = disks.change_over(hist.ring(name, "1h"), now, 600, d["avail_b"])
                row["change_1h_b"] = disks.change_over(hist.ring(name, "24h"), now, 3600, d["avail_b"])
                row["change_24h_b"] = disks.change_over(hist.ring(name, "7d"), now, 86400, d["avail_b"])
                row["write_bps"] = dev_write.get(os.path.basename(dev), 0)
                eta = self.eta.get(d["mount"])
                row["eta_s"], row["loss_bps"] = eta if eta else (None, None)
            disk_rows.append(row)
        if now >= self.eta_due and disk_rows:
            self._update_eta(now, disk_rows)

        # ---- top programs, grouped by name
        by = {}
        for p in self.table.procs.values():
            e = by.get(p.comm)
            if e is None:
                by[p.comm] = [1, p.d_own, p.rss, p.pid]
            else:
                e[0] += 1
                e[1] += p.d_own
                e[2] += p.rss

        def top(index, floor):
            rows = sorted(by.items(), key=lambda kv: -kv[1][index])[:8]
            return [{"name": clean(n, 24), "n": v[0], "cpu_cores": round(self.table.cores(v[1]), 2),
                     "mem_b": v[2] * procfs.PAGE, "agent": self.attr.label_of(v[3]) if v[0] == 1 else None}
                    for n, v in rows if v[index] > floor]

        kill = next(((ts, d) for ts, kind, d in reversed(self.oom) if kind == "oom_kill"), None)
        rss_self = int((procfs.read1("/proc/self/statm") or b"0 0").split()[1]) * procfs.PAGE
        waiting = sum(1 for a in agents if a["state"] == "waiting")
        working = sum(1 for a in agents if a["state"] == "working")

        s = {
            "v": 1, "seq": self.seq + 1, "ts": int(now), "host": self.host,
            "interval_s": FAST_S if self.has_viewer() and not self.throttled else IDLE_S,
            "uptime_s": int(self.table.boot_s),
            "cpu": {"busy_pct": None if busy_pct is None else round(busy_pct, 1), "threads": procfs.NCPU,
                    "per_core_pct": per_core, "load1": float(load[0]) if load else 0.0,
                    "wait_pct": psi_cpu.get("avg10"), "wait60_pct": psi_cpu.get("avg60"), "temp_c": cpu_temp},
            "mem": {"total_b": total_b, "avail_b": avail_b, "used_b": total_b - avail_b, "tmpfs_b": ram_files_b,
                    "cache_b": max(0, (mi.get("Cached", 0) + mi.get("SReclaimable", 0) - mi.get("Shmem", 0)) * 1024),
                    "stall_pct": psi_mem.get("avg10"), "user_stall_pct": w.get("user_stall")},
            "swap": {"total_b": swap_total, "used_b": swap_used,
                     "in_bps": rate("pswpin", procfs.PAGE), "out_bps": rate("pswpout", procfs.PAGE)},
            "io": {"read_bps": read_bps, "write_bps": write_bps, "stall_pct": psi_io.get("avg10"),
                   "stall60_pct": psi_io.get("avg60")},
            "net": {"rx_bps": rx_bps, "tx_bps": tx_bps},
            "gpu": gpu_part, "disks": disk_rows, "temps": temps,
            "fans": [{"id": fid, "label": label, "rpm": rpm} for fid, label, rpm in w["fans"]],
            "agents": agents, "agent_counts": {"total": len(agents), "working": working, "waiting": waiting},
            "ended": self.attr.export_ended(now), "leftovers": left,
            "top": {"cpu": top(1, 0), "mem": top(2, 25 * MB // procfs.PAGE)},
            "scratch": w["scratch_top"], "crash": w["crash"], "folders": self.scanner.export(now),
            "terminals": {"view": self.cfg["terminal_view"] if self.has_tmux else "off",
                          "type": self.cfg["terminal_type"] if self.has_tmux else "off",
                          "live": bool(w.get("ttyd")), "code": bool(self.cfg["access_code"]),
                          "sessions": self._export_sessions(), "saved": self._export_saved(),
                          "auto": self.autosave, "at_login": w.get("at_login")},
            "last_oom_kill": {"ts": int(kill[0]), "name": kill[1]} if kill else None,
        }
        active = self.engine.evaluate(now, alerts.findings(s, self.th), self.events)
        if self._settle_at and mono >= self._settle_at:
            self.events.settle(now)
            self._settle_at = 0
        level, text = alerts.headline(active)
        s["health"] = {"level": level, "headline": text, "alerts": active}
        s["events"] = self.events.recent(now)
        s["self"] = {"version": __version__, "rss_b": rss_self, "cpu_pct": round(self.self_cpu_pct, 2),
                     "viewers": self.viewer_count(), "throttled": self.throttled, "errors": self.errors,
                     "sources": dict(self.sources), "config_error": self.cfg.get("config_error")}

        # ---- trends
        hist.push(now, "cpu_busy_pct", busy_pct)
        hist.push(now, "cpu_wait_pct", psi_cpu.get("avg10"))
        hist.push(now, "mem_used_mb", (total_b - avail_b) / MB)
        hist.push(now, "ram_files_mb", ram_files_b / MB)
        hist.push(now, "swap_used_mb", swap_used / MB)
        if g:
            hist.push(now, "gpu_util_pct", g.get("util_pct"))
            hist.push(now, "vram_used_mb", g["vram_used_b"] / MB)
            hist.push(now, "gpu_temp_c", g.get("temp_c"))
        hist.push(now, "cpu_temp_c", cpu_temp)
        for a in agents:
            hist.push_agent(now, a["id"], a["cpu_cores"], a["mem_b"] / MB if a["mem_b"] is not None else None)

        self.snapshot = s
        self.snapshot_bytes = json.dumps(s, separators=(",", ":")).encode()
        self.seq += 1

    def _update_eta(self, now, disk_rows):
        self.eta_due = now + 300
        for d in disk_rows:
            if d["is_ram"]:
                continue
            mnt = d["mount"]
            name = "disk_avail_mb:" + mnt
            troughs = disks.troughs_from_ring(self.hist.ring(name, "7d"), now)
            recent = self.hist.ring(name, "24h")
            floor = d["avail_b"]
            if recent is not None:
                for k in range(0, 31):
                    v = recent.value_at(now - 60 * k)
                    if v is not None and v * MB < floor:
                        floor = v * MB
            trend = self.trends.setdefault(mnt, disks.DiskTrend())
            warn_h, crit_h = self.th["disk_eta_hours"]
            self.eta[mnt] = trend.update(troughs, d["total_b"], floor, crit_h=crit_h, warn_h=warn_h)

    def history_bytes(self, rng):
        """Serialised history for one range, cached for one step."""
        step = history.RANGES[rng][0]
        now = time.time()
        cached = self._hist_cache.get(rng)
        if cached and now - cached[0] < min(step, 60):
            return cached[1]
        data = json.dumps(self.hist.export(rng, now), separators=(",", ":")).encode()
        self._hist_cache[rng] = (now, data)
        return data

    # ------------------------------------------------------------------ worker thread

    def _worker_loop(self):
        due = {name: 0.0 for name, _, _ in JOBS}
        self.first_tick.wait(15)          # the first sample tells us which processes to look at
        kicked = True
        while not self.stop.is_set():
            mono = time.monotonic()
            fast = self.has_viewer(mono) and not self.throttled
            if self.kick.is_set():
                self.kick.clear()
                kicked = True
                due["env"] = due["sessions"] = due["mem"] = due["tmux"] = due["subagents"] = 0.0
            if self.klog_now:
                due["klog"] = 0.0
                self.klog_now = False
            for name, with_viewer, without in JOBS:
                if mono < due[name] or self.stop.is_set():
                    continue
                due[name] = mono + (with_viewer if fast else without)
                try:
                    getattr(self, "_job_" + name)(time.time())
                    self.sources.setdefault(name, "ok")
                    if self.sources.get(name, "").startswith("error"):
                        self.sources[name] = "ok"
                except Exception as e:    # one failing source must not stop the others
                    self.sources[name] = "error: " + type(e).__name__
            if self._tokens_behind:
                due["tokens"] = min(due["tokens"], mono + 2)    # catching up on big transcripts: keep going
            if kicked:
                kicked = False
                self.wake.set()           # let the next sample include what was just learned
            self.kick.wait(1.0)

    def _job_env(self, now):
        e = self.enrich
        pub = self.attr.pub
        for ident in list(pub["roots"]) + pub["env_wanted"]:
            if ident in e.env:
                continue
            pid = ident[0]
            if procfs.pid_uid(pid) != self.uid:
                e.env[ident] = {}
                continue
            env = procfs.pid_environ_whitelist(pid, agents_claude.ENV_KEYS)
            st = procfs.pid_stat(pid)
            if st is not None and st[6] == ident[1]:     # still the same process
                e.env[ident] = env

    def _job_sessions(self, now):
        e = self.enrich
        for ident, kind in self.attr.pub["roots"].items():
            pid = ident[0]
            if kind == "claude":
                e.sess[ident] = agents_claude.read_session(pid, ident[1])
            info = e.root.get(ident)
            if info is None or now - info["ts"] > 60:
                argv = procfs.pid_cmdline(pid)
                new = {"cwd": procfs.readlink("/proc/%d/cwd" % pid) or "", "ts": now,
                       "model": agents_claude.model_from_args(argv) if kind == "claude" else None,
                       "mode": agents_codex.mode_from_args(argv) if kind == "codex" else None}
                if info and not new["model"]:
                    new["model"] = info.get("model")
                e.root[ident] = new
                cg = procfs.read1("/proc/%d/cgroup" % pid, 1024)
                if cg:
                    if len(self.agent_cgroups) > 400:
                        self.agent_cgroups.clear()       # terminal tabs come and go; do not keep them forever
                    self.agent_cgroups.add(cg.strip())

    def _job_models(self, now):
        """Model name from the tail of the transcript, for sessions started without --model."""
        e = self.enrich
        for ident, kind in self.attr.pub["roots"].items():
            info, sess = e.root.get(ident), e.sess.get(ident)
            if kind != "claude" or not info or info.get("model") or not sess:
                continue
            path = tokens.transcript_path(sess.get("cwd"), sess.get("sessionId"))
            got = procfs.read_tail(path, 1 << 16) if path else None
            if got:
                found = _MODEL_TAIL.findall(got[0])
                if found:
                    e.root[ident] = dict(info, model=found[-1].decode())

    def _job_tmux(self, now):
        """The tmux sessions that exist, and which of them an agent lives in."""
        e = self.enrich
        found = {}
        sessions = {}
        own = None                           # the panes of the tmux server the terminal service uses
        if self.has_tmux and (self.cfg["terminal_view"] != "off" or self.cfg["terminal_type"] != "off"):
            by_socket = {}
            default = tmux.socket_from_env("/tmp/tmux-%d/default" % self.uid, self.uid)
            if default:
                by_socket[default] = []
            for ident in self.attr.pub["roots"]:
                env = e.env.get(ident) or {}
                pane = env.get("TMUX_PANE")
                sock = tmux.socket_from_env(env.get("TMUX"), self.uid) if pane else None
                if sock:
                    by_socket.setdefault(sock, []).append((ident, pane))
            for sock, agents in by_socket.items():
                panes = tmux.list_panes(sock)
                if sock == default:
                    own = panes
                if not panes:
                    continue
                for ident, pane in agents:
                    p = panes.get(pane)
                    proc = self.table.procs.get(ident[0])
                    # The pane must be this agent's own terminal, not merely one it was started from.
                    if p and proc and proc.start == ident[1] and tmux.same_tty(p["tty"], proc.tty):
                        found[ident] = dict(p, socket=sock, pane=pane)
                clients = tmux.list_clients(sock)
                for name, info in tmux.sessions_from(panes).items():
                    if name not in sessions:
                        sessions[name] = dict(info, socket=sock, emdash=self._is_emdash(name, clients.get(name, ())))
            self._emdash_seen &= set(sessions)
        e.tmux = found
        e.sessions = sessions
        self.w["ttyd"] = self._socket_alive(self.cfg["terminal_socket"])
        # None: this PC has no unit that brings the terminals back at login. Else: whether that is switched on.
        self.w["at_login"] = os.path.exists(self.at_login_flag) if os.path.exists(self.at_login_unit) else None
        self._read_saved()
        if self.autosave:
            self._keep_saved(own)

    def _keep_saved(self, panes):
        """Keep the saved terminals in step with the ones running now. saved.Keeper holds the rules; this only
        hands it what tmux says and writes the file when the answer differs from what is in it."""
        kept = (self.enrich.saved or {}).get("rows", [])
        running, ids, server = [], {}, None
        if panes:
            server = next(iter(panes.values()))["server"]
            ids = {p["session"]: p["id"] for p in panes.values()}
            running = saved.rows_for(panes, saved.find_conversations(panes, self.table.procs, self.uid),
                                     os.path.expanduser("~"))
        rows = self.keeper.rows(kept, running, ids, server, time.monotonic())
        if rows and rows != kept and history.atomic_write(self.state_dir + "/" + saved.FILE_NAME,
                                                          saved.dump(rows, time.time())):
            self._read_saved()

    def _read_saved(self):
        """The terminals the terminal service last saved (saved.py). Read again only when the file changes."""
        path = self.state_dir + "/" + saved.FILE_NAME
        try:
            st = os.lstat(path)
            mark = (st.st_mtime_ns, st.st_size)
        except OSError:
            mark = None
        if mark != self._saved_mark:
            self._saved_mark = mark
            self.enrich.saved = saved.load(path) if mark else None

    def _is_emdash(self, name, client_pids):
        """Was this tmux session started by the Emdash app? It finds its sessions by name, so they are not to be
        renamed. Told by the name Emdash gives them, or by an Emdash window being (or having been) attached."""
        if tmux.EMDASH_NAME.search(name) or name in self._emdash_seen:
            return True
        procs = self.table.procs
        for pid in client_pids:
            for _ in range(8):                       # the client, its shell, the app that owns the shell...
                proc = procs.get(pid)
                if proc is None or proc.ppid <= 1:
                    break
                pid = proc.ppid
                parent = procs.get(pid)
                if parent is not None and parent.comm.lower().startswith("emdash"):
                    self._emdash_seen.add(name)
                    return True
        return False

    def rescan(self):
        """The page has just started, renamed or closed a terminal: look at the tmux sessions again now."""
        now = time.monotonic()
        if now - self._rescan_at >= 0.5:
            self._rescan_at = now
            self.kick.set()
            self.wake.set()

    @staticmethod
    def _socket_alive(path):
        import stat as _stat
        try:
            return _stat.S_ISSOCK(os.stat(path).st_mode)
        except OSError:
            return False

    def _export_sessions(self):
        """tmux sessions for the page, each with the agent that runs in it (if any)."""
        e = self.enrich
        by_session = {}
        for ident, pane in e.tmux.items():
            ag = self.attr.agents.get(ident)
            if ag is None or ag.hidden:
                continue
            name = pane["session"]
            # With more than one agent in a session, the one in the pane you land on when joining it speaks for
            # the session. It is also the one whose conversation gets saved (saved.py), so the two agree.
            if name not in by_session or pane.get("pane") == (e.sessions.get(name) or {}).get("pane"):
                by_session[name] = ag
        kept = {r["name"]: r for r in (e.saved or {}).get("rows", ())}
        kept_talks = {r["sid"] for r in kept.values() if r["sid"]}
        out = []
        for name, info in e.sessions.items():
            ag = by_session.get(name)
            out.append({"name": clean(name, 64), "openable": bool(tmux.SESSION_NAME.match(name)),
                        "attached": info["attached"], "windows": info["windows"], "w": info["w"], "h": info["h"],
                        "cmd": clean(info["cmd"], 24), "agent": ag.id if ag else None, "emdash": bool(info.get("emdash")),
                        "saved": self._saved_verdict(name, ag.sid if ag and ag.kind == "claude" else None, kept, kept_talks,
                                                     ag is not None),
                        # Same order as the agents table; sessions without an agent come last, by name.
                        "_key": attribution.order_key(ag.state, ag.state_since, name) if ag else (10, 0, 0, name.lower())})
        out.sort(key=lambda x: x.pop("_key"))
        return out

    @staticmethod
    def _saved_verdict(name, talk, kept, kept_talks, has_agent=False):
        """Is this terminal among the saved ones? Only the verdict goes to the page, never a conversation's id.
        `talk` is the conversation running here, if it is one that can be saved and resumed.
        "yes": what was saved is what is here (a saved conversation is followed even if its terminal was renamed);
        "other": the name was saved, but with something else than the conversation here now (saving again fixes it);
        "stopped": the name was saved with a conversation that is not running here any more;
        "shell": only the terminal is saved, and the agent in it is one that cannot be (Codex, or a Claude whose
                 conversation is not known yet): it would come back as an empty terminal;
        None: not saved."""
        if talk and talk in kept_talks:
            return "yes"
        row = kept.get(name)
        if row is None:
            return None
        if row["sid"]:
            return "other" if talk else "stopped"
        if talk:
            return "other"
        return "shell" if has_agent else "yes"

    def _export_saved(self):
        """What is saved, for the page: when, how many, and which of them are not running (what a restore would
        start). None when nothing is saved."""
        kept = self.enrich.saved              # read once: the worker thread may replace it at any moment
        if not kept or not kept["rows"]:
            return None
        live = {ag.sid for ag in self.attr.agents.values() if ag.sid}
        sessions = self.enrich.sessions
        gone = [r["name"] for r in kept["rows"] if (r["sid"] not in live if r["sid"] else True) and r["name"] not in sessions]
        return {"ts": kept["ts"], "names": [r["name"] for r in kept["rows"]], "gone": gone}

    def terminal(self, session):
        """JSON bytes with the current screen of a tmux session, or None. Called by HTTP threads.

        The caller supplies only a session name; the pane and socket come from our own table.
        """
        target = self.enrich.sessions.get(session)
        if not target:
            return None
        key = (target["socket"], target["pane"])
        with self._term_lock:                 # one capture at a time, shared by everyone watching
            now = time.monotonic()
            hit = self._term_cache.get(key)
            if hit and now - hit[0] < 0.4:
                return hit[1]
            shot = tmux.capture(*key)
            if shot is None:
                return None
            shot.update(session=clean(session, 64), ts=int(time.time()))
            body = json.dumps(shot, separators=(",", ":")).encode()
            if len(self._term_cache) > 32:
                self._term_cache.clear()
            self._term_cache[key] = (now, body)
            return body

    def conversation(self, session, before=None, after=None):
        """JSON bytes with a page of the conversation held in a tmux session, or None. Called by HTTP threads.

        As with terminal(), the caller supplies only a session name (and where in the record to read):
        which agent lives in that session, and which file is its record, come from our own tables.
        """
        e = self.enrich
        sess = None
        for ident, pane in list(e.tmux.items()):
            ag = self.attr.agents.get(ident)
            if pane.get("session") == session and ag is not None and not ag.hidden and ag.kind == "claude":
                sess = e.sess.get(ident)
                break
        if not sess or not sess.get("sessionId"):
            return None
        sid = str(sess["sessionId"])
        path = tokens.transcript_path(sess.get("cwd") or "", sid)
        if path is None:
            return None
        with self._talk_lock:
            got = conversation.page(path, before, after)
        if got is None:
            return None
        got.update(session=clean(session, 64), sid=sid[:8])
        return json.dumps(got, separators=(",", ":")).encode()

    def _with_extras(self, agents):
        """Add to each exported agent what is known about its conversation: whether it is among the saved
        terminals, and the sub-agents it is running now."""
        kept_talks = {r["sid"] for r in (self.enrich.saved or {}).get("rows", ()) if r["sid"]}
        running = self.enrich.subagents
        talk_of = {ag.id: ag.sid for ag in self.attr.agents.values()}
        for a in agents:
            talk = talk_of.get(a["id"])
            a["saved"] = bool(talk) and talk in kept_talks
            a["subagents"] = running.get(talk, []) if talk else []
        return agents

    def _job_subagents(self, now):
        """The sub-agents each Claude agent is running right now (subagents.py). They run inside the agent's own
        process, so they are read from the records Claude Code keeps next to the conversation's transcript."""
        e = self.enrich
        found = {}
        for ident, kind in self.attr.pub["roots"].items():
            sess = e.sess.get(ident)
            if kind != "claude" or not sess or not sess.get("sessionId"):
                continue
            sid = str(sess["sessionId"])
            counted = self.token_counter.sessions.get(sid)        # it already knows where this transcript is
            path = counted.path if counted else tokens.transcript_path(sess.get("cwd") or "", sid)
            if not path:
                continue
            ag = self.attr.agents.get(ident)
            subs = subagents.running(path, ag.started if ag else 0, now, self._sub_cache)[:subagents.MAX_SHOWN]
            if subs:
                found[sid] = subs
        e.subagents = found

    def _job_tokens(self, now):
        """Add up token use from the transcripts of the Claude sessions that are running."""
        e = self.enrich
        live = {}
        for ident, kind in self.attr.pub["roots"].items():
            sess = e.sess.get(ident)
            if kind == "claude" and sess and sess.get("sessionId"):
                live[str(sess["sessionId"])] = sess.get("cwd") or ""
        e.tokens = self.token_counter.update(live, now)
        self._tokens_behind = len(e.tokens) < len(self.token_counter.sessions.keys() & live.keys())

    def _job_codex(self, now):
        e = self.enrich
        boot_epoch = now - procfs.boot_seconds()
        for ident, kind in self.attr.pub["roots"].items():
            if kind != "codex":
                continue
            info = e.root.get(ident)
            if not info:
                continue                      # not looked at yet; the sessions job does that first
            daemon = info.get("mode") in ("daemon", "other")
            path = None if daemon else self._rollouts.get(ident)
            if not path:
                # A daemon only counts while it holds a session file open; never guess one by folder.
                path = agents_codex.open_rollout(ident[0]) if daemon else agents_codex.find_rollout(
                    ident[0], info.get("cwd", ""), boot_epoch + ident[1] / procfs.CLK_TCK)
                if path and not daemon:
                    self._rollouts[ident] = path
            ag = self.attr.agents.get(ident)
            bg = ag.bg_active if ag else False
            if path:
                st = agents_codex.rollout_state(path, bg, now)
                st["title"] = agents_codex.thread_title(path)
            else:
                st = {"state": "working" if bg else "unknown", "text": "Working" if bg else "Running",
                      "model": None, "since": None, "title": ""}
            st["has_rollout"] = bool(path)
            e.codex[ident] = st
        self.sources["codex"] = "ok" if any(k == "codex" for k in self.attr.pub["roots"].values()) else "none running"

    def _sum_mem(self, pids, cap=400):
        anon = swap = written = 0
        for pid in pids[:cap]:
            st = procfs.pid_status(pid)
            if st:
                anon += st.get("RssAnon", 0)
                swap += st.get("VmSwap", 0)
            raw = procfs.read1("/proc/%d/io" % pid, 512)
            if raw:
                written += procfs.parse_io_write_bytes(raw) or 0
        return anon * 1024, swap * 1024, written

    def _job_mem(self, now):
        e = self.enrich
        for ident, pids in self.attr.pub["members"].items():
            mem_b, swap_b, written = self._sum_mem(pids)
            prev = self._io.get(ident)
            self._io[ident] = (now, written)
            bps = None
            steps = self._io_hist.setdefault(ident, deque())
            if prev and now > prev[0]:
                step = max(0, written - prev[1])      # exits outside the tree can make the total dip
                bps = int(step / (now - prev[0]))
                steps.append((now, step))
            while steps and now - steps[0][0] > 3600:
                steps.popleft()
            e.mem[ident] = {"mem_b": mem_b, "swap_b": swap_b, "write_bps": bps,
                            "written_1h_b": sum(b for _, b in steps)}
        self.w["user_stall"] = sysfs.user_memory_pressure(self.uid)

    def _job_groups(self, now):
        e = self.enrich
        listening = None
        for gi, pids in self.attr.pub["groups"].items():
            d = dict(e.group.get(gi) or {})
            if "what" not in d:
                st = procfs.pid_stat(gi[0])
                if st is None or st[6] != gi[1]:
                    continue
                d["what"] = clean(leftovers.what(st[0], procfs.pid_cmdline(gi[0])), 40)
            if listening is None:
                listening = procfs.listen_ports()
            d["ports"] = leftovers.ports_of(pids, listening)
            d["mem_b"], d["swap_b"], _ = self._sum_mem(pids, cap=64)
            e.group[gi] = d

    def _job_suspects(self, now):
        e = self.enrich
        for ident in self.attr.pub["suspect_wanted"]:
            pid = ident[0]
            st = procfs.pid_stat(pid)
            if st is None or st[6] != ident[1]:
                continue
            label = leftovers.what(st[0], procfs.pid_cmdline(pid))
            cg = procfs.read1("/proc/%d/cgroup" % pid, 1024)
            ok = leftovers.is_suspect_shape(label) and cg is not None and cg.strip() in self.agent_cgroups
            e.suspect[ident] = label if ok else None

    def _job_gpu(self, now):
        if not self.has_gpu:
            self.sources["gpu"] = "no NVIDIA GPU found"
            return
        g = gpu.query_gpu()
        if g is None:
            self.w["gpu_fail"] += 1
            if self.w["gpu_fail"] >= 3:
                self.w["gpu"] = None
            self.sources["gpu"] = "nvidia-smi did not answer"
        else:
            self.w["gpu"], self.w["gpu_ts"], self.w["gpu_fail"] = g, now, 0
            self.sources["gpu"] = "ok"

    def _job_gpu_procs(self, now):
        if self.has_gpu:
            procs = gpu.query_procs()
            if procs is not None:
                self.enrich.gpu_procs = procs

    def _job_disks(self, now):
        w = self.w
        if now - w["mounts_ts"] > 60 or not w["mounts"]:
            w["mounts"] = sysfs.parse_mounts(procfs.read("/proc/self/mounts", 1 << 16) or b"")
            w["mounts_ts"] = now
        rows = []
        for dev, mnt, fs in w["mounts"]:
            st = sysfs.statvfs(mnt)
            if st is None or not st["total_b"]:
                continue
            is_ram = fs == "tmpfs"
            rows.append({"mount": mnt, "label": clean(sysfs.mount_label(mnt, dev, fs), 40), "dev": dev, "fs": fs,
                         "is_ram": is_ram, "total_b": st["total_b"], "avail_b": st["avail_b"],
                         "used_b": st["total_b"] - st["avail_b"] if is_ram else None,
                         "used_pct": st["used_pct"], "inodes_free_pct": st["inodes_free_pct"]})
        rows.sort(key=lambda r: (r["is_ram"], r["mount"] != "/", r["mount"]))
        w["disks"] = rows

    def _job_temps(self, now):
        self.w["temps"], self.w["fans"] = sysfs.read_sensors(self.sensors)

    def _job_scratch(self, now):
        """Size the agents' temp folders. They live in /tmp, which on this PC is memory."""
        base = "/tmp/claude-%d" % self.uid
        sizes = {}
        tops = []
        deadline = time.monotonic() + 2.0
        try:
            projects = os.listdir(base)
        except OSError:
            projects = []
        for proj in projects:
            try:
                sessions = os.listdir(base + "/" + proj)
            except OSError:
                continue
            for sid in sessions:
                if time.monotonic() > deadline:
                    break
                r = dirscan.size_of("%s/%s/%s" % (base, proj, sid), budget_s=0.3, max_entries=60000)
                if r is None:
                    continue
                sizes[sid] = (r[0], r[2])
                if r[0] >= 50 * MB:
                    known = self.attr.known.get(("sid", sid)) or {}
                    label = known.get("label") or proj.rsplit("-Projects-", 1)[-1].lstrip("-")
                    tops.append({"label": clean(label, 40), "b": r[0], "partial": r[2],
                                 "live": sid in self.attr.sid_agent})
        tops.sort(key=lambda x: -x["b"])
        self.enrich.scratch = sizes
        self.w["scratch_top"] = tops[:6]

    def _job_crash(self, now):
        c = folders.crash_dumps()
        names = {x["name"] for x in c["items"]}
        if self._seen_crash is None:
            self._seen_crash = set(names)            # first run: these are old news
        else:
            for x in c["items"]:
                if x["name"] not in self._seen_crash:
                    self.events.add("crash:" + x["name"][-40:], "warn",
                                    "%s crashed%s." % (x["program"], " (a %s crash dump was saved)" % alerts.gb(x["b"])
                                                      if x["b"] >= 100 * MB else ""), ts=x["ts"])
                    self._seen_crash.add(x["name"])
                    self.attr.dirty = True
            if len(self._seen_crash) > 400:
                self._seen_crash = set(names)
        for x in c["items"]:
            x.pop("name", None)
        self.scanner.set_size(folders.COREDUMP_DIR, c["core_b"], now)
        self.w["crash"] = c

    def _job_klog(self, now):
        got = klog.poll(self.klog_since)
        if got is None:
            self.sources["klog"] = "cannot read the system journal"
            return
        self.sources["klog"] = "ok"
        for ts, kind, detail in got:
            self.klog_since = max(self.klog_since, ts)
            if kind == "oom_kill":
                if detail and any(detail.encode() in cg for cg in self.agent_cgroups):
                    detail = "the group of apps that runs your agents"
                # Replace the nameless entry the sampler added when the kernel counter moved.
                for i, (ots, okind, odetail) in enumerate(self.oom):
                    if okind == "oom_kill" and not odetail and abs(ots - ts) < 90:
                        self.oom[i] = (ts, kind, clean(detail, 40))
                        break
                else:
                    self.oom.append((ts, kind, clean(detail, 40)))
                eid = "oom:%d" % int(ts)
                if not self.events.has(eid):
                    self.events.add(eid, "crit", "Linux stopped %s because memory ran out." % (clean(detail, 40) or "a program"), ts=ts)
            else:
                self.oom.append((ts, kind, ""))
        while self.oom and now - self.oom[0][0] > 24 * 3600:
            self.oom.popleft()

    def _job_folders(self, now):
        s = self.snapshot
        main = next((d for d in s.get("disks", []) if d["mount"] == "/"), None)
        if main is None:
            return
        overloaded = ((s["cpu"].get("wait_pct") or 0) > 40 or (s["io"].get("stall_pct") or 0) > 20
                      or (s["mem"].get("stall_pct") or 0) > 10)
        hot = [ag.cwd for ag in self.attr.agents.values() if ag.cwd]
        before = self.scanner.scanning
        self.scanner.tick(now, main["avail_b"], overloaded, hot)
        if before and not self.scanner.scanning:
            self.attr.dirty = True               # results arrived: save them with the registry

    def _job_prune(self, now):
        """Forget facts about processes that no longer exist."""
        e = self.enrich
        alive = {(p.pid, p.start) for p in self.table.procs.values()}
        for store in (e.env, e.sess, e.root, e.codex, e.mem, e.group, e.suspect, self._io, self._io_hist,
                      self._rollouts):      # e.tmux is rebuilt whole by its own job
            for k in [k for k in store if k not in alive]:
                store.pop(k, None)
        self.hist.drop_agents({a["id"] for a in self.snapshot.get("agents", [])})
        self.token_counter.forget(now)
        self.attr.forget_old(now)
        self.sources["agents"] = "ok" if os.path.isdir(agents_claude.SESSIONS_DIR) else "no Claude Code session folder"

    def _job_persist(self, now):
        self.save_state()
