# Settings, and opening the page from a phone

## Settings

Optional. Copy `config.example.toml` to `~/.config/pc-health/config.toml`, edit it, then restart the
service (`systemctl --user restart pc-health`). Every key is optional and the example file shows the
defaults.

| Key | Default | Meaning |
|---|---|---|
| `port` | `7780` | The port of the page |
| `listen` | `"lan"` | `"lan"`: this PC and devices on the private network it is attached to. `"local"`: this PC only. See [Security](../README.md#who-is-answered) |
| `access_code` | `""` | A code other devices must enter once. This PC is never asked. Letters and digits only, in quotes: see [The access code](../README.md#the-access-code) |
| `protect` | `"terminals"` | What the code guards on other devices: only terminals, or `"page"` for everything |
| `terminal_view` | `"local"` | Who may view a tmux session's screen and its earlier conversation. The values are in the table under [Terminals](../README.md#who-may-view-and-who-may-type) |
| `terminal_type` | `"local"` | Who may type in a session and start, rename, close, save or restore one. Same table |
| `terminal_autosave` | `true` | Keep the list of saved terminals up to date by itself |
| `terminal_socket` | `""` | Where the terminal service listens. Empty: a socket in your login session's runtime folder |
| `project_dirs` | `["~/Projects", "~/emdash/worktrees"]` | Folders whose sub-folders are projects. Build folders inside them are sized |
| `watch_extra` | `[]` | More folders to size |
| `ignore_leftovers` | `[]` | Program names never listed under "Left behind by agents" |
| `[thresholds]` | see the file | A warning and a critical level for each health rule. A 0 switches that level off |

What happens to a line the program does not understand:

- **A key it does not know** is ignored.
- **A value of the wrong kind** (a number or `true` where a word in quotes belongs) is ignored too, and
  the default stays. For `listen` that default is `"lan"`. For `access_code` it is no code:
  `access_code = 4821` without quotes sets nothing.
- **A word in quotes that it does not know** falls back to the safer side: `listen` to `"local"`, `protect`
  to `"page"`, the terminal switches to `"local"`.
- **A file that cannot be read as TOML** (a word without its quotes, the same key twice) is ignored whole.
  Every setting is then at its default, `listen = "lan"` included, and the foot of the page says "Settings
  file ignored" with the reason.

None of the first three leaves a message. To see what was understood, run this in the cloned folder:

```bash
python3 -c "from pchealth import config; c = config.load(); print(c.get('config_error'), c['listen'], c['port'], bool(c['access_code']))"
```

The first word must be `None`. After a restart, `ss -ltn | grep 7780` shows where the page really
listens: `127.0.0.1:7780` for `"local"`, `0.0.0.0:7780` for `"lan"`.

## On a phone

Open the page from a phone or laptop on the same network: `http://<pc-name>.local:7780` where the network
resolves `.local` names, or the numeric address. `python3 -m pchealth url` prints both. This needs
`listen = "lan"`.

- Below 640 pixels wide, a bar at the bottom jumps between Overview, Agents, Terminals, Disks and More,
  and shows a badge with the number of agents waiting for you.
- The headline tiles show the number and a meter. Trend lines and the range switch are left to bigger
  screens.
- Each agent is two short lines: its name, its state, and the two tmux buttons. A tap shows the rest.
- The four detail sections (busiest programs, temperatures, biggest folders, what happened) start folded.
  Adding `#details` to the address opens everything.
- On the terminal page, the tabs take a row of their own. On a touch screen the row of extra keys is
  shown ([docs/TERMINALS.md](TERMINALS.md#what-each-action-does) lists the keys).

With the default settings a phone sees the dashboard but no terminals. To view sessions from it, set
`terminal_view = "lan"`; to type from it, `terminal_type = "lan"`. Read
[Security](../README.md#security) before you do, and set an `access_code`.

If a phone cannot open the page: check that both are on the same network, try the numeric address, and
check the PC's firewall (for example `sudo ufw status`). The port must be allowed from your network. PC
Health never opens or changes a firewall itself.
