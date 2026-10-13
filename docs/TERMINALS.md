# Terminals in detail

The [README](../README.md#terminals) has the short version: what the Terminals section is for, how to
switch typing on, and who may view and who may type. This file has the rest.

## A row, and its summary

The **Terminals** section lists every tmux session, one compact row each: its name with a pill for what
its agent is doing (the same pill as in the Agents table), and under it the conversation's title and the
folder it works in. Each row has up to three symbols, which say what they are when pointed at: an eye
(**View**: look, without typing), a keyboard (**Open**: type in it), and an "i" (a summary of that
terminal). The eye and the keyboard mean the same everywhere: in the Agents table, for an agent that runs
inside tmux, and in the **View only | Type** switch of the terminal page.

The summary shows the conversation's title and the agent's state; a grid of small numbers (the agent and
its model, how much is in its context, how long it has been running, what it is using); the folder it
works in with its git branch, and the terminal's size and where it is attached; and the last exchange,
what you asked and what it answered. View only, Type, Rename and Close are in a row at the bottom.

## What each action does

- **View** (`/watch`) shows a copy of the session's screen, read again every second, without joining the
  session or resizing it. Nothing you press is sent, and the page says so (a hatched strip, a dashed
  frame). View only needs tmux and nothing else.
  Claude Code draws full-screen, so tmux keeps no scrollback for it and the screen only ever shows the
  latest part. For those sessions, scrolling up loads the earlier conversation above the screen, page by
  page back to its start: what you typed, what the agent answered, and one line per action it took. It is
  read from the session's own record under `~/.claude/projects/`.
- **Open** (`/terminal`) opens the session itself in the browser. It is the same session your local
  terminal shows, and you can type in it. This needs the terminal service (below). On a touch screen there
  is a row of keys that on-screen keyboards lack (Esc, Shift+Tab, Tab, the arrows, Enter, Ctrl+C, Ctrl+O,
  Ctrl+B); the **Keys** button shows or hides it. Both pages have a text-size control.
- **New terminal** (the + button next to the section's title) starts a tmux session in your home folder.
  It asks for a name first (letters, digits, dashes and underscores; no dots, which tmux does not keep in
  names) and then opens it, ready to type.
- **Rename** gives a session another name.
- **Close** ends the tmux session and everything running in it, after asking once more. It says so when an
  agent is working or waiting in it.

Rename and Close are for terminals you started yourself. Terminals that the Emdash app started show
neither: Emdash finds its sessions again by name and expects them to be there. A session counts as Emdash's
when its name is the kind Emdash gives (ending in a dash and ten hex digits, like
`my-project-0a1b2c3d4e`), or when an Emdash window is attached to it.

## The bar of terminals

The terminal page (typing or view only) has one bar: the way back, a tab per
tmux session, the View only | Type switch and the text size. A tab is a mark for what its agent is doing (a
square for waiting for you, a filled dot for working, a ring for idle) and the terminal's name. The tabs
are in the order of the main page: waiting for you first, then working, then idle. The terminal on screen
is the raised tab, and it also says its state in words. A small dot after a name means that terminal has
something for you since you last opened it: its agent finished, or it began to wait for you. This is
remembered per browser tab. When the tabs do not all fit, the row scrolls sideways, and a small button
appears at the end where a terminal that wants you is out of view.

On a phone, the tabs take a row of their own, and on a touch screen the row of extra keys is shown.

## The terminal service

Typing needs [ttyd](https://github.com/tsl0922/ttyd) and `./install-terminal.sh`; the
[README](../README.md#switching-typing-on) has the commands. How it is wired, and what that means:

- ttyd listens on a socket file in your login session's runtime folder
  (`$XDG_RUNTIME_DIR/pc-health-ttyd.sock`), not on the network. The only way to it is through PC Health, on
  the same address as the dashboard, under `/tty`.
- ttyd runs one script, `terminal/attach.sh`, with words taken from the address. The script checks every
  word and accepts seven requests and nothing else: join a session, start one, rename one, close one, save
  the open ones, restore the saved ones, and switch restoring at login on or off.
- The first terminal started from the page after a restart of the PC is what starts tmux itself, and every
  terminal after it descends from that. The script asks your login session (systemd) to start it, as a
  small unit of its own (`pc-health-tmux-…`), rather than starting it inside the terminal service.
  Terminals made from the page therefore behave like ones you open yourself: `sudo` and snap programs work
  in them, and restarting the terminal service does not end them.
- While you use a session from a smaller screen, tmux may resize the session to that screen; your local
  terminal shows it at that size until you use it there again.
- To switch typing off: `systemctl --user disable --now pc-health-terminal`, or `terminal_type = "off"`.

Rename, Close, New terminal, and saving and restoring are done by this same service, so they need typing
to be allowed on the device (`terminal_type`).

## Limits

- **8 at once.** `pc-health-terminal.service` runs ttyd with `-m 8`, which is ttyd's limit on clients.
  Every terminal open for typing is one client, in whatever browser tab or device it is open. Start,
  rename, close, save, restore and the at-login switch each make a short connection to the same service,
  under the same limit. View only does not use ttyd and is not counted.
- **32 saved.** The list of saved terminals holds at most 32. Pressing *Save current terminals* says which
  were left out; the list that keeps itself up to date stops at 32 without saying so.

## Saving the terminals, and bringing them back

tmux sessions do not survive a restart of the PC; the conversations in them do. PC Health keeps a list of
the terminals that are open: the name of each, the Claude Code conversation running in it, and the folder
that conversation was started in. The list keeps itself up to date, without anything to press:

- a new terminal is added once it has been seen open for 20 seconds (the monitor looks at tmux every 10
  seconds while the page is open, every 60 otherwise);
- when the conversation in a terminal changes (`/clear`, or another one started there), the new one is
  saved in its place; while no conversation runs there (Claude was closed, or is still coming up) the saved
  one stays;
- a renamed terminal keeps its entry, and a conversation resumed in another terminal moves there;
- a terminal you close is taken off: at once when closed from the page, after it has been gone for 20
  seconds when it ended some other way while tmux kept running.

It never forgets a terminal merely because it is gone. After a restart of the PC, or if tmux itself
crashes, nothing is running, and everything that was open is still on the list. (To keep it by hand
instead, set `terminal_autosave = false`: it then changes only when you press *Save current terminals* in
Settings.)

*Restore saved terminals* (in **Settings**, and as a button in the Terminals section while something saved
is not running) starts each of them again: a tmux session with the same name, in that folder, with
`claude --resume <conversation>` typed into it (and `--model` if the conversation was started with one). A
terminal without a conversation comes back as an empty terminal in your home folder. Restoring never
touches a terminal that is running: a conversation that is already open somewhere, or a name that is in
use, is skipped, so pressing it twice does no harm. With **Bring them back when I log in to the PC**
ticked in Settings, the same restore runs by itself each time you log in to the desktop (it is off until
you tick it).

*Forget what is not running*, also in Settings, makes the list exactly the terminals as they are right
now: saved ones that are not running are dropped, after it has said which. A small bookmark marks what is
saved, next to the terminal's name, next to the agent's name and in the summary. The list is in
`~/.local/state/pc-health/saved-terminals.tsv`.
