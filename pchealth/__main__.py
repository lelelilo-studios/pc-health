"""Command line:  python3 -m pchealth [serve | --once | bench | url | selfcheck | save-terminals | forget-terminal NAME]"""

import json
import os
import signal
import sys
import time

from . import config, __version__

USAGE = """PC-health %s
  python3 -m pchealth serve       run the monitor and its web page (default)
  python3 -m pchealth --once      print one snapshot as JSON and exit
  python3 -m pchealth bench       time each part of a sample
  python3 -m pchealth url         print the addresses of the page
  python3 -m pchealth selfcheck   say which data sources work
  python3 -m pchealth save-terminals   note the tmux terminals running now, to bring them back after a reboot
  python3 -m pchealth forget-terminal NAME   take one terminal off that list
""" % __version__


def _monitor(persist=False):
    from .sampler import Monitor
    return Monitor(config.load(), persist=persist)


def _warm(mon, seconds=2.5):
    """Run the worker jobs and two samples, so rates and per-agent details are filled in."""
    from .sampler import JOBS
    for name, _, _ in JOBS:
        if name in ("folders", "persist"):
            continue
        try:
            getattr(mon, "_job_" + name)(time.time())
        except Exception as e:
            mon.sources[name] = "error: %s: %s" % (type(e).__name__, e)
    mon.tick()
    time.sleep(seconds)
    for name in ("env", "sessions", "mem", "groups", "codex", "suspects", "scratch", "models", "tokens"):
        try:
            getattr(mon, "_job_" + name)(time.time())
        except Exception as e:
            mon.sources[name] = "error: %s: %s" % (type(e).__name__, e)
    mon.tick()


def cmd_once():
    mon = _monitor()
    _warm(mon)
    json.dump(mon.snapshot, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


def cmd_bench():
    mon = _monitor()
    _warm(mon, 1.0)
    from .sampler import JOBS
    rows = []
    t0 = time.process_time()
    n = 20
    for _ in range(n):
        mon.tick()
    rows.append(("sample (tick)", (time.process_time() - t0) / n * 1000))
    for name, _, _ in JOBS:
        if name in ("folders", "persist", "klog", "gpu", "gpu_procs"):
            continue
        t0 = time.process_time()
        getattr(mon, "_job_" + name)(time.time())
        rows.append(("job " + name, (time.process_time() - t0) * 1000))
    for label, ms in rows:
        print("%-18s %7.2f ms CPU" % (label, ms))
    print("snapshot size      %7d bytes" % len(mon.snapshot_bytes))
    print("resident memory    %7.1f MB" % (mon.snapshot["self"]["rss_b"] / 1048576))
    return 0


def cmd_url():
    from .httpd import page_urls
    cfg = config.load()
    for u in page_urls(cfg):
        print(u)
    return 0


def cmd_selfcheck():
    mon = _monitor()
    _warm(mon, 1.0)
    try:
        mon._job_klog(time.time())
    except Exception as e:
        mon.sources["klog"] = "error: " + type(e).__name__
    bad = 0
    for name, status in sorted(mon.sources.items()):
        ok = status in ("ok", "none running")
        bad += not ok
        print("%-10s %s" % (name, status))
    s = mon.snapshot
    print("agents     %d found" % len(s["agents"]))
    print("drives     %d found" % len(s["disks"]))
    return 1 if bad else 0


def cmd_serve():
    from .httpd import make_server, page_urls
    from .sampler import Monitor
    cfg = config.load()
    mon = Monitor(cfg)
    server = make_server(cfg, mon)
    mon.start()

    def stop(_sig, _frm):
        mon.stop.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    import threading
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 1.0}, name="pch-http", daemon=True).start()
    if sys.stdout.isatty():
        print("PC-health is running. Open:")
        for u in page_urls(cfg):
            print("  " + u)
    while not mon.stop.is_set():
        mon.stop.wait(1.0)
    server.shutdown()
    mon.shutdown()
    return mon.exit_code


def cmd_save_terminals():
    """Run by the terminal service when the page asks to save the terminals. Answers the way that service does."""
    from . import saved
    worked, said = saved.save()
    sys.stdout.write("%s %s\n" % ("PCH-OK" if worked else "PCH-ERROR", said))
    return 0 if worked else 1


def cmd_forget_terminal():
    """Run by the terminal service after it closed a terminal at the page's request: that one is not to come
    back. Only while the saved list is kept automatically; a list you save by hand changes when you save."""
    from . import saved
    if config.load()["terminal_autosave"] and len(sys.argv) > 2:
        saved.forget(sys.argv[2])
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    table = {"serve": cmd_serve, "--once": cmd_once, "once": cmd_once, "bench": cmd_bench, "url": cmd_url,
             "selfcheck": cmd_selfcheck, "save-terminals": cmd_save_terminals, "forget-terminal": cmd_forget_terminal}
    fn = table.get(cmd)
    if fn is None:
        sys.stdout.write(USAGE)
        return 0 if cmd in ("-h", "--help", "help") else 2
    return fn()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
