"""What is taking space on the main disk: a watch list of folders, sized rarely and gently,
plus the crash-dump folders (cheap to list, and they once ate 27 GB unnoticed)."""

import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
GB = 1 << 30
BUILD_DIRS = ("target", "node_modules", "Intermediate", "Saved", "DerivedDataCache")
FIXED = ("/var/lib/apport/coredump", "/var/crash", HOME + "/.config/Epic", HOME + "/.cache",
         HOME + "/.codex", HOME + "/.claude", HOME + "/.local/share/Trash", HOME + "/.cargo/registry")
COREDUMP_DIR = "/var/lib/apport/coredump"
CRASH_DIR = "/var/crash"
HOT_EVERY_S = 30 * 60
COLD_EVERY_S = 12 * 3600
MIN_SHOWN_B = 200 << 20


def _subdirs(path):
    try:
        with os.scandir(path) as it:
            return [e.path for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith(".git")]
    except OSError:
        return []


def build_watch_list(project_dirs, extra=()):
    """Folders worth sizing: build output one or two levels inside each project, plus known hot spots."""
    found = []
    for base in project_dirs:
        for project in _subdirs(base):
            level1 = _subdirs(project)
            for d in level1:
                if os.path.basename(d) in BUILD_DIRS:
                    found.append(d)
                else:
                    found.extend(x for x in _subdirs(d) if os.path.basename(x) in BUILD_DIRS)
    found.extend(p for p in FIXED if os.path.isdir(p))
    found.extend(_subdirs("/data/scratch"))
    found.extend(p for p in extra if os.path.isdir(p))
    return sorted(set(found))


def tilde(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path


def program_from_dump(name):
    """'core._home_u_Apps_UnrealEditor.1000.<boot>.<pid>.<t>' or '_usr_bin_ffmpeg.1000.crash' -> program name."""
    stem = name[5:] if name.startswith("core.") else name
    exe = stem.split(".", 1)[0]
    return exe.rsplit("_", 1)[-1][:32] or "a program"


def crash_dumps():
    """-> {"count", "b", "items": [{"name","program","ts","b"}], "cmd"} for core files and crash reports."""
    items = []
    for folder, prefix in ((COREDUMP_DIR, "core."), (CRASH_DIR, "")):
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if not e.name.startswith(prefix) or not e.is_file(follow_symlinks=False):
                        continue
                    if folder == CRASH_DIR and not e.name.endswith(".crash"):
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    items.append({"name": e.name[:160], "program": program_from_dump(e.name),
                                  "ts": int(st.st_mtime), "b": st.st_blocks * 512,
                                  "core": folder == COREDUMP_DIR})
        except OSError:
            continue
    items.sort(key=lambda x: -x["ts"])
    cores = [x for x in items if x["core"]]
    core_b = sum(x["b"] for x in cores)
    return {"count": len(items), "b": sum(x["b"] for x in items), "cores": len(cores), "core_b": core_b,
            "newest_core": {"program": cores[0]["program"], "ts": cores[0]["ts"]} if cores else None,
            "items": items[:8], "cmd": "sudo rm " + COREDUMP_DIR + "/core.*" if core_b else ""}


class Scanner:
    """Runs dirscan in a separate lowest-priority process and keeps the results."""

    def __init__(self, project_dirs, extra=()):
        self.project_dirs = project_dirs
        self.extra = extra
        self.sizes = {}            # path -> [(ts, bytes, partial), ...] oldest first, at most 4
        self.proc = None
        self.buf = b""
        self.watch = []
        self.started = 0.0
        self.last_hot = 0.0
        self.last_cold = 0.0
        self.avail_at_scan = None
        self.first_due = time.time() + 90

    @property
    def scanning(self):
        return self.proc is not None

    def tick(self, now, main_avail_b, overloaded, hot_roots=()):
        """Call about once a minute. Starts a scan when one is due; collects output of a running one."""
        self._forget_gone()
        if self.proc is not None:
            self._collect(now)
            return
        if now < self.first_due or overloaded:
            return
        cold_due = now - self.last_cold >= COLD_EVERY_S
        dropped = self.avail_at_scan is not None and self.avail_at_scan - main_avail_b >= GB
        freed = self.avail_at_scan is not None and main_avail_b - self.avail_at_scan >= 5 * GB
        spaced = now - max(self.last_hot, self.last_cold) >= HOT_EVERY_S
        hot_due = dropped and spaced
        if freed and spaced:
            cold_due = True                  # something big was deleted: the sizes shown are out of date
        if not (cold_due or hot_due):
            return
        if cold_due or not self.watch:
            self.watch = build_watch_list(self.project_dirs, self.extra)
        if cold_due:
            paths = self.watch
            self.last_cold = now
        else:
            roots = tuple(r.rstrip("/") + "/" for r in hot_roots if r)
            paths = [p for p in self.watch if p.startswith(roots)] if roots else []
            paths += [p for p in (COREDUMP_DIR, CRASH_DIR) if p in self.watch]
            self.last_hot = now
        self.avail_at_scan = main_avail_b
        if paths:
            self._start(paths, now)

    def _forget_gone(self):
        """Drop folders that were deleted, or replaced by a link to somewhere else (e.g. build output
        moved to another drive): their old size no longer describes anything on this disk."""
        for path in [p for p in self.sizes if os.path.islink(p) or not os.path.isdir(p)]:
            del self.sizes[path]

    def _start(self, paths, now):
        try:
            self.proc = subprocess.Popen([sys.executable, "-E", "-s", "-m", "pchealth.dirscan"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, cwd=os.path.dirname(os.path.dirname(__file__)))
            self.proc.stdin.write(json.dumps({"paths": paths, "duty": 0.15, "budget_s": 120}).encode())
            self.proc.stdin.close()
            os.set_blocking(self.proc.stdout.fileno(), False)
            self.started = now
            self.buf = b""
        except (OSError, ValueError):
            self.proc = None

    def _collect(self, now):
        try:
            while True:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.buf += chunk
        except BlockingIOError:
            pass
        except OSError:
            pass
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            try:
                r = json.loads(line)
                self._record(r["path"], int(r["b"]), bool(r["partial"]), now)
            except (ValueError, KeyError, TypeError):
                continue
        if self.proc.poll() is not None:
            self.proc.stdout.close()
            self.proc = None

    def _record(self, path, b, partial, now):
        hist = self.sizes.setdefault(path, [])
        if hist and now - hist[-1][0] < 3600:
            hist[-1] = (hist[-1][0], b, partial)      # refine the newest point, keep its time
        else:
            hist.append((int(now), b, partial))
            del hist[:-4]

    def set_size(self, path, b, now):
        """Record a size learned some cheaper way (e.g. the crash-dump folder, which is listed every minute)."""
        hist = self.sizes.get(path)
        if hist and hist[-1][1] != b:
            hist[-1] = (hist[-1][0], b, False)

    def export(self, now, limit=12):
        items = []
        newest = 0
        for path, hist in self.sizes.items():
            ts, b, partial = hist[-1]
            newest = max(newest, ts)
            if b < MIN_SHOWN_B:
                continue
            first = hist[0]
            items.append({"path": tilde(path), "b": b, "partial": partial,
                          "change_b": b - first[1] if len(hist) > 1 else None,
                          "over_s": int(ts - first[0]) if len(hist) > 1 else None,
                          "main": not path.startswith("/data/")})
        items.sort(key=lambda x: -x["b"])
        return {"age_s": int(now - newest) if newest else None, "scanning": self.scanning, "items": items[:limit]}

    def dump(self):
        return {p: h for p, h in self.sizes.items()}

    def load(self, data):
        try:
            for p, h in data.items():
                if isinstance(p, str) and p.startswith("/"):
                    self.sizes[p] = [(int(t), int(b), bool(x)) for t, b, x in h][-4:]
        except (TypeError, ValueError, AttributeError):
            self.sizes = {}
