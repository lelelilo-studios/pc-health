import os
import subprocess
import sys
import time
import unittest

from pchealth import gpu, httpd, klog, procfs, proctable, sysfs, tmux
from pchealth.proctable import Proc


class PidStat(unittest.TestCase):
    def test_comm_with_spaces_and_parens(self):
        raw = (b"4242 (tmux: server (1)) S 1 4242 4242 0 -1 4194560 100 0 0 0 "
               b"7 3 11 5 20 0 1 0 987654 12345678 2048 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0\n")
        comm, state, ppid, tty, own, child, start, rss = procfs.parse_pid_stat(raw)
        self.assertEqual(comm, "tmux: server (1)")
        self.assertEqual((state, ppid, tty), ("S", 1, 0))
        self.assertEqual((own, child, start, rss), (10, 16, 987654, 2048))

    def test_garbage(self):
        self.assertIsNone(procfs.parse_pid_stat(b""))
        self.assertIsNone(procfs.parse_pid_stat(b"12 (x) S 1"))

    def test_own_process(self):
        st = procfs.pid_stat(os.getpid())
        self.assertIsNotNone(st)
        self.assertGreater(st[7], 0)


class SystemFiles(unittest.TestCase):
    def test_meminfo(self):
        d = procfs.parse_kv_kb(b"MemTotal:       32780796 kB\nMemAvailable:   19733956 kB\nHugePages_Total:       0\nBad line\n")
        self.assertEqual(d["MemTotal"], 32780796)
        self.assertEqual(d["HugePages_Total"], 0)

    def test_pressure(self):
        d = procfs.parse_pressure(b"some avg10=2.17 avg60=1.30 avg300=0.56 total=316734549\nfull avg10=0.00 avg60=0.02 avg300=0.03 total=1\n")
        self.assertEqual(d["some"]["avg10"], 2.17)
        self.assertEqual(d["full"]["avg60"], 0.02)

    def test_cpu_stat(self):
        cpus, btime = procfs.parse_cpu_stat(b"cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 10 0 5 80 5 0 0 0 0 0\nintr 1 2 3\nbtime 1700000000\n")
        self.assertEqual(cpus["cpu"], (150, 1000))
        self.assertEqual(cpus["cpu0"], (15, 100))
        self.assertEqual(btime, 1700000000)

    def test_diskstats_and_netdev(self):
        d = procfs.parse_diskstats(b" 259 0 nvme0n1 10 0 2000 5 20 0 4000 7 0 9 12\n")
        self.assertEqual(d["nvme0n1"], (2000 * 512, 4000 * 512))
        n = procfs.parse_netdev(b"Inter-|   Receive\n face |bytes\n    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n"
                                b"enp4s0: 1000 9 0 0 0 0 0 0 2000 8 0 0 0 0 0 0\n")
        self.assertEqual(n, {"enp4s0": (1000, 2000)})

    def test_counters_and_io(self):
        self.assertEqual(procfs.parse_counters(b"pswpin 7\npswpout 9\nother 1\n", ("pswpin", "oom_kill")), {"pswpin": 7})
        self.assertEqual(procfs.parse_io_write_bytes(b"rchar: 1\nwrite_bytes: 4096\ncancelled_write_bytes: 0\n"), 4096)

    def test_mounts(self):
        raw = (b"/dev/nvme0n1p5 / ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\ntmpfs /run tmpfs rw 0 0\n"
               b"/dev/sda1 /run/media/u/SSD\\0402TB ntfs3 rw 0 0\n/dev/loop3 /snap/x/1 squashfs ro 0 0\n"
               b"/dev/nvme0n1p5 /var/snap/y ext4 rw 0 0\n")
        self.assertEqual(sysfs.parse_mounts(raw), [("/dev/nvme0n1p5", "/", "ext4"), ("tmpfs", "/tmp", "tmpfs"),
                                                   ("/dev/sda1", "/run/media/u/SSD 2TB", "ntfs3")])

    def test_mount_labels(self):
        self.assertEqual(sysfs.mount_label("/"), "Main disk")
        self.assertEqual(sysfs.mount_label("/data", "/dev/sda2", "ext4"), "/data")
        self.assertEqual(sysfs.mount_label("/run/media/u/36C82BFBC82BB7CF", "/dev/nvme0n1p3", "ntfs3"),
                         "Windows drive (nvme0n1p3)")


class Environ(unittest.TestCase):
    def test_only_whitelisted_keys_are_returned(self):
        env = dict(os.environ, EMDASH_TASK_NAME="demo", EMDASH_HOOK_TOKEN="s3cret-token", CLAUDECODE="1")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
        try:
            for _ in range(100):                 # until the child has replaced itself with the new program
                if b"time.sleep" in (procfs.read1("/proc/%d/cmdline" % p.pid) or b""):
                    break
                time.sleep(0.02)
            got = procfs.pid_environ_whitelist(p.pid, ("EMDASH_TASK_NAME", "CLAUDECODE", "MISSING"))
        finally:
            p.terminate()
            p.wait()
        self.assertEqual(got, {"EMDASH_TASK_NAME": "demo", "CLAUDECODE": "1"})


class Gpu(unittest.TestCase):
    def test_query(self):
        g = gpu.parse_gpu(b"35, 615, 16376, 33, 7.09, [N/A]\n")
        self.assertEqual(g["util_pct"], 35.0)
        self.assertEqual(g["vram_total_b"], 16376 * 1024 * 1024)
        self.assertIsNone(g["fan_pct"])
        self.assertIsNone(gpu.parse_gpu(b"garbage\n"))

    def test_pmon(self):
        raw = (b"# gpu         pid   type     fb   ccpm    command \n# Idx           #    C/G     MB     MB    name \n"
               b"    0       4428     G    226      0    gnome-shell    \n    0       5771   C+G     68      0    ptyxis\n"
               b"    0          -     -      -      -    -\n")
        self.assertEqual(gpu.parse_pmon(raw), {4428: ("gnome-shell", 226 << 20), 5771: ("ptyxis", 68 << 20)})


class Journal(unittest.TestCase):
    def test_lines(self):
        raw = (b"1759280046.100000 pc kernel: NVRM: GPU0 nvAssertOkFailedNoLog: Assertion failed: Out of memory [NV_ERR_NO_MEMORY] (0x00000051)\n"
               b"1759280050.000000 pc kernel: Out of memory: Killed process 4242 (UnrealEditor) total-vm:1kB\n"
               b"1759280060.000000 pc systemd-oomd[900]: Killed /user.slice/user-1000.slice/user@1000.service/app.slice/"
               b"app-org.chromium.Chromium-8413.scope due to memory pressure for /user.slice being 61.00% > 50.00%\n"
               b"not a line\n")
        got = klog.parse(raw)
        self.assertEqual([k for _, k, _ in got], ["gpu_oom", "oom_kill", "oom_kill"])
        self.assertEqual(got[1][2], "UnrealEditor")
        self.assertEqual(got[2][2], "app-org.chromium.Chromium")


class Tmux(unittest.TestCase):
    def test_panes_and_sessions(self):
        rows = ["%0\t/dev/pts/8\ttest\t0\t0\t246\t58\t1\t1\t1\t1\tclaude\t$0\t86376\t1791338460",
                "%3\t/dev/pts/9\tmy work\t0\t0\t80\t24\t0\t2\t0\t1\tbash\t$2\t86376\t1791338460",
                "%4\t/dev/pts/10\tmy work\t1\t0\t80\t24\t0\t2\t1\t0\tvim\t$2\t86376\t1791338460",
                "%5\t/dev/pts/11\tmy work\t1\t1\t80\t24\t0\t2\t1\t1\thtop\t$2\t86376\t1791338460",
                "broken line", "x\t1\t2\t3\t4\t5\t6\t7\t8\t9\t10\t11\t12\t13\t14",
                "%6\t/dev/pts/12\told\t0\t0\t80\t24\t0\t1\t1\t1\tbash"]            # a line without the server's fields
        panes = tmux.parse_panes("\n".join(rows).encode())
        self.assertEqual(sorted(panes), ["%0", "%3", "%4", "%5"])
        # "id" is tmux's own name for the session, which a rename does not change; "server" says which tmux answered.
        self.assertEqual(panes["%0"], {"tty": "/dev/pts/8", "session": "test", "window": 0, "index": 0, "w": 246, "h": 58,
                                       "attached": 1, "windows": 1, "active": True, "cmd": "claude",
                                       "id": "$0", "server": "86376:1791338460"})
        sessions = tmux.sessions_from(panes)
        self.assertEqual(sorted(sessions), ["my work", "test"])
        self.assertEqual((sessions["my work"]["pane"], sessions["my work"]["cmd"]), ("%5", "htop"))   # where you would land
        self.assertTrue(tmux.SESSION_NAME.match("test"))
        self.assertIsNone(tmux.SESSION_NAME.match("my work"))             # not openable from an address as it is
        self.assertIsNone(tmux.SESSION_NAME.match("a;b"))

    def test_capture(self):
        raw = "line one\n\x1b[31mred\x1b[39m \u2733\n\n\x1e246\t58\t2\t55\t1\n".encode()
        got = tmux.parse_capture(raw)
        self.assertEqual(got["text"], "line one\n\x1b[31mred\x1b[39m \u2733\n")
        self.assertEqual((got["w"], got["h"], got["cx"], got["cy"], got["cursor"]), (246, 58, 2, 55, True))
        self.assertIsNone(tmux.parse_capture(b"no info line"))
        self.assertIsNone(tmux.parse_capture(b"text\n\x1enot\tnumbers\n"))

    def test_clients_and_emdash_names(self):
        raw = b"4121\tweb-store-3f9c2e7a1b\n4130\twork\n4140\twork\nnot a line\nx\tbad\n"
        self.assertEqual(tmux.parse_clients(raw), {"web-store-3f9c2e7a1b": [4121], "work": [4130, 4140]})
        self.assertEqual(tmux.parse_clients(b""), {})
        for name in ("web-store-3f9c2e7a1b", "projects-7d41c0b9e2"):
            self.assertTrue(tmux.EMDASH_NAME.search(name), name)          # the names the Emdash app gives
        for name in ("work", "notes-2", "api-0123", "build-0123456789x", "ABCDEF0123-ABCDEF0123"):
            self.assertIsNone(tmux.EMDASH_NAME.search(name), name)

    def test_only_this_users_socket_and_plain_pane_ids(self):
        uid = os.getuid()
        self.assertIsNone(tmux.socket_from_env("", uid))
        self.assertIsNone(tmux.socket_from_env("/etc/passwd,1,0", uid))
        self.assertIsNone(tmux.socket_from_env("/tmp/tmux-%d/../../etc/passwd,1,0" % uid, uid))
        self.assertIsNone(tmux.socket_from_env("/tmp/tmux-%d/does-not-exist,1,0" % uid, uid))
        self.assertIsNone(tmux.socket_from_env("/tmp/tmux-%d/default,1,0" % (uid + 1), uid))
        for bad in ("", "0", "%", "%1; kill-server", "%1 -t %2", "test:0.0", None):
            self.assertIsNone(tmux.capture("/tmp/tmux-%d/default" % uid, bad))

    def test_same_tty(self):
        self.assertFalse(tmux.same_tty("/dev/pts/8", 0))                    # no controlling terminal
        self.assertFalse(tmux.same_tty("/dev/null", 34824))
        self.assertFalse(tmux.same_tty("/dev/pts/999999", 34824))


class Routes(unittest.TestCase):
    RAW = (b"Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
           b"enp4s0\t00000000\t0144A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
           b"enp4s0\t0044A8C0\t00000000\t0001\t0\t0\t100\t00FCFFFF\t0\t0\t0\n")

    def test_connected_subnets(self):
        nets = httpd.parse_routes(self.RAW)
        self.assertEqual(nets, [(0xC0A84400, 0xFFFFFC00)])

    def test_client_filter(self):
        nets = httpd.parse_routes(self.RAW)
        self.assertTrue(httpd.client_allowed("127.0.0.1", nets, lan=False))
        self.assertTrue(httpd.client_allowed("192.168.68.77", nets, lan=True))
        self.assertTrue(httpd.client_allowed("192.168.71.254", nets, lan=True))
        self.assertFalse(httpd.client_allowed("192.168.68.77", nets, lan=False))   # "this PC only" mode
        self.assertFalse(httpd.client_allowed("192.168.72.1", nets, lan=True))     # private, but not our subnet
        self.assertFalse(httpd.client_allowed("203.0.113.7", nets, lan=True))       # a public address
        self.assertFalse(httpd.client_allowed("not-an-ip", nets, lan=True))


def _p(pid, ppid, own=0, child=0, start=1, comm="x"):
    return Proc(pid, comm, "S", ppid, 0, own, child, start, 10)


class TreeDeltas(unittest.TestCase):
    def test_survivor_uses_own_delta(self):
        prev = {10: _p(10, 1, own=100)}
        new = {10: _p(10, 1, own=130)}
        proctable.compute_deltas(prev, new, prev_boot_ticks=1000)
        self.assertEqual((new[10].d_own, new[10].d_tree), (30, 30))

    def test_reaped_child_is_counted_once(self):
        # The child had used 40 ticks when last seen and 55 by the time it exited.
        prev = {10: _p(10, 1, own=100, child=0), 11: _p(11, 10, own=40, start=5)}
        new = {10: _p(10, 1, own=100, child=55)}
        proctable.compute_deltas(prev, new, prev_boot_ticks=1000)
        self.assertEqual(new[10].d_tree, 15)

    def test_short_lived_children_are_not_missed(self):
        # 20 processes started and finished between two scans; only the parent's child time shows it.
        prev = {10: _p(10, 1, own=5, child=0)}
        new = {10: _p(10, 1, own=6, child=200)}
        proctable.compute_deltas(prev, new, prev_boot_ticks=1000)
        self.assertEqual(new[10].d_tree, 201)

    def test_chain_exit(self):
        prev = {10: _p(10, 1), 11: _p(11, 10, own=10, start=5), 12: _p(12, 11, own=30, start=6)}
        new = {10: _p(10, 1, child=50)}          # both gone: 11 ended at 12, 12 at 38
        proctable.compute_deltas(prev, new, prev_boot_ticks=1000)
        self.assertEqual(new[10].d_tree, 10)

    def test_new_process_counts_fully_old_one_only_sets_baseline(self):
        prev = {10: _p(10, 1)}
        new = {10: _p(10, 1), 20: _p(20, 10, own=7, child=3, start=2000), 30: _p(30, 1, own=900, start=50)}
        proctable.compute_deltas(prev, new, prev_boot_ticks=1000)
        self.assertEqual(new[20].d_tree, 10)
        self.assertEqual(new[30].d_tree, 0)

    def test_pid_reuse_is_a_different_process(self):
        prev = {10: _p(10, 1, own=500, start=5)}
        new = {10: _p(10, 1, own=3, start=5000)}
        proctable.compute_deltas(prev, new, prev_boot_ticks=1000)
        self.assertEqual(new[10].d_tree, 3)

    def test_real_scan(self):
        t = proctable.Table()
        t.update()
        t.update()
        self.assertIn(os.getpid(), t.procs)
        self.assertGreaterEqual(t.cores(10 ** 9), procfs.NCPU - 0.001)   # capped at the number of threads


if __name__ == "__main__":
    unittest.main()
