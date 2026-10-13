"""Hardware sensors (/sys/class/hwmon), mounted drives, and the memory pressure Ubuntu's memory guard watches."""

import os
import re

from . import procfs

HWMON = "/sys/class/hwmon"
DISK_FS = ("ext4", "ext3", "xfs", "btrfs", "f2fs", "ntfs", "ntfs3", "fuseblk", "exfat")
RAM_MOUNTS = ("/tmp", "/dev/shm")
_OCT = re.compile(r"\\([0-7]{3})")


def _read_text(path):
    raw = procfs.read1(path, 256)
    return raw.decode("ascii", "replace").strip() if raw else ""


def discover_sensors(root=HWMON):
    """Find the sensors worth showing. -> [{'id','label','kind','path'}], kind is 'temp' or 'fan'."""
    found = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found
    n_ssd = 0
    for hw in names:
        base = root + "/" + hw
        chip = _read_text(base + "/name")
        try:
            files = sorted(os.listdir(base))
        except OSError:
            continue
        for f in files:
            if not f.endswith("_input"):
                continue
            stem = f[:-6]
            label = _read_text(base + "/" + stem + "_label")
            path = base + "/" + f
            low = label.lower()
            if f.startswith("temp"):
                if chip in ("k10temp", "zenpower") and label in ("Tctl", "Tdie"):
                    found.append({"id": "cpu", "label": "Processor", "kind": "temp", "path": path})
                elif chip == "coretemp" and low.startswith("package"):
                    found.append({"id": "cpu", "label": "Processor", "kind": "temp", "path": path})
                elif chip == "nvme" and label == "Composite":
                    n_ssd += 1
                    found.append({"id": "ssd" if n_ssd == 1 else "ssd%d" % n_ssd,
                                  "label": "NVMe SSD" if n_ssd == 1 else "NVMe SSD %d" % n_ssd,
                                  "kind": "temp", "path": path})
                elif "coolant" in low or "liquid" in low:
                    found.append({"id": "coolant", "label": "Liquid cooler", "kind": "temp", "path": path})
            elif f.startswith("fan"):
                if "pump" in low:
                    found.append({"id": "pump", "label": "Cooler pump", "kind": "fan", "path": path})
                else:
                    found.append({"id": hw + stem, "label": label.title() if label else "Fan",
                                  "kind": "fan", "path": path})
    # Keep the first of each id (e.g. Tctl over Tdie).
    seen = set()
    out = []
    for s in found:
        if s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
    return out


def read_sensors(sensors):
    """-> (temps {id: (label, celsius)}, fans [(label, rpm)]); fans reporting 0 rpm are not connected."""
    temps = {}
    fans = []
    for s in sensors:
        txt = _read_text(s["path"])
        try:
            v = int(txt)
        except ValueError:
            continue
        if s["kind"] == "temp":
            temps[s["id"]] = (s["label"], round(v / 1000.0, 1))
        elif v > 0 or s["id"] == "pump":
            fans.append((s["id"], s["label"], v))
    return temps, fans


def parse_mounts(raw):
    """/proc/self/mounts -> [(device, mountpoint, fstype)] for real drives and RAM-backed temp dirs."""
    out = []
    seen_dev = set()
    for line in raw.decode("utf-8", "replace").splitlines():
        f = line.split()
        if len(f) < 3:
            continue
        dev, mnt, fs = f[0], _OCT.sub(lambda m: chr(int(m.group(1), 8)), f[1]), f[2]
        if fs in DISK_FS and dev.startswith("/dev/"):
            if dev in seen_dev or mnt.startswith(("/snap/", "/var/snap/", "/boot")):
                continue
            seen_dev.add(dev)
            out.append((dev, mnt, fs))
        elif fs == "tmpfs" and mnt in RAM_MOUNTS:
            out.append((dev, mnt, fs))
    return out


def mount_label(mnt, dev="", fs=""):
    if mnt == "/":
        return "Main disk"
    if mnt == "/tmp":
        return "Temp files (held in memory)"
    if mnt == "/dev/shm":
        return "Shared memory"
    if not mnt.startswith(("/run/media/", "/media/", "/mnt/")):
        return mnt                                    # e.g. /data: the path is the name people use
    name = os.path.basename(mnt.rstrip("/"))
    if re.fullmatch(r"[0-9A-Fa-f-]{8,40}", name):     # mounted under its serial number: not a name
        kind = "Windows drive" if fs.startswith("ntfs") or fs == "fuseblk" else "Drive"
        return "%s (%s)" % (kind, os.path.basename(dev)) if dev else kind
    return name or mnt


def statvfs(mnt):
    """-> dict(total_b, avail_b, used_pct, inodes_free_pct) or None. used_pct matches `df`."""
    try:
        s = os.statvfs(mnt)
    except OSError:
        return None
    total = s.f_blocks * s.f_frsize
    avail = s.f_bavail * s.f_frsize
    used = (s.f_blocks - s.f_bfree) * s.f_frsize
    denom = used + avail
    return {
        "total_b": total,
        "avail_b": avail,
        "used_pct": round(100.0 * used / denom, 1) if denom else 0.0,
        "inodes_free_pct": round(100.0 * s.f_favail / s.f_files, 1) if s.f_files else None,
    }


def user_memory_pressure(uid):
    """avg10 of the 'some' line for the user's session: the number systemd-oomd acts on."""
    raw = procfs.read1("/sys/fs/cgroup/user.slice/user-%d.slice/user@%d.service/memory.pressure" % (uid, uid))
    if not raw:
        return None
    return procfs.parse_pressure(raw).get("some", {}).get("avg10")
