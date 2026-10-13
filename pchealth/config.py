"""Defaults plus an optional overlay from ~/.config/pc-health/config.toml."""

import os

GB = 1 << 30
CONFIG_PATH = os.path.expanduser("~/.config/pc-health/config.toml")
STATE_DIR = os.path.expanduser("~/.local/state/pc-health")
# The saved terminals come back at login while this file exists (the page's switch makes and removes it, through
# the terminal service), and only if the unit that does it is installed (install-terminal.sh).
AT_LOGIN_FLAG = STATE_DIR + "/restore-at-login"
AT_LOGIN_UNIT = os.path.expanduser("~/.config/systemd/user/pc-health-restore.service")

DEFAULTS = {
    "port": 7780,
    # "lan": this PC plus devices on the connected home network. "local": this PC only.
    "listen": "lan",
    # A code other devices must enter once. This PC never needs it.
    "access_code": "",
    # What the code protects on other devices: "terminals" (the dashboard stays open) or "page" (everything).
    "protect": "terminals",
    # tmux sessions. "local" = this PC only, "lan" = other devices on the home network too, "off" = nowhere.
    # If access_code is set, other devices must enter it first; if it is empty they get straight in.
    "terminal_view": "local",        # watching a session's screen (read-only)
    "terminal_type": "local",        # typing in a session; starting, renaming, closing one (needs install-terminal.sh)
    # Keep the saved terminals in step with the ones that are open, without pressing Save.
    "terminal_autosave": True,
    # Where the terminal service listens. Empty = a private socket in your login session's runtime folder.
    "terminal_socket": "",
    # Folders whose sub-folders are projects; build folders inside them are watched.
    "project_dirs": ["~/Projects", "~/emdash/worktrees"],
    # Extra folders to size (in addition to the built-in watch list).
    "watch_extra": [],
    # Program names that are never reported as leftovers.
    "ignore_leftovers": [],
    # (warning, critical). "below" rules fire when the value drops under the number.
    "thresholds": {
        "main_disk_free_gb": [30, 12],
        "other_disk_free_pct": [5, 2],
        "disk_eta_hours": [48, 8],
        "disk_drop_gb_10min": [5, 15],
        "crash_dumps_gb": [2, 20],
        "mem_available_gb": [3, 1.5],
        "mem_pressure_pct": [15, 35],
        "swap_used_pct": [50, 80],
        "ram_files_gb": [4, 10],
        "cpu_wait_pct": [25, 50],
        "vram_used_pct": [90, 97],
        "gpu_oom_15min": [1, 20],
        "cpu_temp_c": [85, 92],
        "gpu_temp_c": [83, 88],
        "ssd_temp_c": [70, 80],
        "coolant_temp_c": [45, 55],
        "leftover_cores": [0, 1.0],
        "leftover_mem_gb": [0, 4],
        "leftover_vram_gb": [0, 2],
        "idle_agent_cores": [1.0, 0],
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if k not in base:
            continue  # unknown keys are ignored rather than trusted
        if isinstance(base[k], dict) and isinstance(v, dict):
            out[k] = _merge(base[k], v)
        elif type(v) is type(base[k]) or (isinstance(base[k], (int, float)) and isinstance(v, (int, float))):
            out[k] = v
    return out


def load(path=CONFIG_PATH):
    cfg = _merge(DEFAULTS, {})
    if os.path.isfile(path):
        import tomllib  # imported only when a config file exists
        try:
            with open(path, "rb") as f:
                cfg = _merge(DEFAULTS, tomllib.load(f))
        except (OSError, tomllib.TOMLDecodeError) as e:
            cfg["config_error"] = str(e)[:200]
    cfg["project_dirs"] = [os.path.expanduser(p) for p in cfg["project_dirs"]]
    cfg["watch_extra"] = [os.path.expanduser(p) for p in cfg["watch_extra"]]
    if cfg["listen"] not in ("lan", "local"):
        cfg["listen"] = "local"
    if cfg["protect"] not in ("terminals", "page"):
        cfg["protect"] = "page"                       # when in doubt, protect more
    for key in ("terminal_view", "terminal_type"):
        if cfg[key] not in ("local", "lan", "off"):
            cfg[key] = "local"
    if not cfg["terminal_socket"]:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        cfg["terminal_socket"] = runtime + "/pc-health-ttyd.sock"
    return cfg
