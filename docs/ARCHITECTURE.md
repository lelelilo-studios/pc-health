# How PC Health is built

The [README](../README.md) says what PC Health shows, how to install it and what it exposes. This file
says how it works inside: the two threads, how agents are found, how it stays light, where it looks for
other programs, the layout of the code, and what the tests cover.

## How it works

Two threads. A sampler reads `/proc` and builds one snapshot per tick: every 2 seconds while a page is
open, every 10 seconds when nobody is looking. A worker does everything that can block: `nvidia-smi`, the
journal, drive and folder sizes, the agents' own files. The snapshot is turned into JSON once, and the web
server hands out those same bytes to every page. The page asks for `/api/snapshot` every 2 seconds and
stops asking while its tab is hidden.

## How agents are found

- **Claude Code**: processes named `claude`, joined with `~/.claude/sessions/<pid>.json`, which holds the
  session's title and whether it is busy, idle or waiting. That file is internal to Claude Code and may
  change; if it does, agents still appear but their state shows as unknown.
- **Context** is how many tokens a session's conversation holds right now. For Claude Code it is read from
  the newest message in the session's transcript under `~/.claude/projects/`, reading only what was
  appended since the last look.
- **Sub-agents** (Claude Code): an agent can hand part of its work to sub-agents, which run inside its own
  process, so there is no process to look at. Claude Code keeps a transcript for each next to the session's
  own (`<session>/subagents/agent-<id>.jsonl`) and a small note saying what kind it is and, in a few words,
  what it was asked. A sub-agent is listed while the end of its transcript shows it has not given its
  answer yet and the file was written in the last 15 minutes (and since the agent itself started). Only the
  end of those files is read, and only when they change. From them the page takes the tool the sub-agent is
  waiting on, by name only: nothing it read, wrote or ran.
- **Codex**: processes named `codex`, with state, model and token counts read from the session's rollout
  file under `~/.codex/sessions/`. See [Status and limits](../README.md#status-and-limits).
- **Emdash** sessions (the Emdash app runs agents in tmux sessions of its own) are labelled
  "project / task" from variables Emdash sets.
- **Left behind**: Claude Code marks every process it starts, in that process's environment. A marked
  process that is no longer underneath a running agent was left behind. A headless browser or dev server
  without the mark is listed as a maybe, and only when it has run for half an hour with no terminal, after
  its parent has gone, in a group of processes that hosted an agent. Nothing else is listed, so your own browser,
  editor and apps never appear.
- **Temp files**: a `tmpfs` mounted at `/tmp` or `/dev/shm` is counted as memory, not disk, because that
  is where its files live. Each Claude Code session's scratch folder under `/tmp/claude-<uid>/` is sized
  and shown in that agent's row.

## How it stays light

- The monitor samples every 10 seconds instead of every 2 when no page has asked in the last 12 seconds.
- Folder sizes are measured rarely (every 12 hours, and sooner when free space on the main disk has
  changed by gigabytes), in a separate process at the lowest priority that sleeps most of the time, and not
  at all while the machine is overloaded.
- Transcripts are read incrementally: only the bytes added since the last look.
- The monitor watches its own cost, shown at the foot of the page. It slows down above 5% of one processor
  thread and stops itself above 25% (systemd is told not to restart it into the same problem).
- The service is capped at 256 MB for itself and its helpers, and is never swapped.

Measured on the author's workstation with 11 agent sessions open and nobody looking at the page: 0.13% of
one processor thread over five minutes, and about 24 MB of memory. One sample costs about 8 ms of
processor time. Your own figures are at the foot of the page, and `python3 -m pchealth bench` prints the
cost of each part.

## Where programs are looked for

Some places name a program by its full path, and others by its name alone, which means the first one on
`PATH`.

| Program | Run by its full path in `/usr/bin` | Run by name, through `PATH` |
|---|---|---|
| `python3` | `pc-health.service` (the monitor). `terminal/attach.sh`, to save the open terminals and to take a closed one off the saved list | `install.sh`, to get the page's address for the menu entry and to print the addresses at the end. `install-terminal.sh`, to work out where the socket goes. Every `python3 -m …` command in the documentation, the tests included |
| `tmux` | The monitor (`pchealth/tmux.py`): the list of sessions and the view-only screen. The tests, to decide whether to skip the 21 that need it | `terminal/attach.sh`: joining, starting, renaming, closing and restoring a session |
| `ttyd` | `pc-health-terminal.service` | `install-terminal.sh`: its check that ttyd is there and has the options it needs |
| `nvidia-smi` | The monitor (`pchealth/gpu.py`) | Nowhere |
| `journalctl` | The monitor (`pchealth/klog.py`) | Nowhere |

What follows from the table:

- The monitor never looks for a `ttyd` program. It only looks whether the terminal service's socket file is
  there (`_socket_alive` in `pchealth/sampler.py`), and the page offers typing when it is.
- `install-terminal.sh` and the unit it installs can mean two different copies of ttyd. With ttyd on `PATH`
  and none at `/usr/bin/ttyd` (built into `/usr/local/bin`, a download in `~/.local/bin`), the script's
  check passes, it installs the units, and the service cannot start. systemd reports the start of a
  service of this kind as done even when its program is missing, so the script can still end with
  "Terminals are on.". The Terminals section goes on saying "typing is off: run ./install-terminal.sh in
  the PC-health folder", and running it again changes nothing. To know, ask systemd:
  `systemctl --user is-active pc-health-terminal`.
- With tmux on `PATH` and none at `/usr/bin/tmux`, the monitor counts tmux as missing: the Terminals
  section stays empty although `tmux` works in your shell, and the 21 tests are skipped.
- `install.sh` runs `python3 -m pchealth url` with the `python3` on `PATH`, once before it enables the
  service. If that `python3` cannot run PC Health, the script stops there: the unit file is written, and
  the service is neither enabled nor started.
- The monitor reads tmux with `/usr/bin/tmux`, and the terminal script acts on tmux with the `tmux` it
  finds by name. The `PATH` that counts for the script is the one of the service that runs it, which is
  not necessarily your shell's.

The [README](../README.md#which-copy-of-a-program-is-used) has the commands to compare the two before
installing.

## Layout

```
pchealth/
  procfs.py sysfs.py gpu.py klog.py     readers for /proc, /sys, nvidia-smi, the journal
  proctable.py attribution.py           process tree, which process belongs to which agent
  agents_claude.py agents_codex.py      session files and state mapping
  leftovers.py folders.py dirscan.py    leftover rules, folder watch list, gentle folder sizing
  tokens.py                             token use per session, read incrementally from transcripts
  subagents.py                          the sub-agents an agent is running now, from the end of their transcripts
  tmux.py                               which tmux pane an agent is in, and what it shows (read-only)
  saved.py                              which terminals to bring back after a restart: finds the conversation in each,
                                        keeps the list up to date (the terminal service does the bringing back)
  conversation.py                       earlier turns of a Claude Code conversation, for the view-only page
  disks.py history.py alerts.py         time-to-full, trend buffers and event log, health rules
  config.py                             the defaults, and the optional settings file
  sampler.py httpd.py __main__.py       the two worker threads, the GET-only server, commands
  static/                               the page (no framework, nothing loaded from the internet)
  static/terminal.*                     the terminal page (/terminal to type, /watch to view only): text size,
                                        on-screen keys, earlier conversation
  static/nav.js                         the bar of terminals (one tab per session, with its status) on the terminal page
terminal/attach.sh                      what the terminal service (ttyd) may do: join, start, rename or close a tmux session,
                                        save the open ones, restore the saved ones, switch restoring at login on or off
pc-health.service                       the monitor, as a per-user service (install.sh fills in the folder)
pc-health-terminal.service              the terminal service (install-terminal.sh fills in the folder and the socket)
pc-health-restore.service               runs that restore when you log in, while the switch in Settings is on
install.sh install-terminal.sh          install the monitor, and the terminal service, as per-user services
uninstall.sh                            stops them and removes the units and the menu entry
config.example.toml                     every setting with its default, to copy and edit
tests/                                  unit tests, including the read-only and no-secrets checks
docs/AGENT_SETUP.md                     instructions for a coding agent that installs PC Health for you
docs/ARCHITECTURE.md                    this file
docs/TERMINALS.md                       the Terminals section in detail, and how terminals are saved and brought back
docs/SETTINGS.md                        every setting, and opening the page from a phone
CLAUDE.md                               notes for a coding agent that changes the code
```

State is kept in `~/.local/state/pc-health/`: trend history (24 hours and 7 days), the event log, what is
known about agents that ended, progress through the transcripts, and the saved terminals. A few hundred
kilobytes.

## Tests

```bash
python3 -m unittest discover -s tests
```

164 tests, about 5 seconds, no dependencies. They pass on Python 3.11, 3.12 and 3.14.

- `tests/test_parsers.py` (28): the readers of `/proc`, `/sys`, `nvidia-smi`, the journal, tmux and the
  routing table, each against a small fixture.
- `tests/test_logic.py` (83): trend buffers, time-to-full, agent states, which process belongs to which
  agent, leftovers, token counting, the earlier conversation, sub-agents, the saved-terminals rules,
  settings and the health rules.
- `tests/test_server_safety.py` (53): what the server refuses (other methods, odd paths, foreign `Host`
  and `Origin`), who may reach terminals in every combination of the settings, that the code has no call
  that stops, deletes or changes anything, which tmux commands may appear and where, and that a planted
  secret in a process's environment and command line never reaches a snapshot.

21 of them drive `terminal/attach.sh` against a tmux server and a home folder of their own, in a temporary
folder, so your sessions and saved terminals are never touched. They are skipped where `/usr/bin/tmux`
is missing.
