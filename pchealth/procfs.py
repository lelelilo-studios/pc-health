"""Parsers and thin readers for /proc.

Parsers are pure (bytes/text in, values out) so tests can feed them fixtures.
Readers return None instead of raising: processes vanish between listing and reading.
"""

import os
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1


def read1(path, size=4096):
    """One read() call; enough for small single-record files such as /proc/<pid>/stat."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return os.read(fd, size)
    except OSError:
        return None
    finally:
        os.close(fd)


def read(path, limit=1 << 18):
    """Read a whole (bounded) file; /proc seq files hand out one page per read()."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        chunks = []
        n = 0
        while n < limit:
            b = os.read(fd, min(65536, limit - n))
            if not b:
                break
            chunks.append(b)
            n += len(b)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_tail(path, size):
    """Last `size` bytes of a regular file plus its (size, mtime), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        os.lseek(fd, max(0, st.st_size - size), os.SEEK_SET)
        return os.read(fd, size), st.st_size, st.st_mtime
    except OSError:
        return None
    finally:
        os.close(fd)


def readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def boot_seconds():
    """Seconds since boot, including suspend: the clock /proc start times are measured on."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def parse_pid_stat(raw):
    """/proc/<pid>/stat -> (comm, state, ppid, tty_nr, own_ticks, child_ticks, starttime, rss_pages).

    comm may contain spaces and parentheses, so split at the LAST ')'.
    own = utime+stime; child = cutime+cstime (time of reaped descendants).
    """
    left = raw.find(b"(")
    right = raw.rfind(b")")
    if left < 0 or right < left:
        return None
    f = raw[right + 2:].split()
    try:
        return (
            raw[left + 1:right].decode("utf-8", "replace"),
            chr(f[0][0]),
            int(f[1]),
            int(f[4]),
            int(f[11]) + int(f[12]),
            int(f[13]) + int(f[14]),
            int(f[19]),
            int(f[21]),
        )
    except (IndexError, ValueError):
        return None


def parse_kv_kb(raw):
    """'Key:   123 kB' lines (meminfo, status) -> {key: int}. Non-numeric values are skipped."""
    out = {}
    for line in raw.split(b"\n"):
        k, sep, v = line.partition(b":")
        if not sep:
            continue
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k.decode("ascii", "replace")] = int(parts[0])
    return out


def parse_pressure(raw):
    """/proc/pressure/* -> {'some': {'avg10':..,'avg60':..,'avg300':..}, 'full': {...}}."""
    out = {}
    for line in raw.decode("ascii", "replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        vals = {}
        for item in parts[1:]:
            k, _, v = item.partition("=")
            try:
                vals[k] = float(v)
            except ValueError:
                pass
        out[parts[0]] = vals
    return out


def parse_cpu_stat(raw):
    """/proc/stat -> ({'cpu': (busy, total), 'cpu0': ...}, btime). Ticks; busy excludes idle+iowait."""
    cpus = {}
    btime = 0
    for line in raw.split(b"\n"):
        if line.startswith(b"cpu"):
            f = line.split()
            try:
                v = [int(x) for x in f[1:9]]
            except ValueError:
                continue
            total = sum(v)
            cpus[f[0].decode()] = (total - v[3] - v[4], total)
        elif line.startswith(b"btime"):
            btime = int(line.split()[1])
    return cpus, btime


def parse_diskstats(raw):
    """/proc/diskstats -> {dev: (bytes_read, bytes_written)} for every block device listed."""
    out = {}
    for line in raw.split(b"\n"):
        f = line.split()
        if len(f) < 10:
            continue
        try:
            out[f[2].decode()] = (int(f[5]) * 512, int(f[9]) * 512)
        except ValueError:
            pass
    return out


def parse_netdev(raw):
    """/proc/net/dev -> {iface: (rx_bytes, tx_bytes)}, loopback excluded."""
    out = {}
    for line in raw.split(b"\n")[2:]:
        name, sep, rest = line.partition(b":")
        if not sep:
            continue
        f = rest.split()
        name = name.strip().decode()
        if name == "lo" or len(f) < 9:
            continue
        try:
            out[name] = (int(f[0]), int(f[8]))
        except ValueError:
            pass
    return out


def parse_counters(raw, keys):
    """'key value' lines (vmstat) -> {key: int} for the requested keys."""
    want = {k.encode() for k in keys}
    out = {}
    for line in raw.split(b"\n"):
        k, _, v = line.partition(b" ")
        if k in want:
            try:
                out[k.decode()] = int(v)
            except ValueError:
                pass
    return out


def parse_io_write_bytes(raw):
    """/proc/<pid>/io -> write_bytes (bytes actually sent to storage), or None."""
    for line in raw.split(b"\n"):
        if line.startswith(b"write_bytes:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def pid_stat(pid):
    raw = read1("/proc/%d/stat" % pid)
    return parse_pid_stat(raw) if raw else None


def pid_status(pid):
    """-> {'RssAnon','RssShmem','VmSwap' in kB, 'Uid'} (missing keys absent), or None."""
    raw = read1("/proc/%d/status" % pid, 8192)
    return parse_kv_kb(raw) if raw else None


def pid_uid(pid):
    try:
        return os.stat("/proc/%d" % pid).st_uid
    except OSError:
        return None


def pid_cmdline(pid, limit=4096):
    """argv as a list of str. Kept in memory only; callers must never export it raw."""
    raw = read1("/proc/%d/cmdline" % pid, limit)
    if not raw:
        return []
    return [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")]


def pid_environ_whitelist(pid, keys, limit=1 << 17):
    """Selected variables from a process environment.

    The environment of agent processes also holds secret tokens. Only the keys in `keys`
    are ever decoded or returned; everything else is dropped unread.
    Returns {} if readable but none present, None if unreadable.
    """
    raw = read("/proc/%d/environ" % pid, limit)
    if raw is None:
        return None
    want = {k.encode() + b"=" for k in keys}
    out = {}
    for item in raw.split(b"\0"):
        eq = item.find(b"=")
        if eq > 0 and item[:eq + 1] in want:
            out[item[:eq].decode()] = item[eq + 1:eq + 513].decode("utf-8", "replace")
    return out


def listen_ports():
    """{socket inode: port} for listening TCP sockets (IPv4 and IPv6)."""
    out = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        raw = read(path)
        if not raw:
            continue
        for line in raw.split(b"\n")[1:]:
            f = line.split()
            if len(f) > 9 and f[3] == b"0A":
                try:
                    out[int(f[9])] = int(f[1].rsplit(b":", 1)[1], 16)
                except (ValueError, IndexError):
                    pass
    return out


def pid_socket_inodes(pid, cap=256):
    """Inodes of the sockets a process holds open (bounded)."""
    out = []
    base = "/proc/%d/fd" % pid
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names[:cap]:
        target = readlink(base + "/" + name)
        if target and target.startswith("socket:["):
            try:
                out.append(int(target[8:-1]))
            except ValueError:
                pass
    return out
