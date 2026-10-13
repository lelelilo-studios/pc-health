# Setting up PC Health: instructions for a coding agent

You are installing PC Health on someone's Linux machine. Read `README.md` first: it says what the program
does and what it exposes. This file says what to check, what to adapt, what not to change, how to verify,
and how to undo.

## Ground rules

1. **Look first, change nothing.** Everything in step 1 only reads. Cloning the repository is the one
   change you may make before the person says OK.
2. **Report before you install.** Tell the person what will work on this machine and what will not, and
   wait for their answer.
3. **Never use `sudo` and never install a package without asking.** PC Health itself needs neither. Only
   two optional steps do, and the person decides both: installing a package (tmux, ttyd) and switching off
   the ttyd package's own service.
4. **Keep it local.** Set `listen = "local"` unless the person asks to open the page from a phone.
5. **Terminals are the person's decision.** Do not switch on viewing or typing in terminals without asking.
   Both stay `"off"` in the settings file until the person has said yes to each (steps 2 and 3).
6. **Do not edit the code to make it fit.** If something does not fit this machine, say so.

## 1. Check the machine

Run these and keep the answers for your report.

```bash
uname -sr; . /etc/os-release && echo "$PRETTY_NAME"       # Linux, and which distribution
/usr/bin/python3 --version                                # must be 3.11 or newer: this is the one the service runs
systemctl --user is-system-running                        # "running" or "degraded": user services work
ls -l /usr/bin/python3 /usr/bin/nvidia-smi /usr/bin/tmux /usr/bin/ttyd /usr/bin/journalctl 2>&1
command -v python3 tmux ttyd                              # what the install scripts find on PATH: compare with the line above
/usr/bin/tmux -V; /usr/bin/ttyd --version                 # ttyd must be 1.7.4 or newer
ss -ltn | grep -E ':7780\b' || echo "port 7780 is free"
ls -d ~/.claude/sessions ~/.codex/sessions 2>&1           # which agents this person uses
df -h /; free -g                                          # for the disk and memory thresholds
findmnt -n -o FSTYPE /tmp                                 # tmpfs: files in /tmp are held in memory
```

Then, from the cloned folder:

```bash
/usr/bin/python3 -m pchealth selfcheck       # one line per data source
```

Its exit code is 1 when any line says something other than `ok` or `none running`. That is normal on a
machine without an NVIDIA card (the `gpu` line says `no NVIDIA GPU found`), without a readable journal
(`klog`: `cannot read the system journal`) or without a `~/.claude/sessions` folder (`agents`:
`no Claude Code session folder`). It is not a failed install and not a reason to stop. Put the lines in
your report.

What each answer means:

| If this is missing | Then |
|---|---|
| Linux | Stop. PC Health reads `/proc` and `/sys` and runs nowhere else |
| Python 3.11+ at `/usr/bin/python3` | Stop and tell the person. The service and the terminal script call that path; a newer Python somewhere else (conda, pyenv) is not used by them |
| `systemctl --user` | `./install.sh` will fail. Offer to run `python3 -m pchealth serve` in the foreground or in a tmux session instead |
| `/usr/bin/nvidia-smi` | No graphics card readings. Everything else works |
| `/usr/bin/tmux` | No Terminals section, even when another `tmux` is on `PATH`. Offer to install tmux from the distribution's packages; the person decides |
| `/usr/bin/ttyd` | No typing from the page, and no start, rename, close, save or restore. Viewing a session still works. Installing it is optional and needs `sudo` |
| `/usr/bin/journalctl` | No names for programs stopped when memory ran out |
| `~/.claude/sessions` and `~/.codex/sessions` | No agents to show yet. The health part works |
| A free port 7780 | Choose another `port` in the settings file. Never stop what is using it |

**The paths are fixed in some places and not in others.** The monitor runs `/usr/bin/nvidia-smi`,
`/usr/bin/journalctl` and `/usr/bin/tmux`, and the unit files run `/usr/bin/python3` and `/usr/bin/ttyd`. A
copy somewhere else is not used by them. The install scripts and `terminal/attach.sh` call `python3`,
`ttyd` and `tmux` by name and get the first one on `PATH`. Compare the `ls` and `command -v` answers above.
Where they differ:

- **`ttyd` on `PATH` but not at `/usr/bin/ttyd`:** `./install-terminal.sh` checks the one on `PATH`, so it
  passes and installs the units, and the terminal service then cannot start. systemd reports the start of
  a service of this kind as done even when its program is missing, so the script can still end with
  "Terminals are on.". Do not run it on such a machine. Report that typing needs ttyd at `/usr/bin/ttyd`.
- **`python3` on `PATH` is not `/usr/bin/python3`:** what counts is `/usr/bin/python3 --version`. Run the
  commands in this file with `/usr/bin/python3`, as written, so that you test the one the service runs.
- **`tmux` on `PATH` but not at `/usr/bin/tmux`:** the monitor counts tmux as missing.

Say so in your report rather than adding links in system folders.

## 2. Tell the person what you found

Before changing anything, write a short list:

- what will work (health, agents, terminals to view, terminals to type in);
- what will not, and why (for example "no NVIDIA card: the graphics tile stays empty");
- what you are about to create (see step 4), and that the page will answer on this PC only;
- anything that would need `sudo` or a package, as a question.

And ask:

- which folders hold their projects (for `project_dirs`);
- whether anyone else can log in to this machine. "This PC only" means every connection from the machine
  itself, so other user accounts on it see the dashboard, and see or type in terminals if those are on;
- whether they want to **view** tmux sessions from the page. A viewer sees a session's screen as it is,
  with whatever is printed on it;
- whether they want to **type** in them from the page. That is a separate install (step 4) and lets the
  page run commands as them.

Then wait.

## 3. Write the settings file

Copy `config.example.toml` to `~/.config/pc-health/config.toml` and change only what this machine needs.
If the file is already there, read it and keep the person's choices.

| Key | What to set |
|---|---|
| `listen` | `"local"`. With `"lan"` (the program's default) every device on an attached private network sees the dashboard without a password |
| `port` | Leave 7780 unless it is taken |
| `project_dirs` | The folders the person named. The default is `["~/Projects", "~/emdash/worktrees"]` |
| `watch_extra` | Other large folders the person wants sized, if any |
| `terminal_view` | `"local"` if the person said yes to viewing, otherwise `"off"`. Never `"lan"` unless they ask for it |
| `terminal_type` | `"local"` if the person said yes to typing, otherwise `"off"`. Never `"lan"` unless they ask for it |
| `access_code`, `protect` | Leave as they are. They only matter once other devices are let in |
| `ignore_leftovers` | Leave empty |

Thresholds are `[warning, critical]` pairs, and a 0 switches that level off. The defaults suit a large
workstation. Adapt the ones that depend on size:

- `main_disk_free_gb` (default `[30, 12]`): on a small main disk, use about 10% and 5% of its size.
- `mem_available_gb` (default `[3, 1.5]`): on a machine with 16 GB or less, lower both, for example to
  about 10% and 5% of memory.
- `crash_dumps_gb`, `ram_files_gb`: lower them on a small disk or little memory.
- Temperatures (`cpu_temp_c`, `gpu_temp_c`, `ssd_temp_c`, `coolant_temp_c`): leave the defaults unless the
  person knows the limits of their hardware.
- Leave the rest alone.

Only the keys in `config.example.toml` are read. An unknown key or a value of the wrong kind is ignored
without a message, and a file that is not valid TOML is ignored whole, which puts `listen` back to `"lan"`.
So check the result:

```bash
/usr/bin/python3 -c "from pchealth import config; c = config.load(); print(c.get('config_error'), c['listen'], c['port'], c['project_dirs'], c['terminal_view'], c['terminal_type'])"
```

The first word must be `None`, and `listen` must be `local`.

## 4. Install

```bash
./install.sh
```

It writes two files:

- `~/.config/systemd/user/pc-health.service`, which runs the cloned folder itself;
- `~/.local/share/applications/pc-health.desktop`, the menu entry.

Then it enables and starts the service (systemd adds its own link for that). Once running, the service
keeps its history in `~/.local/state/pc-health/`. Notes:

- Clone into a path without spaces, `|` or `&`. Do not move the folder afterwards; if it has to move, run
  `./install.sh` again.
- The service runs while the person is logged in. `loginctl enable-linger $USER` keeps it running when
  they are logged out. That changes how their login session behaves, so ask first.
- Where there is no `systemctl --user`, do not write a system-wide unit. Run it in the foreground.
- `install.sh` itself runs the `python3` on `PATH` to print the page's address. If that one cannot run
  PC Health, the script stops after writing the unit file, before the service is enabled.

**Terminals you can type in are a separate step. Ask before doing it.** It lets the page run commands as
this person, from anything on this PC that can open the page. It needs ttyd from the distribution's
packages (on Debian and Ubuntu: `sudo apt install ttyd`) and `terminal_type = "local"` in the settings file
(restart `pc-health` after changing it), then:

```bash
./install-terminal.sh
systemctl --user is-active pc-health-terminal    # must say "active"; do not go by the script's last line
```

This adds `pc-health-terminal.service` and `pc-health-restore.service` to the same folder of user units.
At most 8 terminals can be open for typing at once (the unit runs ttyd with `-m 8`).

Ubuntu's ttyd package also enables a service of its own (`ttyd.service`; on Ubuntu 26.04, a login prompt
on port 7681 of the loopback address). Check with `systemctl is-enabled ttyd`. PC Health does not use it.
Tell the person, and let them decide whether to run `sudo systemctl disable --now ttyd`.

## 5. What must not be changed

- Do not set `listen`, `terminal_view` or `terminal_type` to `"lan"` unless the person asks for phone
  access. If they do, set an `access_code` as well: letters and digits only, in quotes (a code with `&`,
  `%`, `+`, `#` or a space in it can never be entered). Tell them that the page is plain HTTP: on that
  network the dashboard, what they type and the code itself can be read by others.
- Do not open a firewall port.
- Do not run PC Health as root or as a system service. It finds agents through the home folder of the
  user it runs as.
- Do not change files under `pchealth/`, `terminal/` or `tests/`, and do not edit the unit files to
  loosen them (`NoNewPrivileges`, the memory limits, the paths).
- Do not close, rename or type into the person's tmux sessions while testing. The tests bring their own
  tmux server.
- Do not start a second copy on a port that is in use, and do not stop someone else's process to free one.
- Do not delete `~/.local/state/pc-health/` or `~/.config/pc-health/` unless you are undoing the install
  and the person agreed.

## 6. Verify

```bash
/usr/bin/python3 -m unittest discover -s tests   # "Ran 164 tests" and "OK" (21 skipped without /usr/bin/tmux)
systemctl --user is-active pc-health             # active
ss -ltn | grep -E ':7780\b'                      # 127.0.0.1:7780, not 0.0.0.0:7780
curl -s http://localhost:7780/healthz            # "ok <samples> <seconds since the last one>"
curl -s 'http://localhost:7780/api/snapshot?peek=1' | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['health']['level'], len(s['agents']), s['self']['sources'], s['self']['config_error'])"
journalctl --user -u pc-health -n 20 --no-pager  # start and stop lines only; a traceback here is a problem
```

What to look for:

- The last number of `/healthz` stays under about 12 (the monitor samples every 10 seconds when no page
  is open).
- In the snapshot, a source that is not `ok` or `none running` is what the foot of the page lists as "Not
  available". Report each one with its reason. A missing NVIDIA card or an unreadable journal is a gap on
  this machine, not a failed install. `config_error` must be `None`.
- If the tests do not all pass, stop and report the failing test names. Do not edit tests.

Open the page for the person (`xdg-open http://localhost:7780`), or give them the address.

Then report:

- what works and what does not, with the reason for each gap;
- the settings you wrote, and why each differs from the default;
- every file you created;
- every command that used `sudo`, if any, and that the person approved it.

## 7. Undo

```bash
./uninstall.sh
```

This stops the services and removes the three unit files and the menu entry. To remove the rest:

- settings and history: `rm -r ~/.config/pc-health ~/.local/state/pc-health` (ask first);
- the cloned folder;
- if lingering was switched on for this: `loginctl disable-linger $USER`;
- if the ttyd package's own service was switched off and the person wants it back:
  `sudo systemctl enable --now ttyd`;
- if ttyd or tmux were installed only for this, the person can remove the packages.

tmux sessions that were started from the page are ordinary tmux sessions. They keep running after an
uninstall and belong to the person; leave them alone.
