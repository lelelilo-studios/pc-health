"""Trend buffers and the event log.

Three fixed-size rings per series, one per range the page offers. A slot's position is
derived from absolute time (bucket % n), so persisting a ring needs only its newest
bucket index and the raw values.
"""

import array
import json
import os
import struct
import time

NAN = float("nan")
RANGES = {"1h": (10, 360), "24h": (60, 1440), "7d": (600, 1008)}
PERSISTED = ("24h", "7d")
EXPORT_POINTS = 360
MAGIC = b"PCH1"


def atomic_write(path, data):
    """Write-then-rename. Returns False (never raises) if the disk is full or unwritable."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


class Ring:
    __slots__ = ("step", "n", "agg", "vals", "head", "count")

    def __init__(self, step, n, agg="max"):
        self.step = step
        self.n = n
        self.agg = agg                       # how samples inside one bucket combine
        self.vals = array.array("f", [NAN]) * n
        self.head = 0                        # absolute index (ts // step) of the newest bucket
        self.count = 0                       # samples folded into the newest bucket

    def push(self, ts, v):
        b = int(ts // self.step)
        if self.head == 0:
            self.head = b
        if b < self.head:
            return                           # clock stepped back; ignore
        if b > self.head:
            for k in range(1, min(b - self.head, self.n) + 1):
                self.vals[(self.head + k) % self.n] = NAN
            self.head = b
            self.count = 0
        i = b % self.n
        cur = self.vals[i]
        if self.count == 0 or cur != cur:
            self.vals[i] = v
        elif self.agg == "min":
            if v < cur:
                self.vals[i] = v
        elif self.agg == "max":
            if v > cur:
                self.vals[i] = v
        else:
            self.vals[i] = (cur * self.count + v) / (self.count + 1)
        self.count += 1

    def ordered(self, now=None):
        """Values oldest -> newest, ending at the bucket for `now` (stale slots read as NaN)."""
        end = self.head if now is None else int(now // self.step)
        out = []
        for b in range(end - self.n + 1, end + 1):
            out.append(self.vals[b % self.n] if b <= self.head and self.head - b < self.n else NAN)
        return out, (end - self.n + 1) * self.step

    def value_at(self, ts):
        """Value of the bucket containing ts, or None if it is not held."""
        b = int(ts // self.step)
        if b > self.head or self.head - b >= self.n:
            return None
        v = self.vals[b % self.n]
        return None if v != v else v


def _agg_for(name):
    if name.startswith("disk_avail"):
        return "min"                         # a disk runs out at its troughs, not its average
    if name in ("cpu_busy_pct", "gpu_util_pct"):
        return "avg"
    return "max"


def _combine(chunk, agg):
    vals = [v for v in chunk if v == v]
    if not vals:
        return None
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    return sum(vals) / len(vals)


class History:
    def __init__(self):
        self.series = {}                     # name -> {range: Ring}
        self.agents = {}                     # agent id -> {"cpu_cores": Ring, "mem_mb": Ring}

    def push(self, ts, name, value):
        if value is None:
            return
        rings = self.series.get(name)
        if rings is None:
            agg = _agg_for(name)
            rings = self.series[name] = {r: Ring(step, n, agg) for r, (step, n) in RANGES.items()}
        for ring in rings.values():
            ring.push(ts, value)

    def push_agent(self, ts, agent_id, cpu_cores, mem_mb):
        rings = self.agents.get(agent_id)
        if rings is None:
            step, n = RANGES["1h"]
            rings = self.agents[agent_id] = {"cpu_cores": Ring(step, n, "avg"), "mem_mb": Ring(step, n, "max")}
        rings["cpu_cores"].push(ts, cpu_cores)
        if mem_mb is not None:
            rings["mem_mb"].push(ts, mem_mb)

    def drop_agents(self, keep_ids):
        for k in [k for k in list(self.agents) if k not in keep_ids]:
            self.agents.pop(k, None)

    def ring(self, name, rng):
        rings = self.series.get(name)
        return rings[rng] if rings else None

    def export(self, rng, now=None):
        """JSON-ready dict for one range, thinned to about EXPORT_POINTS points per series."""
        now = time.time() if now is None else now
        step, n = RANGES[rng]
        k = -(-n // EXPORT_POINTS)                 # ceiling: never more than EXPORT_POINTS points
        out = {"range": rng, "step_s": step * k, "n": 0, "t0": 0, "series": {}}
        for name, rings in list(self.series.items()):   # the sampler adds series while pages read
            vals, t0 = rings[rng].ordered(now)
            agg = rings[rng].agg
            pts = [_combine(vals[i:i + k], agg) for i in range(0, len(vals), k)]
            out["series"][name] = [None if v is None else round(v, 1) for v in pts]
            out["n"], out["t0"] = len(pts), t0
        if rng == "1h":
            ag = out["agents"] = {}
            for aid, rings in list(self.agents.items()):
                row = {}
                for key, ring in rings.items():
                    vals, _ = ring.ordered(now)
                    row[key] = [None if v != v else round(v, 2 if key == "cpu_cores" else 0) for v in vals]
                ag[aid] = row
        return out

    # -- persistence of the coarse rings, so the disk trend survives restarts --

    def dump(self):
        index = []
        blobs = []
        for name, rings in sorted(list(self.series.items())):
            for rng in PERSISTED:
                ring = rings[rng]
                index.append({"name": name, "range": rng, "step": ring.step, "n": ring.n, "head": ring.head})
                blobs.append(ring.vals.tobytes())
        header = json.dumps({"v": 1, "rings": index}).encode()
        return MAGIC + struct.pack("<I", len(header)) + header + b"".join(blobs)

    def load(self, data):
        """Restore rings from dump() output. Anything unexpected is skipped, never fatal."""
        try:
            if data[:4] != MAGIC:
                return 0
            hlen = struct.unpack("<I", data[4:8])[0]
            index = json.loads(data[8:8 + hlen])["rings"]
            off = 8 + hlen
            loaded = 0
            for e in index:
                size = int(e["n"]) * 4
                blob = data[off:off + size]
                off += size
                spec = RANGES.get(e["range"])
                if spec != (e["step"], e["n"]) or len(blob) != size:
                    continue
                agg = _agg_for(e["name"])
                rings = self.series.setdefault(
                    e["name"], {r: Ring(s, n, agg) for r, (s, n) in RANGES.items()})
                ring = rings[e["range"]]
                ring.vals = array.array("f")
                ring.vals.frombytes(blob)
                ring.head = int(e["head"])
                ring.count = 1
                loaded += 1
            return loaded
        except (KeyError, ValueError, TypeError, struct.error):
            return 0


class Events:
    """Things that happened, kept so the page can say what went on while nobody was looking."""

    def __init__(self, cap=200):
        self.items = []                      # newest last: {"id","ts","end","level","msg"}
        self.cap = cap
        self.dirty = False
        self.restored = set()                # ids that were ongoing when the monitor last stopped

    def open(self, eid, level, msg, ts=None):
        """Start (or refresh) an ongoing event with a stable id."""
        ts = time.time() if ts is None else ts
        for e in reversed(self.items):
            if e["id"] == eid and e["end"] is None:
                # The wording carries live numbers and changes every sample; only a change of
                # level is worth another write to disk.
                if e["level"] != level:
                    self.dirty = True
                e["msg"], e["level"] = msg[:240], level
                self.restored.discard(eid)   # still true after a restart: the same event carries on
                return e
        return self.add(eid, level, msg, ts, end=None)

    def close(self, eid, ts=None):
        ts = time.time() if ts is None else ts
        for e in reversed(self.items):
            if e["id"] == eid and e["end"] is None:
                e["end"] = int(ts)
                self.dirty = True

    def add(self, eid, level, msg, ts=None, end=0):
        """Record an event. end=0 means a moment in time; end=None means still ongoing."""
        ts = time.time() if ts is None else ts
        e = {"id": eid, "ts": int(ts), "end": int(ts) if end == 0 else end, "level": level, "msg": msg[:240]}
        self.items.append(e)
        del self.items[:-self.cap]
        self.dirty = True
        return e

    def has(self, eid):
        return any(e["id"] == eid for e in self.items)

    def recent(self, now, window_s=48 * 3600, limit=40):
        return [e for e in self.items if (e["end"] or now) >= now - window_s][-limit:][::-1]

    def dump(self):
        return json.dumps({"v": 1, "events": self.items}).encode()

    def load(self, data, now=None):
        now = time.time() if now is None else now
        try:
            items = json.loads(data)["events"]
        except (ValueError, KeyError, TypeError):
            return
        for e in items[-self.cap:]:
            if not isinstance(e, dict) or not {"id", "ts", "level", "msg"} <= e.keys():
                continue
            if e.get("end") is None:
                self.restored.add(str(e["id"]))      # may simply continue; settle() decides
            self.items.append({"id": str(e["id"]), "ts": int(e["ts"]), "end": e.get("end"),
                               "level": str(e["level"]), "msg": str(e["msg"])[:240]})

    def settle(self, now):
        """Shortly after start-up: end the restored events that did not turn out to be still true."""
        for eid in list(self.restored):
            self.close(eid, now)
        self.restored.clear()
