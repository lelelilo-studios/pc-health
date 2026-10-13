import json
import os
import tempfile
import unittest

from pchealth import (agents_claude, agents_codex, alerts, attribution, config, conversation, disks, folders, history, leftovers,
                      procfs, saved, subagents, tokens)
from pchealth.proctable import Proc

GB = 1 << 30
MB = 1 << 20
TH = config.DEFAULTS["thresholds"]


class Rings(unittest.TestCase):
    def test_buckets_and_aggregation(self):
        r = history.Ring(10, 6, "avg")
        r.push(1000, 10)
        r.push(1005, 30)          # same bucket: averaged
        r.push(1020, 50)          # skips one bucket
        vals, t0 = r.ordered(1020)
        self.assertEqual(vals[-1], 50)
        self.assertNotEqual(vals[-2], vals[-2])      # NaN: nothing was sampled there
        self.assertEqual(vals[-3], 20)
        self.assertEqual(t0, 970)
        self.assertEqual(r.value_at(1003), 20)
        self.assertIsNone(r.value_at(1010))
        self.assertIsNone(r.value_at(100))

    def test_min_ring_keeps_troughs(self):
        r = history.Ring(600, 4, "min")
        for ts, v in ((6000, 50), (6100, 20), (6500, 80)):
            r.push(ts, v)
        self.assertEqual(r.value_at(6000), 20)

    def test_stale_ring_reads_as_gaps(self):
        r = history.Ring(10, 4)
        r.push(1000, 5)
        vals, _ = r.ordered(1000 + 10 * 50)
        self.assertTrue(all(v != v for v in vals))

    def test_dump_load_roundtrip(self):
        h = history.History()
        for i in range(200):
            h.push(1_700_000_000 + i * 60, "disk_avail_mb:/", 50_000 - i)
            h.push(1_700_000_000 + i * 60, "cpu_busy_pct", i % 100)
        h2 = history.History()
        self.assertEqual(h2.load(h.dump()), 4)
        now = 1_700_000_000 + 199 * 60
        self.assertEqual(h2.ring("disk_avail_mb:/", "24h").value_at(now), 50_000 - 199)
        self.assertEqual(history.History().load(b"junk"), 0)
        self.assertEqual(history.History().load(h.dump()[:40]), 0)

    def test_export_is_bounded_and_json_safe(self):
        h = history.History()
        h.push(1_700_000_000, "cpu_busy_pct", 12.345)
        for rng in history.RANGES:
            out = h.export(rng, 1_700_000_000)
            self.assertLessEqual(out["n"], history.EXPORT_POINTS)
            json.dumps(out, allow_nan=False)


class EventLog(unittest.TestCase):
    def test_open_close_and_reload(self):
        ev = history.Events(cap=5)
        ev.open("disk", "warn", "low", ts=100)
        ev.open("disk", "warn", "lower", ts=150)         # same ongoing event, new wording
        self.assertEqual(len(ev.items), 1)
        ev.close("disk", ts=200)
        ev.add("crash:x", "warn", "x crashed", ts=300)
        self.assertEqual(ev.items[0]["end"], 200)
        self.assertEqual([e["id"] for e in ev.recent(400)], ["crash:x", "disk"])
        again = history.Events()
        again.load(ev.dump(), now=500)
        self.assertEqual(len(again.items), 2)
        again.load(b"not json")
        for i in range(20):
            ev.add("n%d" % i, "info", "x", ts=400 + i)
        self.assertEqual(len(ev.items), 5)

    def test_ongoing_events_survive_a_restart(self):
        ev = history.Events()
        ev.open("disk", "warn", "low", ts=100)
        ev.open("swap", "warn", "full", ts=100)
        after = history.Events()
        after.load(ev.dump(), now=200)
        after.open("disk", "warn", "still low", ts=205)       # still true after the restart
        after.settle(300)                                      # the swap problem went away meanwhile
        by_id = {e["id"]: e for e in after.items}
        self.assertEqual(len(after.items), 2)                  # no duplicate entry for the disk
        self.assertEqual((by_id["disk"]["ts"], by_id["disk"]["end"]), (100, None))
        self.assertEqual(by_id["swap"]["end"], 300)


class DiskEta(unittest.TestCase):
    def sawtooth(self, hours, drift_gb_per_h, swing_gb=30):
        """10-minute troughs: builds swing free space by tens of GB around a slow downward drift."""
        pts = []
        for i in range(hours * 6):
            t = i * 600
            base = 200 * GB - drift_gb_per_h * GB * t / 3600
            pts.append((t, base - (swing_gb * GB if (i // 3) % 2 else 0)))
        return pts

    def test_theil_sen(self):
        slope, neg = disks.theil_sen([(0, 10), (1, 8), (2, 6), (3, 4)])
        self.assertEqual(slope, -2)
        self.assertEqual(neg, 1.0)

    def test_steady_disk_has_no_rate(self):
        self.assertIsNone(disks.loss_rate([(i * 600, 100 * GB) for i in range(144)], 400 * GB))
        self.assertIsNone(disks.loss_rate([(0, 1), (600, 1)], 400 * GB))

    def test_sawtooth_without_drift_is_steady(self):
        self.assertIsNone(disks.loss_rate(self.sawtooth(24, 0), 400 * GB))

    def test_drift_under_sawtooth_is_found(self):
        rate = disks.loss_rate(self.sawtooth(24, 2.5), 400 * GB)
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate * 3600 / GB, 2.5, delta=1.0)

    def test_shown_value_does_not_flap(self):
        trend = disks.DiskTrend()
        pts = self.sawtooth(24, 2.5)
        shown = set()
        for wobble in (0, 2, -2, 3, -3, 1, 0, -1):
            eta, _ = trend.update(pts, 400 * GB, (100 + wobble) * GB)
            shown.add(eta)
        self.assertEqual(len(shown), 1)

    def test_worse_severity_shows_at_once(self):
        trend = disks.DiskTrend()
        pts = self.sawtooth(24, 2.5)
        first, _ = trend.update(pts, 400 * GB, 150 * GB)        # roughly 60 h
        second, _ = trend.update(pts, 400 * GB, 15 * GB)        # roughly 6 h: crosses "critical"
        self.assertGreater(first, 48 * 3600)
        self.assertLess(second, 8 * 3600)


class ClaudeSessions(unittest.TestCase):
    def test_states(self):
        now = 10_000.0
        st = lambda status, age, bg=False, busy=False, **kw: agents_claude.state(
            dict(status=status, statusUpdatedAt=(now - age) * 1000, **kw), bg, busy, now)[:2]
        self.assertEqual(st("busy", 5), ("working", "Working"))
        self.assertEqual(st("busy", 1200), ("working", "Working, quiet for 20 min"))
        self.assertEqual(st("busy", 1200, bg=True), ("working", "Working"))
        self.assertEqual(st("waiting", 5, waitingFor="input needed"), ("working", "Working"))
        self.assertEqual(st("waiting", 60, waitingFor="input needed"), ("waiting", "Waiting for you: input needed"))
        self.assertIn("background work", st("waiting", 60, bg=True)[1])
        self.assertEqual(st("idle", 60), ("idle", "Idle"))
        self.assertEqual(st("something-new", 60)[0], "unknown")
        self.assertEqual(agents_claude.state(None, True, False, now)[0], "working")
        self.assertEqual(agents_claude.state(None, False, False, now)[0], "unknown")

    def test_labels(self):
        label, title, host = agents_claude.describe(
            {"cwd": "/home/u/Projects/game", "name": "fix \x07 bell"},
            {"EMDASH_TASK_NAME": "menu", "EMDASH_ROOT_PATH": "/home/u/Projects/game"}, "")
        self.assertEqual((label, title, host), ("game / menu", "fix   bell", "emdash"))
        self.assertEqual(agents_claude.describe({"cwd": "/home/u/Projects/x"}, {}, "")[::2], ("x", "terminal"))
        self.assertEqual(agents_claude.describe(None, None, "")[0], "session")

    def test_session_file_must_match_process_start(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "77.json"), "w") as f:
                json.dump({"pid": 77, "procStart": "555", "status": "idle", "sessionId": "abc",
                           "messagingSocketPath": "/run/secret.sock"}, f)
            got = agents_claude.read_session(77, 555, d)
            self.assertEqual(got, {"procStart": "555", "status": "idle", "sessionId": "abc"})
            self.assertIsNone(agents_claude.read_session(77, 556, d))      # pid reused by another process
            self.assertIsNone(agents_claude.read_session(78, 555, d))
            with open(os.path.join(d, "79.json"), "w") as f:
                f.write("{broken")
            self.assertIsNone(agents_claude.read_session(79, 1, d))

    def test_model_and_clean(self):
        self.assertEqual(agents_claude.model_from_args(["claude", "--model", "opus[1m]"]), "opus[1m]")
        self.assertIsNone(agents_claude.model_from_args(["claude", "--model", "x y; rm -rf"]))
        self.assertEqual(len(agents_claude.clean("a" * 500)), 80)


class Codex(unittest.TestCase):
    def write(self, lines):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_rollout_states(self):
        started = {"type": "event_msg", "payload": {"type": "task_started"}}
        done = {"type": "event_msg", "payload": {"type": "task_complete"}}
        ctx = {"type": "turn_context", "payload": {"model": "gpt-5.5-codex", "cwd": "/x"}}
        path = self.write([ctx, started, done, started])
        mtime = os.stat(path).st_mtime
        st = agents_codex.rollout_state(path, True, mtime + 1)
        self.assertEqual((st["state"], st["model"]), ("working", "gpt-5.5-codex"))
        self.assertEqual(agents_codex.rollout_state(path, False, mtime + 500)["text"], "Possibly waiting for you")
        self.assertEqual(agents_codex.rollout_state(self.write([started, done]), False, mtime)["state"], "idle")
        self.assertEqual(agents_codex.rollout_state("/nonexistent", False, 0)["state"], "unknown")

    def test_mode(self):
        self.assertEqual(agents_codex.mode_from_args(["codex", "exec", "do x"]), "exec")
        self.assertEqual(agents_codex.mode_from_args(["codex"]), "tui")
        self.assertEqual(agents_codex.mode_from_args(["codex", "fix the tests"]), "tui")
        daemon = "/home/u/.codex/packages/app-server-daemon/releases/0.160.0/bin/codex"
        self.assertEqual(agents_codex.mode_from_args([daemon, "app-server", "--listen", "unix://"]), "daemon")
        self.assertEqual(agents_codex.mode_from_args([daemon, "app-server", "daemon", "pid-update-loop"]), "daemon")
        self.assertEqual(agents_codex.mode_from_args(["codex", "login"]), "other")


class LeftoverLabels(unittest.TestCase):
    def test_what(self):
        w = leftovers.what
        self.assertEqual(w("chrome", ["/snap/chromium/1/chrome", "--headless=new", "--user-data-dir=/tmp/looks"]), "Headless browser")
        self.assertEqual(w("chrome", ["/opt/google/chrome/chrome"]), "Browser")
        self.assertEqual(w("python3", ["python3", "-m", "http.server", "8797", "--bind", "127.0.0.1"]), "Python web server")
        self.assertEqual(w("xtask", ["target/debug/xtask", "serve", "8766"]), "Dev web server (xtask)")
        self.assertEqual(w("node", ["node", "/x/node_modules/.bin/vite"]), "Dev web server")
        self.assertEqual(w("colmap", ["colmap", "--token=abc"]), "colmap")
        self.assertTrue(leftovers.is_suspect_shape("Headless browser"))
        self.assertFalse(leftovers.is_suspect_shape("colmap"))

    def test_kill_command_is_digits_only(self):
        self.assertEqual(leftovers.kill_command([123, 456]), "kill 123 456")
        with self.assertRaises(ValueError):
            leftovers.kill_command(["1; rm -rf ~"])

    def test_levels(self):
        lv = lambda kind, age, c=0.0, mem=0, vram=0: leftovers.level(kind, age, c, mem, vram, TH)
        self.assertEqual(lv("orphan", 30), "info")
        self.assertEqual(lv("orphan", 700), "warn")
        self.assertEqual(lv("orphan", 700, c=7.0), "crit")
        self.assertEqual(lv("orphan", 30, c=7.0), "warn")
        self.assertEqual(lv("orphan", 700, mem=5 * GB), "crit")
        self.assertEqual(lv("suspect", 99999, c=7.0), "info")


class FakeTable:
    def __init__(self, procs, boot_s=1000.0, dt=2.0):
        self.procs = {p.pid: p for p in procs}
        self.boot_s = boot_s
        self.dt = dt

    def cores(self, ticks):
        return ticks / procfs.CLK_TCK / self.dt


def P(pid, ppid, comm, start=100, d=0, tty=0):
    p = Proc(pid, comm, "S", ppid, tty, d, 0, start, 100)
    p.d_own = p.d_tree = d
    return p


class Attribution(unittest.TestCase):
    def setUp(self):
        self.e = attribution.Enrich()
        self.a = attribution.Attribution(self.e, TH)

    def base(self):
        return [P(1, 0, "systemd"), P(50, 1, "systemd"), P(60, 50, "Emdash"), P(70, 50, "chrome")]

    def test_agent_with_children(self):
        procs = self.base() + [P(100, 60, "claude"), P(101, 100, "bash"), P(102, 101, "rustc", d=200),
                               P(103, 101, "rustc", d=200)]
        self.e.sess[(100, 100)] = {"status": "busy", "sessionId": "sid-a", "cwd": "/home/u/Projects/game"}
        self.e.env[(100, 100)] = {}
        self.a.update(FakeTable(procs), 5000.0)
        (ag,) = self.a.export_agents(5000.0)
        self.assertEqual((ag["label"], ag["state"], ag["procs"]), ("game", "working", 4))
        self.assertAlmostEqual(ag["cpu_cores"], 2.0)
        self.assertEqual(ag["doing"], [{"name": "rustc", "n": 2, "cpu_cores": 2.0}])
        self.assertEqual(self.a.export_leftovers(FakeTable(procs), 5000.0), [])
        self.assertEqual(self.a.label_of(103), "game")
        self.assertIsNone(self.a.label_of(70))            # the user's own browser belongs to no agent

    def test_unmarked_processes_are_never_flagged(self):
        procs = self.base() + [P(200, 50, "chrome"), P(201, 200, "chrome"), P(210, 50, "UnrealEditor"),
                               P(220, 1, "python3")]
        for p in procs:
            self.e.env[(p.pid, p.start)] = {}
        t = FakeTable(procs)
        self.a.update(t, 5000.0)
        self.assertEqual(self.a.export_leftovers(t, 5000.0), [])
        self.assertEqual(self.a.pub["env_wanted"], [])

    def test_detached_helper_then_orphan(self):
        marker = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sid-a", "CLAUDE_PID": "100"}
        agent = [P(100, 60, "claude")]
        helper = [P(300, 50, "chrome", d=1400), P(301, 300, "chrome", d=0)]       # handed to systemd
        self.e.sess[(100, 100)] = {"status": "busy", "sessionId": "sid-a", "cwd": "/home/u/Projects/game"}
        self.e.env.update({(100, 100): {}, (300, 100): marker, (301, 100): None})
        t = FakeTable(self.base() + agent + helper)
        self.a.update(t, 5000.0)
        self.a.update(t, 5002.0)                 # second pass: the session id is known by now
        (ag,) = self.a.export_agents(5002.0)
        self.assertEqual((ag["helpers"], ag["procs"]), (1, 3))       # counted on the agent while it lives
        self.assertEqual(self.a.export_leftovers(t, 5002.0), [])

        t2 = FakeTable(self.base() + helper)                         # the agent exits
        self.a.update(t2, 5010.0)
        self.assertEqual(self.a.export_leftovers(t2, 5010.0), [])    # grace period
        self.a.update(t2, 5100.0)
        (lo,) = self.a.export_leftovers(t2, 5100.0)
        self.assertEqual((lo["kind"], lo["from"], lo["n_procs"], lo["pid"]), ("orphan", "game", 2, 300))
        self.assertEqual(lo["cmd"], "kill 300")
        self.assertEqual(len(self.a.export_ended(5100.0)), 1)

    def test_orphan_found_after_monitor_restart(self):
        marker = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "gone-session", "CLAUDE_PID": "999"}
        procs = self.base() + [P(400, 50, "python3")]
        self.e.env[(400, 100)] = marker
        t = FakeTable(procs, boot_s=90000.0)
        self.a.update(t, 5000.0)
        (lo,) = self.a.export_leftovers(t, 5000.0)
        self.assertEqual((lo["kind"], lo["from_state"]), ("orphan", "ended"))

    def test_nested_claude_needs_a_session_file(self):
        procs = self.base() + [P(100, 60, "claude"), P(110, 100, "claude", start=200)]
        self.e.sess[(100, 100)] = {"status": "busy", "sessionId": "sid-a", "cwd": "/p/game"}
        self.a.update(FakeTable(procs), 5000.0)
        self.assertEqual(len(self.a.export_agents(5000.0)), 1)       # the second is a helper so far
        self.e.sess[(110, 200)] = {"status": "busy", "sessionId": "sid-b", "cwd": "/p/game"}
        self.a.update(FakeTable(procs), 5002.0)
        rows = self.a.export_agents(5002.0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["parent"], rows[0]["id"])

    def test_waiting_first_then_working_then_idle(self):
        procs = self.base()
        for i, (name, status) in enumerate((("idle-a", "idle"), ("work-b", "busy"), ("wait-c", "waiting"), ("work-a", "busy"))):
            procs.append(P(100 + i * 10, 60, "claude", start=100 + i))
            self.e.sess[(100 + i * 10, 100 + i)] = {"status": status, "statusUpdatedAt": (5000 - 120) * 1000,
                                                    "sessionId": "sid-%d" % i, "cwd": "/p/" + name}
        self.a.update(FakeTable(procs), 5000.0)
        self.assertEqual([r["label"] for r in self.a.export_agents(5000.0)], ["wait-c", "work-a", "work-b", "idle-a"])

    def test_idle_agents_most_recently_idle_first(self):
        procs = self.base()
        # (name, status, seconds since the state changed)
        rows = (("alpha", "idle", 9000), ("bravo", "idle", 60), ("charlie", "idle", 600), ("delta", "busy", 5),
                ("echo", "idle", None))
        for i, (name, status, ago) in enumerate(rows):
            procs.append(P(100 + i * 10, 60, "claude", start=100 + i))
            sess = {"status": status, "sessionId": "sid-%d" % i, "cwd": "/p/" + name}
            if ago is not None:
                sess["statusUpdatedAt"] = (5000 - ago) * 1000
            self.e.sess[(100 + i * 10, 100 + i)] = sess
        for now in (5000.0, 5002.0):                       # the order holds from one sample to the next
            self.a.update(FakeTable(procs), now)
            self.assertEqual([r["label"] for r in self.a.export_agents(now)],
                             ["delta", "bravo", "charlie", "alpha", "echo"])

    def test_terminals_follow_the_same_order(self):
        from pchealth.sampler import Monitor
        mon = Monitor(config.load("/nonexistent"), persist=False)
        procs = self.base()
        for i, (name, status) in enumerate((("idle-a", "idle"), ("work-b", "busy"), ("wait-c", "waiting"))):
            ident = (100 + i * 10, 100 + i)
            procs.append(P(ident[0], 60, "claude", start=ident[1]))
            mon.enrich.sess[ident] = {"status": status, "statusUpdatedAt": (5000 - 120) * 1000,
                                      "sessionId": "sid-%d" % i, "cwd": "/p/" + name}
            mon.enrich.tmux[ident] = {"session": "s-" + name}
        pane = {"attached": 0, "windows": 1, "w": 80, "h": 24, "cmd": "claude"}
        mon.enrich.sessions = {"s-" + n: dict(pane) for n in ("idle-a", "work-b", "wait-c")}
        # two more idle sessions, one idle for longer and one that only just went idle
        for i, (name, ago) in enumerate((("idle-old", 9000), ("idle-new", 30)), start=5):
            ident = (100 + i * 10, 100 + i)
            procs.append(P(ident[0], 60, "claude", start=ident[1]))
            mon.enrich.sess[ident] = {"status": "idle", "statusUpdatedAt": (5000 - ago) * 1000,
                                      "sessionId": "sid-%d" % i, "cwd": "/p/" + name}
            mon.enrich.tmux[ident] = {"session": "s-" + name}
            mon.enrich.sessions["s-" + name] = dict(pane)
        mon.enrich.sessions["a-plain-shell"] = dict(pane, cmd="bash")
        mon.attr.update(FakeTable(procs), 5000.0)
        got = mon._export_sessions()
        self.assertEqual([x["name"] for x in got],
                         ["s-wait-c", "s-work-b", "s-idle-new", "s-idle-a", "s-idle-old", "a-plain-shell"])
        self.assertIsNone(got[-1]["agent"])
        self.assertNotIn("_key", got[0])

    def test_the_agent_in_the_pane_you_land_on_speaks_for_its_terminal(self):
        from pchealth.sampler import Monitor
        mon = Monitor(config.load("/nonexistent"), persist=False)
        procs = self.base()
        for pid, start, pane in ((110, 101, "%1"), (120, 102, "%2")):               # two conversations, two windows, one session
            procs.append(P(pid, 60, "claude", start=start))
            mon.enrich.sess[(pid, start)] = {"status": "idle", "statusUpdatedAt": 4000 * 1000, "sessionId": "sid-%d" % pid, "cwd": "/p"}
            mon.enrich.tmux[(pid, start)] = {"session": "both", "pane": pane}
        info = {"attached": 0, "windows": 2, "w": 80, "h": 24, "cmd": "claude"}
        mon.attr.update(FakeTable(procs), 5000.0)
        for landing, pid in (("%2", 120), ("%1", 110), ("%2", 120)):
            mon.enrich.sessions = {"both": dict(info, pane=landing)}
            (row,) = mon._export_sessions()
            self.assertEqual(row["agent"], "claude:%d:%d" % (pid, 101 if pid == 110 else 102), landing)

    def test_rows_keep_their_order_whatever_the_numbers_do(self):
        def table(busy_pid):
            return FakeTable(self.base() + [P(100, 60, "claude"), P(200, 60, "claude", start=300),
                                            P(101, 100, "rustc", d=900 if busy_pid == 100 else 0),
                                            P(201, 200, "rustc", start=301, d=900 if busy_pid == 200 else 0)])
        self.e.sess[(100, 100)] = {"status": "idle", "sessionId": "sid-a", "cwd": "/p/zebra"}
        self.e.sess[(200, 300)] = {"status": "busy", "sessionId": "sid-b", "cwd": "/p/apple"}
        orders = []
        for i, busy in enumerate((100, 200, 100)):
            self.a.update(table(busy), 5000.0 + i)
            orders.append([r["label"] for r in self.a.export_agents(5000.0 + i)])
        self.assertEqual(orders, [["apple", "zebra"]] * 3)

    def test_one_shot_claude_runs_are_not_listed(self):
        t = FakeTable(self.base() + [P(100, 60, "claude", start=int(999.0 * procfs.CLK_TCK))], boot_s=1000.0)
        self.a.update(t, 5000.0)                         # one second old, no session file: e.g. `claude --version`
        self.assertEqual(self.a.export_agents(5000.0), [])
        self.a.update(FakeTable(self.base(), boot_s=1002.0), 5002.0)
        self.assertEqual(self.a.export_ended(5002.0), [])

    def test_codex_daemon_is_not_an_agent_until_it_has_a_session(self):
        procs = self.base() + [P(500, 50, "codex"), P(600, 50, "codex", start=200)]
        t = FakeTable(procs)
        self.a.update(t, 5000.0)
        self.assertEqual(self.a.export_agents(5000.0), [])                   # nothing known about them yet
        self.e.root[(500, 100)] = {"cwd": "/p/puzzle", "mode": "daemon"}
        self.e.root[(600, 200)] = {"cwd": "/p/puzzle", "mode": "tui"}
        self.e.codex[(500, 100)] = {"state": "unknown", "text": "Running", "has_rollout": False}
        self.a.update(t, 5001.0)
        self.assertEqual([(r["pid"], r["kind"]) for r in self.a.export_agents(5001.0)], [(600, "codex")])
        self.e.codex[(500, 100)] = {"state": "working", "text": "Working", "has_rollout": True, "since": 1.0}
        self.a.update(t, 5002.0)
        self.assertEqual(sorted(r["pid"] for r in self.a.export_agents(5002.0)), [500, 600])
        self.e.codex[(500, 100)] = {"state": "unknown", "text": "Running", "has_rollout": False}
        self.a.update(t, 5003.0)
        self.a.update(FakeTable(self.base() + [P(600, 50, "codex", start=200)]), 5004.0)   # the daemon stops
        self.assertEqual(self.a.export_ended(5004.0), [])

    def test_registry_roundtrip(self):
        procs = self.base() + [P(100, 60, "claude")]
        self.e.sess[(100, 100)] = {"status": "idle", "sessionId": "sid-a", "cwd": "/p/game"}
        self.a.update(FakeTable(procs), 5000.0)
        self.a.update(FakeTable(self.base()), 5050.0)
        other = attribution.Attribution(attribution.Enrich(), TH)
        other.load_registry(json.loads(json.dumps(self.a.dump_registry())), 6000.0)
        self.assertEqual(other.known[("sid", "sid-a")]["label"], "game")
        self.assertEqual(len(other.export_ended(6000.0)), 1)
        other.load_registry({"sessions": "nonsense"}, 6000.0)


def reply(mid, usage, text="hi", extra=""):
    """One transcript line of an assistant message, shaped like Claude Code writes them."""
    return json.dumps({"parentUuid": "p", "isSidechain": False, "type": "assistant",
                       "message": {"model": "claude-opus-5-5", "id": mid, "type": "message", "role": "assistant",
                                   "content": [{"type": "text", "text": text + extra}], "stop_reason": "end_turn",
                                   "usage": usage},
                       "uuid": "u", "timestamp": "2026-10-04T00:00:00Z"}, separators=(",", ":")) + "\n"


def usage(i, cw, cr, o):
    return {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": o,
            "iterations": [{"input_tokens": 999, "output_tokens": 999}], "service_tier": "standard"}


class Tokens(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sid = "0a1b2c3d-0000-4000-8000-000000000001"
        self.proj = os.path.join(self.dir.name, "-home-u-Projects-game")
        os.makedirs(self.proj)
        self.path = os.path.join(self.proj, self.sid + ".jsonl")

    def append(self, text, path=None):
        with open(path or self.path, "a") as f:
            f.write(text)

    def test_a_message_split_over_lines_is_counted_once(self):
        u = usage(2, 100, 5000, 40)
        self.append(reply("msg_01", u) * 3 + reply("msg_02", usage(3, 0, 5100, 60)))
        st = tokens.FileState()
        tokens.scan_file(self.path, st)
        self.assertEqual(st.sums, [5, 100, 10100, 100])
        self.assertEqual(st.ctx, 3 + 0 + 5100)                 # what the newest message had in context

    def test_lines_that_only_quote_usage_are_ignored(self):
        tool_result = json.dumps({"type": "user", "message": {"role": "user", "content": "x"},
                                  "toolUseResult": {"usage": usage(7, 7, 7, 7), "note": '"type":"assistant"'}},
                                 separators=(",", ":")) + "\n"
        quoting = reply("msg_03", usage(1, 0, 0, 10), extra=' the record looks like "usage":{"input_tokens":5,"output_tokens":5}')
        self.append(tool_result + quoting + '{"type":"cost-state","totalCostUSD":1.0}\n')
        st = tokens.FileState()
        tokens.scan_file(self.path, st)
        self.assertEqual(st.sums, [1, 0, 0, 10])

    def test_only_appended_bytes_are_read_and_half_lines_wait(self):
        self.append(reply("msg_01", usage(1, 10, 100, 5)))
        st = tokens.FileState()
        self.assertGreater(tokens.scan_file(self.path, st), 0)
        self.assertEqual(tokens.scan_file(self.path, st), 0)                 # nothing new
        whole = reply("msg_02", usage(2, 20, 200, 6))
        self.append(whole[:60])                                              # a line still being written
        tokens.scan_file(self.path, st)
        self.assertEqual(st.sums, [1, 10, 100, 5])
        self.append(whole[60:])
        tokens.scan_file(self.path, st)
        self.assertEqual(st.sums, [3, 30, 300, 11])

    def test_same_message_continued_after_a_restart_is_not_doubled(self):
        u = usage(1, 10, 100, 5)
        self.append(reply("msg_01", u))
        st = tokens.FileState()
        tokens.scan_file(self.path, st)
        st = tokens.FileState.load(json.loads(json.dumps(st.dump())))        # monitor restarted
        self.append(reply("msg_01", u) + reply("msg_02", u))
        tokens.scan_file(self.path, st)
        self.assertEqual(st.sums, [2, 20, 200, 10])

    def test_rewritten_file_starts_over(self):
        self.append(reply("msg_01", usage(1, 0, 0, 5)) * 1 + reply("msg_02", usage(1, 0, 0, 5)))
        st = tokens.FileState()
        tokens.scan_file(self.path, st)
        with open(self.path, "w") as f:
            f.write(reply("msg_09", usage(4, 0, 0, 4)))
        tokens.scan_file(self.path, st)
        self.assertEqual(st.sums, [4, 0, 0, 4])
        self.assertEqual(tokens.scan_file(self.path + ".missing", st), -1)

    def test_session_totals_include_subagents(self):
        self.append(reply("msg_01", usage(1, 10, 100, 5)))
        sub = os.path.join(self.proj, self.sid, "subagents")
        os.makedirs(sub)
        self.append(reply("msg_50", usage(2, 20, 200, 7)), os.path.join(sub, "agent-abc.jsonl"))
        self.append("{}", os.path.join(sub, "agent-abc.meta.json"))
        c = tokens.Counter(self.dir.name)
        got = c.update({self.sid: "/home/u/Projects/game", "ffffffff-0000-4000-8000-00000000dead": "/nowhere"}, 1000.0)
        self.assertEqual(got, {self.sid: {"in": 33, "cached": 300, "out": 12, "total": 345, "ctx": 111}})
        again = tokens.Counter(self.dir.name)
        again.load(json.loads(json.dumps(c.dump())))
        self.append(reply("msg_02", usage(0, 0, 150, 3)))
        self.assertEqual(again.update({self.sid: ""}, 1001.0)[self.sid]["total"], 345 + 153)
        again.load({"broken": 1})
        self.assertEqual(again.sessions, {})

    def test_reading_is_budgeted(self):
        self.append(reply("msg_01", usage(1, 0, 0, 1), text="x" * 2000))
        c = tokens.Counter(self.dir.name)
        self.assertEqual(c.update({self.sid: ""}, 1.0, budget_bytes=0), {})     # nothing shown until fully read
        self.assertIn(self.sid, c.update({self.sid: ""}, 2.0))

    def test_transcript_path_rejects_odd_ids(self):
        self.assertIsNone(tokens.transcript_path("/x", "../../etc/passwd", self.dir.name))
        self.assertIsNone(tokens.transcript_path("/x", None, self.dir.name))
        self.append("")
        self.assertEqual(tokens.transcript_path("/home/u/Projects/game", self.sid, self.dir.name), self.path)
        self.assertEqual(tokens.transcript_path("/moved/elsewhere", self.sid, self.dir.name), self.path)

    def test_codex_totals(self):
        tail = (b'{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":900,'
                b'"cached_input_tokens":700,"output_tokens":50,"reasoning_output_tokens":10,"total_tokens":950},'
                b'"last_token_usage":{"input_tokens":120,"cached_input_tokens":100,"output_tokens":5}}}}\n')
        self.assertEqual(agents_codex.rollout_tokens(tail), {"in": 200, "cached": 700, "out": 50, "total": 950, "ctx": 120})
        self.assertIsNone(agents_codex.rollout_tokens(b"nothing here"))


class Conversation(unittest.TestCase):
    """Earlier turns for the view-only page: what was typed, what was answered, one line per action. Nothing else."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.jsonl")

    @staticmethod
    def user(text, **extra):
        return dict({"type": "user", "timestamp": "2026-10-05T18:00:00Z", "message": {"role": "user", "content": text}}, **extra)

    @staticmethod
    def agent(*blocks):
        return {"type": "assistant", "timestamp": "2026-10-05T18:00:05Z", "message": {"role": "assistant", "content": list(blocks)}}

    def write(self, rows, tail=b""):
        with open(self.path, "wb") as f:
            for row in rows:
                f.write((row if isinstance(row, bytes) else self.line(row)) + b"\n")
            f.write(tail)

    @staticmethod
    def line(row):
        return json.dumps(row, separators=(",", ":")).encode()          # compact, as Claude Code writes it

    def test_what_is_read_out_and_what_is_not(self):
        secret = "hunter2-do-not-show"
        self.write([
            {"type": "mode", "mode": "auto"},
            self.user("fix the header"),
            self.agent({"type": "thinking", "thinking": secret}, {"type": "text", "text": "On it."},
                       {"type": "tool_use", "name": "Bash", "input": {"command": "make  test", "description": "Run the tests"}}),
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": secret}]}},
            self.user("<local-command-caveat>" + secret + "</local-command-caveat>"),
            self.user("<command-name>/clear</command-name>\n<command-args></command-args>"),
            self.user([{"type": "text", "text": "and the dock<system-reminder>" + secret + "</system-reminder>"}, {"type": "image", "source": {}}]),
            self.user(secret, isMeta=True),
            self.user(secret, isSidechain=True),
            self.agent({"type": "tool_use", "name": "mcp__docs__read", "input": {"file_path": "/x/y.md"}}),
        ])
        got = conversation.page(self.path)
        self.assertEqual([(i["r"], i["t"]) for i in got["items"]], [
            ("you", "fix the header"), ("agent", "On it."), ("did", "Bash: Run the tests"), ("you", "/clear"),
            ("you", "and the dock\n[image]"), ("did", "read: /x/y.md")])
        self.assertEqual(got["items"][0]["at"], "2026-10-05T18:00:00Z")
        self.assertNotIn(secret, json.dumps(got))
        self.assertEqual(got["start"], 0)                         # the beginning was reached

    def test_pages_go_back_without_losing_or_repeating_a_line(self):
        rows = [self.user("message %d %s" % (i, "x" * 3000)) for i in range(900)]      # about 2.8 MB
        self.write(rows)
        seen, before, pages = [], None, 0
        while True:
            got = conversation.page(self.path, before=before)
            seen = [i["t"].split()[1] for i in got["items"]] + seen
            pages += 1
            if got["start"] == 0:
                break
            self.assertLess(got["start"], os.path.getsize(self.path) if before is None else before)
            before = got["start"]
        self.assertEqual(seen, [str(i) for i in range(900)])
        self.assertGreater(pages, 2)

    def test_new_lines_since_the_last_look(self):
        self.write([self.user("one")])
        first = conversation.page(self.path)
        self.assertEqual(conversation.page(self.path, after=first["end"])["items"], [])
        with open(self.path, "ab") as f:
            f.write(self.line(self.user("two")) + b"\n" + self.line(self.user("three"))[:20])
        more = conversation.page(self.path, after=first["end"])
        self.assertEqual([i["t"] for i in more["items"]], ["two"])              # the unfinished line waits
        with open(self.path, "ab") as f:
            f.write(self.line(self.user("three"))[20:] + b"\n")
        self.assertEqual([i["t"] for i in conversation.page(self.path, after=more["end"])["items"]], ["three"])
        # an offset in the middle of a line costs only that line
        self.assertEqual([i["t"] for i in conversation.page(self.path, after=5)["items"]], ["two", "three"])

    def test_huge_and_broken_lines_are_stepped_over(self):
        big = self.line(self.user("y" * (3 << 20)))                           # an image or bulky output: 3 MB
        self.write([self.user("before"), big, b"{not json", self.user("after")], tail=b'{"type":"user","mess')
        got = conversation.page(self.path)
        texts = [i["t"] for i in got["items"]]
        while got["start"] > 0:
            got = conversation.page(self.path, before=got["start"])
            texts = [i["t"] for i in got["items"]] + texts
        self.assertEqual(texts, ["before", "after"])
        self.assertIsNone(conversation.page(os.path.join(self.dir.name, "missing.jsonl")))

    def test_long_messages_are_cut(self):
        self.write([self.agent({"type": "text", "text": "z" * 50000})])
        text = conversation.page(self.path)["items"][0]["t"]
        self.assertLess(len(text), conversation.MAX_TEXT + 10)
        self.assertTrue(text.endswith("…"))


class EmdashTerminals(unittest.TestCase):
    """Which tmux sessions the Emdash app started: those may be closed from the page, but not renamed."""

    def check(self, name, clients, procs, seen=()):
        import types
        from pchealth.sampler import Monitor
        me = types.SimpleNamespace(table=types.SimpleNamespace(procs=procs), _emdash_seen=set(seen))
        return Monitor._is_emdash(me, name, clients), me._emdash_seen

    def test_by_name_by_window_and_by_memory(self):
        P = lambda pid, comm, ppid: Proc(pid, comm, "S", ppid, 0, 0, 0, 0, 0)
        emdash = {50: P(50, "tmux: client", 40), 40: P(40, "sh", 30), 30: P(30, "Emdash", 2), 2: P(2, "systemd", 1)}
        plain = {60: P(60, "tmux: client", 61), 61: P(61, "bash", 62), 62: P(62, "ptyxis", 2), 2: P(2, "systemd", 1)}
        self.assertEqual(self.check("web-store-3f9c2e7a1b", [], {})[0], True)            # the name Emdash gives
        self.assertEqual(self.check("work", [50], emdash), (True, {"work"}))             # an Emdash window is attached
        self.assertEqual(self.check("work", [60], plain), (False, set()))                # an ordinary terminal window
        self.assertEqual(self.check("work", [], {}, seen=["work"])[0], True)             # Emdash was attached earlier
        self.assertEqual(self.check("work", [], {})[0], False)
        self.assertEqual(self.check("work", [999], {})[0], False)                        # a client that has gone


class SavedTerminals(unittest.TestCase):
    """The file that says which terminals to bring back, and what the page is told about it."""

    A = "0a1b2c3d-1111-4222-8333-444455556666"
    B = "9f8e7d6c-aaaa-4bbb-8ccc-ddddeeeeffff"

    def rows(self):
        return [{"name": "work", "sid": self.A, "model": "opus[1m]", "folder": "/home/me/Projects/work"},
                {"name": "plain", "sid": "", "model": "", "folder": "/home/me"}]

    def test_file_roundtrip(self):
        raw = saved.dump(self.rows(), 1791330000.7)
        self.assertEqual(raw.decode().splitlines(), ["pch-saved-terminals\t1\t1791330000",
                                                     "work\t%s\topus[1m]\t/home/me/Projects/work" % self.A,
                                                     "plain\t-\t-\t/home/me"])
        self.assertEqual(saved.parse(raw), {"ts": 1791330000, "rows": self.rows()})

    def test_anything_odd_is_left_out(self):
        good = {"name": "work", "sid": self.A, "model": "", "folder": "/p"}
        for field, bad in (("name", "two words"), ("name", "-dash"), ("name", "$(id)"), ("name", "x" * 65), ("name", ""), ("name", "a.b"),
                           ("name", "web-store-3f9c2e7a1b"), ("sid", "not-an-id"), ("sid", self.A + ";id"),
                           ("sid", self.A.upper()), ("model", "a b"), ("model", "x';id;'"), ("model", "$(id)"),
                           ("folder", "relative"), ("folder", "/a#(id)"), ("folder", "/a\tb"), ("folder", "/a\nb"),
                           ("folder", "/" + "x" * 1024), ("folder", "")):
            row = dict(good, **{field: bad})
            self.assertFalse(saved.valid_row(row), (field, bad))
            self.assertEqual(saved.parse(saved.dump([row, good], 5))["rows"], [good], (field, bad))
        head = "pch-saved-terminals\t1\t5\n"
        self.assertEqual(saved.parse((head + "work\t-\t-\t/p\textra\nwork\t-\t-\n\nok\t-\t-\t/p\nok\t-\t-\t/q\n").encode())["rows"],
                         [{"name": "ok", "sid": "", "model": "", "folder": "/p"}])           # also: a name only once
        # Fields are cut the way the script's `read` cuts them, so both readers agree on a hand-edited line too.
        self.assertEqual(saved.parse((head + "work\t\t\t/home/me\n").encode())["rows"], [])                # two fields, not four
        self.assertEqual(saved.parse((head + "\twork\t\t-\t-\t/home/me\t\n").encode())["rows"],
                         [{"name": "work", "sid": "", "model": "", "folder": "/home/me"}])
        self.assertEqual(saved.parse((head + "work\t-\t-\t/home/me").encode())["rows"],                    # no line break at the end
                         [{"name": "work", "sid": "", "model": "", "folder": "/home/me"}])
        for field, bad in (("name", "work\n"), ("sid", self.A + "\n"), ("model", "opus\n")):
            self.assertFalse(saved.valid_row(dict(good, **{field: bad})), (field, bad))
        for not_ours in (b"", b"something else\n", b"pch-saved-terminals\t2\t5\n", b"pch-saved-terminals\t1\tsoon\n",
                         b"pch-saved-terminals\t1\t5\textra\n", b"\xff\xfe"):
            self.assertIsNone(saved.parse(not_ours), not_ours)
        many = [dict(good, name="t%d" % i) for i in range(50)]
        self.assertEqual(len(saved.parse(saved.dump(many, 5))["rows"]), saved.MAX_ROWS)

    def test_only_a_plain_file_of_our_own_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + "/saved-terminals.tsv"
            self.assertIsNone(saved.load(path))
            self.assertTrue(history.atomic_write(path, saved.dump(self.rows(), 7)))
            self.assertEqual(saved.load(path)["rows"], self.rows())
            os.replace(path, d + "/elsewhere")
            os.symlink(d + "/elsewhere", path)
            self.assertIsNone(saved.load(path))                    # a link to another file is not followed

    def test_saving_says_when_it_lets_go_of_a_conversation(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + "/saved-terminals.tsv"
            self.assertEqual(saved.save(path, self.rows()), (True, "2 terminals saved, 1 with a conversation"))
            self.assertEqual(saved.save(path, self.rows()), (True, "2 terminals saved, 1 with a conversation"))
            # Claude is no longer running in "work" (closed, or not up yet after a restore): it is saved empty, and said.
            now = [dict(self.rows()[0], sid="", model=""), self.rows()[1]]
            self.assertEqual(saved.save(path, now),
                             (True, "2 terminals saved, 0 with a conversation. Saved without the conversation it had before: work"))
            self.assertEqual(saved.save(path, now), (True, "2 terminals saved, 0 with a conversation"))
            self.assertEqual(saved.save(path, [])[0], False)                   # nothing running: the file is left alone
            self.assertEqual(saved.load(path)["rows"], now)
            # ...and when something could not be saved in full, it says what and why instead of only counting.
            self.assertEqual(saved.save(path, now, {"odd": ["plain"], "left": ["_tmp", "web-store-3f9c2e7a1b"]})[1],
                             "2 terminals saved, 0 with a conversation. Saved without its conversation, whose folder has a character "
                             "in its name that tmux would misread (such as #): plain. Not saved (a name that cannot be given back "
                             "to tmux, or more than 32 terminals): _tmp, web-store-3f9c2e7a1b")

    def test_which_conversation_is_in_which_terminal(self):
        panes = {"%1": {"tty": "/dev/pts/1", "session": "work", "window": 0, "index": 0, "active": True},
                 "%2": {"tty": "/dev/pts/2", "session": "work", "window": 1, "index": 0, "active": False},
                 "%3": {"tty": "/dev/pts/3", "session": "plain", "window": 0, "index": 0, "active": True},
                 "%4": {"tty": "/dev/pts/4", "session": "other", "window": 0, "index": 0, "active": True},
                 "%5": {"tty": "/dev/pts/5", "session": "web-store-3f9c2e7a1b", "window": 0, "index": 0, "active": True}}
        procs = {p.pid: p for p in (P(10, 1, "bash", tty=1), P(11, 10, "claude", start=111, tty=1),
                                    P(12, 11, "claude", start=112, tty=1),              # started by the one above
                                    P(20, 1, "bash", tty=2), P(21, 20, "claude", start=121, tty=2),
                                    P(30, 1, "bash", tty=3),
                                    P(40, 1, "bash", tty=4), P(41, 40, "claude", start=141, tty=4),
                                    P(50, 1, "claude", start=151, tty=9))}              # in no tmux pane at all
        panes["%6"] = {"tty": "/dev/pts/6", "session": "sharp", "window": 0, "index": 0, "active": True}
        panes["%7"] = {"tty": "/dev/pts/7", "session": "_hidden", "window": 0, "index": 0, "active": True}
        procs[61] = P(61, 1, "claude", start=161, tty=6)
        files = {(61, 161): {"sessionId": "22222222-2222-4222-8222-222222222222", "cwd": "/home/me/C#/game"},
                 (11, 111): {"sessionId": self.A, "cwd": "/home/me/Projects/work"},
                 (12, 112): {"sessionId": "11111111-1111-4111-8111-111111111111", "cwd": "/tmp/x"},
                 (21, 121): {"sessionId": self.B, "cwd": "/home/me/Projects/second"},
                 (41, 141): {"sessionId": "someone-else", "cwd": "/home/me"},            # not an id we would type
                 (50, 151): {"sessionId": self.B, "cwd": "/home/me"}}
        argv = {11: ["claude", "--model", "opus[1m]"]}
        real = saved.tmux.same_tty
        saved.tmux.same_tty = lambda pane_tty, tty_nr: pane_tty == "/dev/pts/%d" % tty_nr
        try:
            odd = set()
            found = saved.find_conversations(panes, procs, 1000, read_session=lambda pid, start: files.get((pid, start)),
                                             argv_of=lambda pid: argv.get(pid, ["claude"]), uid_of=lambda pid: 1000, odd=odd)
        finally:
            saved.tmux.same_tty = real
        # The pane you land on when joining "work" wins over its second window; "other" has nothing we can resume.
        self.assertEqual(found, {"work": {"sid": self.A, "folder": "/home/me/Projects/work", "model": "opus[1m]"}})
        self.assertEqual(odd, {"sharp"})              # a conversation, in a folder whose name cannot be handed to tmux
        left = []
        rows = saved.rows_for(panes, found, "/home/me", left)
        self.assertEqual(rows, [{"name": "other", "sid": "", "model": "", "folder": "/home/me"},
                                {"name": "plain", "sid": "", "model": "", "folder": "/home/me"},
                                {"name": "sharp", "sid": "", "model": "", "folder": "/home/me"},
                                {"name": "work", "sid": self.A, "model": "opus[1m]", "folder": "/home/me/Projects/work"}])
        self.assertEqual(left, ["_hidden", "web-store-3f9c2e7a1b"])            # names that are not saved at all

    # ---- kept automatically (saved.Keeper): one call is one look at tmux, `at` seconds into the story

    def row(self, name, sid="", folder="/home/me"):
        return {"name": name, "sid": sid, "model": "", "folder": folder}

    def look(self, keeper, kept, running, at, server="1:1", ids=None):
        ids = ids or {r["name"]: "$" + r["name"] for r in running}
        return keeper.rows(kept, running, ids, server, at)

    def test_kept_a_new_terminal_is_added_once_it_has_stayed(self):
        k = saved.Keeper()
        work, plain = self.row("work", self.A, "/p"), self.row("plain")
        self.assertEqual(self.look(k, [], [plain, work], 0), [])                # just seen: it may be gone in a moment
        self.assertEqual(self.look(k, [], [plain, work], 10), [])
        self.assertEqual(self.look(k, [], [plain, work], 25), [plain, work])
        self.assertEqual(self.look(k, [plain, work], [plain, work], 35), [plain, work])       # nothing to write

    def test_kept_nothing_is_forgotten_because_it_is_gone(self):
        work, plain = self.row("work", self.A, "/p"), self.row("plain")
        k = saved.Keeper()
        for at in (0, 30):
            self.look(k, [plain, work], [plain, work], at)
        # tmux is gone (a restart of the PC, a crash): every row stays, however long it lasts.
        for at in (40, 100, 5000):
            self.assertEqual(self.look(k, [plain, work], [], at, server=None), [plain, work])
        # A new tmux server, and only one new terminal in it so far: the saved ones are still there to restore.
        new = self.row("new")
        for at in (5010, 5040, 5100):
            got = self.look(k, [plain, work], [new], at, server="2:9")
        self.assertEqual(got, [new, plain, work])
        # Restoring starts them under the new server; one that was started and taken down at once is not "closed".
        self.assertEqual(self.look(k, got, [new, plain], 5110, server="2:9"), [new, plain, work])
        self.assertEqual(self.look(k, got, [new], 5200, server="2:9"), [new, plain, work])
        self.assertEqual(self.look(k, got, [new], 5300, server="2:9"), [new, plain, work])

    def test_kept_a_terminal_closed_while_tmux_runs_is_taken_off(self):
        work, plain, third = self.row("work", self.A, "/p"), self.row("plain"), self.row("third", self.B, "/q")
        kept = [plain, third, work]
        k = saved.Keeper()
        for at in (0, 30):
            self.look(k, kept, kept, at)
        self.assertEqual(self.look(k, kept, [plain, work], 40), [plain, work, third])       # gone a moment: not yet
        self.assertEqual(self.look(k, kept, [plain, work], 50), [plain, work, third])
        self.assertEqual(self.look(k, kept, [plain, work], 61), [plain, work])              # still gone, tmux still there
        # Seen ending just before tmux itself went (a shutdown): that is not a terminal someone closed.
        k = saved.Keeper()
        for at in (0, 30):
            self.look(k, kept, kept, at)
        self.assertEqual(self.look(k, kept, [plain], 40), [plain, third, work])
        self.assertEqual(self.look(k, kept, [], 41, server=None), kept)
        self.assertEqual(self.look(k, kept, [], 400, server=None), kept)
        self.assertEqual(self.look(k, kept, [self.row("new")], 500, server="2:9"), kept)

    def test_kept_the_conversation_in_a_terminal_is_followed(self):
        C = "33333333-3333-4333-8333-333333333333"
        work, plain = self.row("work", self.A, "/p"), self.row("plain")
        kept = [plain, work]
        k = saved.Keeper()
        for at in (0, 30):
            self.look(k, kept, kept, at)
        # /clear, or another conversation started there: saved at once, the terminal is known.
        fresh = self.row("work", self.B, "/p")
        self.assertEqual(self.look(k, kept, [plain, fresh], 40), [plain, fresh])
        # Claude is closed there, or not up yet: the conversation saved with the terminal stays.
        empty = self.row("work")
        self.assertEqual(self.look(k, [plain, fresh], [plain, empty], 50), [plain, fresh])
        # ...unless it now runs in another terminal: it is saved there, and only once.
        moved = self.row("plain", self.B, "/p")
        self.assertEqual(self.look(k, [plain, fresh], [moved, empty], 60), [moved, empty])
        # A conversation that is already saved is followed into a terminal that is new, without waiting.
        k = saved.Keeper()
        self.assertEqual(self.look(k, kept, [self.row("elsewhere", self.A, "/p")], 0), [self.row("elsewhere", self.A, "/p"), plain])
        # A new terminal with a conversation nobody saved waits like any new terminal.
        k = saved.Keeper()
        self.assertEqual(self.look(k, kept, [self.row("extra", C, "/q")], 0), kept)

    def test_kept_a_renamed_terminal_keeps_its_row(self):
        work, vps = self.row("work", self.A, "/p"), self.row("vps")
        kept = [vps, work]
        k = saved.Keeper()
        ids = {"work": "$0", "vps": "$1"}
        for at in (0, 30):
            self.look(k, kept, kept, at, ids=ids)
        # tmux knows it is the same session under another name: no "not running" in between, nothing left behind.
        server = self.row("server")
        self.assertEqual(self.look(k, kept, [server, work], 31, ids={"work": "$0", "server": "$1"}), [server, work])
        self.assertEqual(self.look(k, [server, work], [server, work], 90, ids={"work": "$0", "server": "$1"}), [server, work])

    def test_kept_never_more_rows_than_can_be_restored(self):
        k = saved.Keeper()
        many = [self.row("t%02d" % i) for i in range(40)]
        for at in (0, 30):
            got = self.look(k, [self.row("gone")], many, at)
        self.assertEqual(len(got), saved.MAX_ROWS)
        self.assertNotIn("gone", [r["name"] for r in got])                    # the running ones come first

    def test_one_terminal_can_be_taken_off_the_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + "/saved-terminals.tsv"
            self.assertFalse(saved.forget("work", path))                      # nothing saved
            history.atomic_write(path, saved.dump(self.rows(), 7))
            self.assertFalse(saved.forget("other", path))
            self.assertEqual(saved.load(path), {"ts": 7, "rows": self.rows()})
            self.assertTrue(saved.forget("work", path))
            self.assertEqual(saved.load(path)["rows"], self.rows()[1:])

    def test_the_monitor_keeps_the_file_in_step(self):
        from pchealth.sampler import Monitor
        work, plain = self.row("work", self.A, "/p"), self.row("plain", folder=os.path.expanduser("~"))

        def pane(n, name):
            return {"tty": "/dev/pts/%d" % n, "session": name, "window": 0, "index": 0, "active": True,
                    "id": "$%d" % n, "server": "86376:1791338460"}
        panes = {"%1": pane(1, "work"), "%2": pane(2, "plain")}
        talks = {"work": {"sid": self.A, "folder": "/p", "model": ""}}
        real = saved.find_conversations, saved.SETTLE_S
        saved.find_conversations, saved.SETTLE_S = (lambda panes, procs, uid: {n: t for n, t in talks.items()
                                                                          if any(p["session"] == n for p in panes.values())}), 0
        try:
            with tempfile.TemporaryDirectory() as d:
                path = d + "/" + saved.FILE_NAME
                # A one-off look at the PC (--once, the tests) never writes it.
                once = Monitor(config.load("/nonexistent"), state_dir=d, persist=False)
                self.assertFalse(once.autosave)
                cfg = dict(config.load("/nonexistent"), terminal_autosave=False)
                self.assertFalse(Monitor(cfg, state_dir=d).autosave)             # switched off in the settings file
                self.assertFalse(Monitor(dict(config.load("/nonexistent"), terminal_type="off"), state_dir=d).autosave)
                mon = Monitor(config.load("/nonexistent"), state_dir=d)
                self.assertTrue(mon.autosave)
                mon._read_saved()
                mon._keep_saved(panes)                                          # first seen
                mon._keep_saved(panes)                                          # still there: saved
                self.assertEqual(saved.load(path)["rows"], [plain, work])
                self.assertEqual(mon.enrich.saved["rows"], [plain, work])       # and the monitor has read it back
                before = os.stat(path).st_mtime_ns
                mon._keep_saved(panes)
                self.assertEqual(os.stat(path).st_mtime_ns, before)             # nothing changed: nothing written
                mon._keep_saved(None)                                           # tmux is gone
                mon._keep_saved(None)
                self.assertEqual(saved.load(path)["rows"], [plain, work])
        finally:
            saved.find_conversations, saved.SETTLE_S = real

    def test_the_page_is_told_about_the_at_login_switch(self):
        from pchealth.sampler import Monitor
        with tempfile.TemporaryDirectory() as d:
            mon = Monitor(config.load("/nonexistent"), state_dir=d, persist=False)
            mon.has_tmux = False                                  # nothing is asked of tmux here
            mon.at_login_unit = d + "/pc-health-restore.service"
            mon._job_tmux(0)
            self.assertIsNone(mon.w["at_login"])                  # this PC has no unit for it: no switch to show
            open(mon.at_login_unit, "w").close()
            mon._job_tmux(0)
            self.assertIs(mon.w["at_login"], False)
            open(mon.at_login_flag, "w").close()
            mon._job_tmux(0)
            self.assertIs(mon.w["at_login"], True)
            self.assertEqual(os.path.basename(mon.at_login_flag), "restore-at-login")

    def test_what_the_page_is_told(self):
        from pchealth.sampler import Monitor
        mon = Monitor(config.load("/nonexistent"), persist=False)
        verdict = mon._saved_verdict
        kept = {r["name"]: r for r in self.rows()}
        talks = {self.A}
        self.assertEqual(verdict("work", self.A, kept, talks), "yes")
        self.assertEqual(verdict("renamed", self.A, kept, talks), "yes")       # the conversation is followed, not the name
        self.assertEqual(verdict("work", self.B, kept, talks), "other")        # another conversation runs there now
        self.assertEqual(verdict("work", None, kept, talks), "stopped")        # the saved one is not running there
        self.assertEqual(verdict("plain", None, kept, talks), "yes")
        self.assertEqual(verdict("plain", self.B, kept, talks), "other")
        # An agent that cannot be saved (Codex, or a Claude not known yet) in a terminal saved empty: not "all saved".
        self.assertEqual(verdict("plain", None, kept, talks, True), "shell")
        self.assertEqual(verdict("work", None, kept, talks, True), "stopped")
        self.assertEqual(verdict("work", self.A, kept, talks, True), "yes")
        self.assertIsNone(verdict("new", self.B, kept, talks))
        self.assertIsNone(verdict("new", None, kept, talks))
        self.assertIsNone(mon._export_saved())                                # nothing saved: nothing to say
        mon.enrich.saved = {"ts": 1791330000, "rows": self.rows() + [{"name": "third", "sid": self.B, "model": "", "folder": "/p"}]}
        pane = {"attached": 0, "windows": 1, "w": 80, "h": 24, "cmd": "bash"}
        mon.enrich.sessions = {"plain": dict(pane)}
        self.assertEqual(mon._export_saved(), {"ts": 1791330000, "names": ["work", "plain", "third"], "gone": ["work", "third"]})
        self.assertEqual([(x["name"], x["saved"]) for x in mon._export_sessions()], [("plain", "yes")])
        with tempfile.TemporaryDirectory() as d:                              # the monitor follows the file as it changes
            mon.state_dir = d
            mon.enrich.saved = None
            mon._read_saved()
            self.assertIsNone(mon.enrich.saved)
            history.atomic_write(d + "/" + saved.FILE_NAME, saved.dump(self.rows(), 9))
            mon._read_saved()
            self.assertEqual(mon.enrich.saved, {"ts": 9, "rows": self.rows()})
            os.unlink(d + "/" + saved.FILE_NAME)
            mon._read_saved()
            self.assertIsNone(mon.enrich.saved)


class SubAgents(unittest.TestCase):
    """Which sub-agents an agent is running, told from the end of the records Claude Code keeps of them."""

    PROMPT = json.dumps({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "Find the thing"}}).encode()
    NOISE = json.dumps({"type": "attachment", "attachment": {"x": 1}}).encode()

    @staticmethod
    def said(*blocks, stop=None):
        msg = {"role": "assistant", "content": list(blocks),
               "usage": {"input_tokens": 3, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 1000, "output_tokens": 50}}
        if stop:
            msg["stop_reason"] = stop
        return json.dumps({"type": "assistant", "isSidechain": True, "message": msg}).encode()

    @staticmethod
    def use(name):
        return {"type": "tool_use", "name": name, "input": {"command": "cat ~/.ssh/CANARY-key"}}

    RESULT = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "CANARY out"}]}}).encode()
    TEXT = {"type": "text", "text": "Here is what I found."}
    THINK = {"type": "thinking", "thinking": "hm"}

    def test_the_end_of_a_record_says_whether_it_is_still_running(self):
        p, n, r = self.PROMPT, self.NOISE, self.RESULT
        for lines, want in (
                ([p], (False, "")),                                                         # only just asked
                ([p, n, self.said(self.THINK)], (False, "")),                               # thinking
                ([p, self.said(self.THINK), self.said(self.use("Bash"))], (False, "Bash")),   # waiting on a tool
                ([p, self.said(self.use("Bash")), r], (False, "")),                         # the tool answered: thinking again
                ([p, self.said(self.use("Bash")), r, self.said(self.TEXT, stop="end_turn")], (True, "")),      # its answer
                ([p, self.said(self.TEXT, stop="end_turn"), n], (True, "")),
                ([p, self.said(self.use("SubagentHandback"))], (True, "")),                 # handing its work back
                ([p, self.said(self.use("SubagentHandback")), r], (True, "")),
                ([p, self.said(self.use("StructuredOutput")), r], (True, "")),
                ([p, self.said(self.TEXT, stop="end_turn"), p], (False, "")),               # told something more: at it again
                ([p, self.said(self.use("rm -rf /; x"))], (False, "")),                     # not a plain tool name: not shown
                ([p, self.said(self.TEXT, stop="max_tokens")], (True, "")),
                ([b"not json", b"", b"[1, 2]"], (False, ""))):
            self.assertEqual(subagents.last_step(b"\n".join(lines) + b"\n"), want, lines[-1][:80])
        # A piece of a line at the start of what was read is not a line.
        cut = self.said(self.use("Read"))[40:] + b"\n" + self.said(self.TEXT, stop="end_turn") + b"\n"
        self.assertEqual(subagents.last_step(cut, whole=False), (True, ""))
        self.assertEqual(subagents.last_step(self.said(self.TEXT, stop="end_turn") + b"\n" + self.said(self.use("Read"))[:60], whole=True), (True, ""))
        # Its own context: what went into its newest reply. None yet when it has only just been asked.
        self.assertEqual(subagents.read_end(p + b"\n" + self.said(self.use("Bash")) + b"\n" + r + b"\n"), (False, "", 1203))
        self.assertEqual(subagents.read_end(p + b"\n"), (False, "", 0))

    def test_which_are_running_now(self):
        now = 2_000_000.0
        with tempfile.TemporaryDirectory() as d:
            transcript = d + "/proj/sid.jsonl"
            folder = d + "/proj/sid/subagents"
            os.makedirs(folder + "/workflows/w1")
            open(transcript, "w").close()

            def put(name, lines, about, written_ago, started_ago=600, where=folder):
                path = "%s/agent-%s.jsonl" % (where, name)
                with open(path, "wb") as f:
                    f.write(b"\n".join(lines) + b"\n")
                os.utime(path, (now - written_ago, now - written_ago))
                if about is not None:
                    with open(path[:-6] + ".meta.json", "w") as f:
                        json.dump(about, f)
                    os.utime(path[:-6] + ".meta.json", (now - started_ago, now - started_ago))
                return path

            p, r = self.PROMPT, self.RESULT
            a1 = put("a1", [p, self.said(self.use("Bash"))], {"agentType": "Explore", "description": "Find the thing", "spawnDepth": 1}, 20, 300)
            put("a2", [p, self.said(self.TEXT, stop="end_turn")], {"agentType": "Plan", "description": "Finished one"}, 10)
            put("a3", [p, self.said(self.use("Read"))], {"agentType": "Plan", "description": "Not written for an hour"}, 3600)
            put("a4", [p, self.said(self.use("Grep"))], {"agentType": "Plan", "description": "From before this run"}, 180)
            put("a5", [p], {"agentType": "workflow-subagent", "description": "Phase one\x07\n", "spawnDepth": 2, "workflowPhase": "x"}, 5, 60,
                where=folder + "/workflows/w1")
            put("a6", [p, self.said(self.use("Bash")), r], None, 30)                       # no note about it
            put("not-an-agent", [p], None, 5)
            os.rename(folder + "/agent-not-an-agent.jsonl", folder + "/other.jsonl")
            cache = {}
            got = subagents.running(transcript, now - 150, now, cache)                     # the agent itself started 150 s ago
            self.assertEqual([(x["id"], x["kind"], x["what"], x["depth"], x["doing"]) for x in got],
                             [("a6", "", "", 1, ""), ("a1", "Explore", "Find the thing", 1, "Bash"),
                              ("a5", "workflow-subagent", "Phase one", 2, "")])
            self.assertEqual([(x["started"], x["active"], x["ctx"]) for x in got],
                             [(0, now - 30, 1203), (now - 300, now - 20, 1203), (now - 60, now - 5, 0)])
            self.assertNotIn("path", got[0])
            self.assertNotIn("CANARY", json.dumps(got))                                    # nothing it read, wrote or ran
            self.assertEqual(subagents.running(transcript, now - 150, now, cache), got)    # nothing changed: same answer
            put("a1", [p, self.said(self.use("Bash")), r, self.said(self.TEXT, stop="end_turn")], None, 1)      # it finishes
            self.assertEqual([x["id"] for x in subagents.running(transcript, now - 150, now, cache)], ["a6", "a5"])
            self.assertEqual(subagents.running(d + "/proj/none.jsonl", 0, now, {}), [])     # a session with no sub-agents
            self.assertEqual(a1[-14:], "agent-a1.jsonl")

    def test_they_are_listed_with_their_agent(self):
        from pchealth.sampler import Monitor
        mon = Monitor(config.load("/nonexistent"), persist=False)
        procs = [P(1, 0, "systemd"), P(50, 1, "systemd"), P(60, 50, "bash"), P(110, 60, "claude", start=101), P(120, 60, "claude", start=102)]
        for pid, start in ((110, 101), (120, 102)):
            mon.enrich.sess[(pid, start)] = {"status": "busy", "statusUpdatedAt": 4000 * 1000, "sessionId": "sid-%d" % pid, "cwd": "/p"}
        mon.attr.update(FakeTable(procs), 5000.0)
        one = {"id": "a1", "kind": "Explore", "what": "Find the thing", "depth": 1, "started": 4900, "active": 4990, "doing": "Bash", "ctx": 1200}
        mon.enrich.subagents = {"sid-110": [one]}
        got = {a["pid"]: a["subagents"] for a in mon._with_extras(mon.attr.export_agents(5000.0))}
        self.assertEqual(got, {110: [one], 120: []})


class GitBranch(unittest.TestCase):
    """The branch shown in a terminal's summary, read from the folder's own git files (nothing is run)."""

    def test_branch_of_a_folder(self):
        with tempfile.TemporaryDirectory() as d:
            plain, repo, detached, tree = (os.path.join(d, n) for n in ("plain", "repo", "detached", "tree"))
            for folder in (plain, repo, detached, tree):
                os.makedirs(folder)
            os.makedirs(os.path.join(repo, ".git"))
            with open(os.path.join(repo, ".git", "HEAD"), "w") as f:
                f.write("ref: refs/heads/feature/glass\n")
            os.makedirs(os.path.join(detached, ".git"))
            with open(os.path.join(detached, ".git", "HEAD"), "w") as f:
                f.write("3d55515a9f0c2b7e8d1a4c6f0e9b8a7d6c5b4a39\n")
            os.makedirs(os.path.join(repo, ".git", "worktrees", "tree"))            # a worktree: .git is a file
            with open(os.path.join(repo, ".git", "worktrees", "tree", "HEAD"), "w") as f:
                f.write("ref: refs/heads/side\n")
            with open(os.path.join(tree, ".git"), "w") as f:
                f.write("gitdir: %s\n" % os.path.join(repo, ".git", "worktrees", "tree"))
            self.assertEqual(attribution.git_branch(plain, 100.0), "")
            self.assertEqual(attribution.git_branch(repo, 100.0), "feature/glass")
            self.assertEqual(attribution.git_branch(detached, 100.0), "3d55515a")
            self.assertEqual(attribution.git_branch(tree, 100.0), "side")
            with open(os.path.join(repo, ".git", "HEAD"), "w") as f:
                f.write("ref: refs/heads/main\n")
            self.assertEqual(attribution.git_branch(repo, 105.0), "feature/glass")       # remembered for a few seconds
            self.assertEqual(attribution.git_branch(repo, 130.0), "main")


class Settings(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return config.load(f.name)

    def test_defaults_keep_terminals_on_this_pc(self):
        cfg = config.load("/nonexistent/config.toml")
        self.assertEqual((cfg["terminal_view"], cfg["terminal_type"], cfg["access_code"]), ("local", "local", ""))
        self.assertTrue(cfg["terminal_socket"].endswith("/pc-health-ttyd.sock"))

    def test_owner_may_open_terminals_to_the_home_network_without_a_code(self):
        cfg = self.load('terminal_view = "lan"\nterminal_type = "lan"\naccess_code = ""\n')
        self.assertEqual((cfg["terminal_view"], cfg["terminal_type"]), ("lan", "lan"))

    def test_saved_terminals_are_kept_up_to_date_unless_switched_off(self):
        self.assertIs(config.load("/nonexistent/config.toml")["terminal_autosave"], True)
        self.assertIs(self.load("terminal_autosave = false\n")["terminal_autosave"], False)
        self.assertIs(self.load('terminal_autosave = "no"\n')["terminal_autosave"], True)       # not a yes or no: ignored

    def test_nonsense_falls_back_to_the_safe_side(self):
        cfg = self.load('terminal_view = "everyone"\nterminal_type = 5\nprotect = "nothing"\nlisten = "internet"\nunknown = 1\n')
        self.assertEqual((cfg["terminal_view"], cfg["terminal_type"], cfg["protect"], cfg["listen"]),
                         ("local", "local", "page", "local"))
        self.assertNotIn("unknown", cfg)
        self.assertIn("config_error", self.load("this is not toml ["))


def snap(**over):
    s = {"ts": 100_000, "cpu": {"threads": 12, "wait60_pct": 2.0, "load1": 1.0},
         "mem": {"total_b": 31 * GB, "avail_b": 20 * GB, "tmpfs_b": 0, "user_stall_pct": 0.0},
         "swap": {"total_b": 40 * GB, "used_b": 0, "in_bps": 0}, "io": {"stall60_pct": 0.0},
         "gpu": {"ok": True, "vram_used_b": GB, "vram_total_b": 16 * GB, "procs": [], "oom_15m": 0},
         "disks": [{"mount": "/", "label": "Main disk", "is_ram": False, "total_b": 422 * GB, "avail_b": 100 * GB,
                    "used_pct": 76.0, "eta_s": None, "loss_bps": None, "change_10m_b": None}],
         "temps": [], "fans": [], "agents": [], "leftovers": [], "top": {"cpu": [], "mem": []},
         "folders": {"items": []}, "scratch": [], "crash": {}, "last_oom_kill": None}
    s.update(over)
    return s


class Alerts(unittest.TestCase):
    def ids(self, s):
        return {f["id"]: f["level"] for f in alerts.findings(s, TH)}

    def test_healthy_machine_has_no_findings(self):
        self.assertEqual(self.ids(snap()), {})

    def test_this_weeks_incidents(self):
        s = snap()
        s["disks"][0].update(avail_b=14 * GB, used_pct=97.0, eta_s=7 * 3600, loss_bps=2.5 * GB / 3600)
        s["mem"].update(avail_b=int(1.2 * GB), tmpfs_b=12 * GB, user_stall_pct=40.0)
        s["swap"]["used_b"] = 39 * GB
        s["cpu"]["wait60_pct"] = 62.0
        s["gpu"].update(vram_used_b=int(15.9 * GB), oom_15m=34)
        s["crash"] = {"core_b": 27 * GB, "cores": 5, "newest_core": {"program": "UnrealEditor", "ts": 99_000},
                      "cmd": "sudo rm /var/lib/apport/coredump/core.*"}
        s["last_oom_kill"] = {"ts": 99_900, "name": "UnrealEditor"}
        s["leftovers"] = [{"id": "300:1", "kind": "orphan", "level": "crit", "from": "demo-game", "from_ended": 100_000 - 2 * 86400,
                           "what": "Headless browser", "age_s": 2 * 86400, "cpu_avg_cores": 7.0, "mem_b": 600 * MB,
                           "cmd": "kill 300"}]
        got = self.ids(s)
        self.assertEqual(got.get("disk_low:/"), "warn")            # 14 GB: warning; critical is under 12 GB
        for fid in ("disk_eta:/", "mem_low", "mem_pressure", "ram_files", "swap_high", "cpu_wait",
                    "vram", "gpu_oom", "crash_dumps", "oom_kill", "leftover:300:1"):
            self.assertEqual(got.get(fid), "crit", fid)
        text = " ".join(f["msg"] for f in alerts.findings(s, TH))
        self.assertIn("full in about 7 hours", text)
        self.assertIn("62% of the time", text)
        self.assertIn("7 of 12 cores", text)
        self.assertIn("ended 2 days ago", text)

    def test_hold_and_clear(self):
        eng = alerts.Engine()
        f = [{"id": "x", "level": "warn", "msg": "m", "hold_s": 60, "section": "s"}]
        eng.evaluate(0, [])                       # engine start
        self.assertEqual(eng.evaluate(100, f), [])              # not held long enough yet
        self.assertEqual(eng.evaluate(130, f), [])
        self.assertEqual(len(eng.evaluate(165, f)), 1)          # held for 60 s
        self.assertEqual(len(eng.evaluate(170, [])), 1)         # a brief absence does not clear it
        self.assertEqual(eng.evaluate(170 + alerts.CLEAR_S + 1, []), [])

    def test_current_picture_shows_at_once_after_start(self):
        eng = alerts.Engine()
        f = [{"id": "x", "level": "crit", "msg": "m", "hold_s": 600, "section": "s"}]
        self.assertEqual(len(eng.evaluate(1000, f)), 1)

    def test_events_follow_alerts(self):
        eng, ev = alerts.Engine(), history.Events()
        f = [{"id": "x", "level": "warn", "msg": "m", "hold_s": 0, "section": "s"}]
        eng.evaluate(1000, f, ev)
        self.assertIsNone(ev.items[0]["end"])
        eng.evaluate(1000 + alerts.CLEAR_S + 5, [], ev)
        self.assertIsNotNone(ev.items[0]["end"])

    def test_headline(self):
        self.assertEqual(alerts.headline([]), ("ok", "Everything looks healthy"))
        self.assertEqual(alerts.headline([{"level": "warn"}])[1], "1 thing needs attention")
        self.assertEqual(alerts.headline([{"level": "warn"}, {"level": "crit"}]), ("crit", "2 things need attention"))


class Folders(unittest.TestCase):
    def test_program_names(self):
        f = folders.program_from_dump
        self.assertEqual(f("core._home_u_Applications_UnrealEngine_5_8_3_Engine_Binaries_Linux_UnrealEditor.1000.7549b934.1591232.38574585"),
                         "UnrealEditor")
        self.assertEqual(f("_usr_bin_ffmpeg.1000.crash"), "ffmpeg")

    def test_watch_list_and_sizes(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d + "/proj/target/debug")
            os.makedirs(d + "/proj/Game/Saved")
            os.makedirs(d + "/proj/src")
            with open(d + "/proj/target/debug/big", "wb") as f:
                f.write(b"x" * 300_000)
            watch = folders.build_watch_list([d])
            self.assertIn(d + "/proj/target", watch)
            self.assertIn(d + "/proj/Game/Saved", watch)
            self.assertNotIn(d + "/proj/src", watch)
            from pchealth import dirscan
            size, n, partial = dirscan.size_of(d + "/proj/target")
            self.assertGreaterEqual(size, 300_000)
            self.assertFalse(partial)
            self.assertIsNone(dirscan.size_of(d + "/missing"))

    def test_moved_or_deleted_folders_drop_off_the_list(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d + "/kept")
            os.makedirs(d + "/elsewhere")
            os.symlink(d + "/elsewhere", d + "/moved")            # e.g. target -> a folder on another drive
            sc = folders.Scanner([])
            for name in ("kept", "moved", "deleted"):
                sc._record(d + "/" + name, 5 * GB, False, 1000)
            sc.first_due = 10 ** 12                               # no scan in this test
            sc.tick(2000, 100 * GB, False)
            self.assertEqual(sorted(sc.sizes), [d + "/kept"])

    def test_scanner_records_growth(self):
        sc = folders.Scanner([])
        sc._record("/home/u/x/target", 10 * GB, False, 1000)
        sc._record("/home/u/x/target", 14 * GB, False, 1000 + 7200)
        out = sc.export(9000)
        self.assertEqual(out["items"][0]["change_b"], 4 * GB)
        sc2 = folders.Scanner([])
        sc2.load(json.loads(json.dumps(sc.dump())))
        self.assertEqual(sc2.export(9000)["items"][0]["b"], 14 * GB)
        sc2.set_size("/home/u/x/target", 0, 9000)                  # e.g. deleted: the list must not keep the old size
        self.assertEqual(sc2.export(9000)["items"], [])
        sc2.set_size("/never/scanned", 5 * GB, 9000)               # only corrects folders already on the list
        self.assertEqual(sc2.export(9000)["items"], [])


if __name__ == "__main__":
    unittest.main()
