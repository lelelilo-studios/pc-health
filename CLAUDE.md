# PC Health: notes for coding agents

A local dashboard for a Linux workstation and the coding agents running on it. `README.md` says what it
does and what it exposes, and `docs/ARCHITECTURE.md` says how it is wired. If you are here to install it on
someone's machine, read `docs/AGENT_SETUP.md`. This file is for changing the code.

## Before and after every change

```bash
python3 -m unittest discover -s tests
```

164 tests, about 5 seconds. 21 of them need tmux at `/usr/bin/tmux` and are skipped without it; they use a
tmux server and a home folder of their own in a temporary folder, so they never touch real sessions.
All of them must pass before you say a change is done.

To try a change by hand, run `python3 -m pchealth serve` in the foreground. If PC Health is installed as a
service on this machine, the service runs this folder itself: `systemctl --user restart pc-health` after a
change. The page's files are read once at start, so restart after changing anything in `pchealth/static/` too.

## Rules the tests hold you to

`tests/test_server_safety.py` fails if one of these is broken. Do not loosen a test to make a change pass:
change the design instead, or stop and ask.

- **Standard library only.** No pip packages, no build step. The page uses no framework and loads nothing
  from the internet.
- **The monitor only reads.** Nothing in `pchealth/` may stop, delete or change anything: no `os.kill`,
  `os.remove`, `os.unlink`, `os.rmdir`, `os.rename`, `os.chmod`, `os.truncate`, `os.system`, `os.popen`,
  `shutil`, `shell=True`, `eval(` or `exec(`.
- **Programs are started in four files only:** `gpu.py` (nvidia-smi), `klog.py` (journalctl), `tmux.py`
  and `folders.py` (its own folder-sizing worker, one `Popen`).
- **tmux is only read from Python:** `list-panes`, `list-clients`, `capture-pane`, `display-message`.
  Starting, renaming, closing and typing belong to `terminal/attach.sh`, which the terminal service runs.
  The tmux commands that script may use, and the lines that type, save or remove something, are pinned
  word for word.
- **Files are written through `history.atomic_write` only**, and only into the state folder.
- **The server answers `GET` only.** Routes are an exact-match table; nothing a visitor sends reaches a
  file path.
- **Nothing secret reaches the page.** Never export a command line or an environment. Only the variables
  in `agents_claude.ENV_KEYS` and the fields in `agents_claude.SESSION_KEYS` are ever read.
- **The page inserts text, never HTML:** no `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `document.write`, `eval(` or `new Function`. Every `fetch` is a plain GET.
- **The pages keep to `Content-Security-Policy: default-src 'self'`:** no inline script or style, no
  `data:` URLs, nothing fetched from another site.
- **Nothing animates forever** (`infinite` is not allowed in `app.css`).
- **Imports stay light.** Loading the monitor must not import `logging`, `argparse`, `asyncio`, `sqlite3`,
  `http.server`, `http.client`, `email`, `xml`, `urllib.request`, `ssl`, `tomllib`, `hashlib`, `ipaddress`,
  `shutil` or `tempfile`. Import such a module inside the function that needs it.

## Style

- Python 3.11 or newer. Follow the file you are in: `%` formatting, no type hints, lines up to about 120
  characters, comments that say why in plain words.
- Readers of `/proc`, `/sys` and other programs' files must be tolerant: a missing file or an unknown value
  becomes "unknown", never an error. Each parser has a test with a small fixture in `tests/test_parsers.py`.
- Text shown to people uses plain words and short sentences ("Processor", "Waiting for you").
- Test fixtures use made-up names. No real project names, paths, host names, addresses or conversation ids.
- When behaviour changes, change the matching part of `README.md` and of `docs/` in the same commit.

## Never

- Add a dependency, a route that is not `GET`, or code that makes the monitor act on the machine.
- Widen who is answered (`listen`, `terminal_view`, `terminal_type`) or weaken the Host and Origin checks
  in `httpd.py` without the owner asking for exactly that.
- Use `sudo`, or change system files, from the code or the install scripts.
- Start a second copy on a port that is in use. To run next to an installed service, point `HOME` at an
  empty folder (settings and state are found through it) and set another `port` in that folder's
  `.config/pc-health/config.toml`.

## Commits

One change per commit, with a message that says what changed and why in plain words. Do not push or open
a pull request unless you are asked to.
