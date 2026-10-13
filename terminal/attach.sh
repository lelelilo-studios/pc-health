#!/usr/bin/env bash
# What the browser terminal may do, and nothing else. ttyd starts this script with words taken from the
# address, so every word is checked here and the address bar cannot be used to start arbitrary programs.
#
#   attach.sh NAME                 join the tmux session NAME (this is the terminal you type in)
#   attach.sh --new NAME           start a tmux session called NAME, in your home folder
#   attach.sh --rename OLD NEW     give a tmux session another name
#   attach.sh --close NAME         end a tmux session and everything running in it
#   attach.sh --save               note the tmux sessions running now, and the conversation in each
#   attach.sh --restore            start again the noted sessions that are not running, each with its conversation
#   attach.sh --at-login on|off    whether --restore also runs by itself when you log in to the PC
# Rename and close are refused for sessions the Emdash app made: those are Emdash's to rename or end.
#
# PC Health's own server only ever reads. These actions live here, in the terminal service, which is
# the one part that acts on your sessions; the same setting (terminal_type) decides who may reach it.
# The actions answer with one line, "PCH-OK ..." or "PCH-ERROR ...", and leave.
#
# --restore takes no words from the address: what it starts comes only from the file --save wrote
# (pchealth/saved.py describes it). Every field of that file is checked here before it is used, and the one
# thing ever typed into a session is a fixed line, "claude --resume <conversation id>", into a session this
# run has just created itself.

NAME_OK='^[A-Za-z0-9_.-]{1,64}$'                       # a session that can be joined from the page
NEW_NAME_OK='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'         # a name we are willing to give one (tmux turns a dot into "_")
ID_OK='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'      # a Claude Code conversation
MODEL_OK='^[][A-Za-z0-9._:-]{1,48}$'                   # a model name: nothing a shell would read as more than a word
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAVED="$HOME/.local/state/pc-health/saved-terminals.tsv"
AT_LOGIN="$HOME/.local/state/pc-health/restore-at-login"      # while this file exists, pc-health-restore.service acts at login

fail() { printf 'PCH-ERROR %s\n' "$1"; exit 1; }
ok() { printf 'PCH-OK %s\n' "$1"; exit 0; }
exists() { tmux has-session -t "=$1" 2>/dev/null; }

# The Emdash app starts tmux sessions of its own, finds them again by name and expects them to be there,
# so those are neither renamed nor closed from here. They are recognised by the name Emdash gives them
# (ending in a dash and ten hex digits), or by an Emdash window being attached to them right now.
is_emdash() {
  [[ "$1" =~ -[0-9a-f]{10}$ ]] && return 0
  local pid step
  for pid in $(tmux list-clients -t "=$1" -F '#{client_pid}' 2>/dev/null); do
    for step in 1 2 3 4 5 6 7 8; do
      pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
      [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || break
      [[ "$(ps -o comm= -p "$pid" 2>/dev/null)" =~ ^[Ee]mdash ]] && return 0
    done
  done
  return 1
}

# Start a session. If no tmux server is running yet, this is what starts one, and every terminal from then on
# descends from it. A server started from inside the terminal service would carry the service's limits into
# all of them (no sudo, and no snap programs such as Chromium, Firefox or Blender), and would end, with every
# terminal, whenever the service is restarted. So when this script runs inside a service and has to start the
# server, it asks your login session (systemd) to start it instead, as a small unit of its own. Once a server
# is running, and anywhere else, tmux is simply asked for one more session.
#
# -E, here and where a session is joined: tmux would otherwise copy a few settings (DISPLAY, XAUTHORITY...) from
# whoever creates or joins a session into that session, and drop the ones that program does not have. This
# service is started before the desktop is up and has no DISPLAY, so every terminal made or opened from the page
# lost it, and nothing started in one could open a window. With -E a session keeps what the tmux server has.
in_service() { [[ -n "${PCH_AS_SERVICE:-}" || "$(< /proc/self/cgroup)" == *.service ]]; }      # the variable is for the tests
start_session() {      # NAME FOLDER
  if ! tmux list-sessions > /dev/null 2>&1 && in_service && command -v systemd-run > /dev/null; then
    local same=()      # the server must be the one this script goes on talking to
    [[ -n "${TMUX_TMPDIR:-}" ]] && same=(--setenv="TMUX_TMPDIR=$TMUX_TMPDIR")
    # --expand-environment=no: systemd would otherwise rewrite "${NAME}" and "$$" in the words below, such as the folder.
    systemd-run --user --quiet --collect --expand-environment=no --service-type=forking --unit="pc-health-tmux-$$" "${same[@]}" -- "$(command -v tmux)" new-session -d -E -s "$1" -c "$2" 2>/dev/null && return 0
  fi
  tmux new-session -d -E -s "$1" -c "$2" 2>/dev/null      # also the way out if systemd would not do it
}

# Is this conversation open in a terminal right now? Claude Code keeps one small file per running process.
conversation_open() {
  local f pid comm
  for f in "$HOME"/.claude/sessions/*.json; do
    [[ -f "$f" ]] || continue
    grep -q -F -- "\"$1\"" "$f" 2>/dev/null || continue
    pid="${f##*/}"; pid="${pid%.json}"
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/comm" ]] || continue
    comm=""; read -r comm < "/proc/$pid/comm" || true
    [[ "$comm" == "claude" ]] && return 0
  done
  return 1
}

case "${1:-}" in
  --save)
    cd "$HERE/.." 2>/dev/null || fail "PC Health's own folder was not found."
    exec /usr/bin/python3 -E -s -m pchealth save-terminals
    ;;
  --restore)
    [[ -f "$SAVED" && ! -L "$SAVED" && -O "$SAVED" ]] || fail "Nothing is saved yet. Save the terminals first, from Settings."
    started=0; open=0; rows=0; damaged=0; skipped=""; begun=" "
    skip() { skipped+="${skipped:+, }$1"; }
    {
      IFS=$'\t' read -r magic version when rest || fail "The saved file is damaged."
      [[ "$magic" == "pch-saved-terminals" && "$version" == "1" && "$when" =~ ^[0-9]{1,12}$ && -z "$rest" ]] || fail "The saved file is damaged."
      while IFS=$'\t' read -r name id model folder rest || [[ -n "$name" ]]; do      # "||": a last line without a line break counts too
        (( ++rows > 32 )) && break
        if [[ -n "$rest" || ! "$name" =~ $NEW_NAME_OK || "$name" =~ -[0-9a-f]{10}$ ]] ||
           [[ "$id" != "-" && ! "$id" =~ $ID_OK ]] || [[ "$model" != "-" && ! "$model" =~ $MODEL_OK ]] ||
           [[ "$folder" != /* || "$folder" == *"#"* || "$folder" =~ [[:cntrl:]] || ${#folder} -gt 1024 ]]; then
          damaged=$((damaged + 1)); continue
        fi
        # A conversation is opened once: not if it is open somewhere already, nor twice by this run.
        if [[ "$id" != "-" ]] && { conversation_open "$id" || [[ "$begun" == *" $id "* ]]; }; then open=$((open + 1)); continue; fi
        if exists "$name" < /dev/null; then          # never type into a session this run did not create
          if [[ "$id" == "-" ]]; then open=$((open + 1)); else skip "$name (the name is in use)"; fi
          continue
        fi
        [[ -d "$folder" ]] || { skip "$name (its folder is gone)"; continue; }
        start_session "$name" "$folder" < /dev/null || { skip "$name (tmux could not start it)"; continue; }
        if [[ "$id" != "-" ]]; then
          line="claude --resume $id"
          [[ "$model" != "-" ]] && line+=" --model '$model'"
          if ! tmux send-keys -t "=$name:" -l -- "$line" < /dev/null 2>/dev/null ||
             ! tmux send-keys -t "=$name:" Enter < /dev/null 2>/dev/null; then
            tmux kill-session -t "=$name" < /dev/null 2>/dev/null      # leave nothing half-started, so trying again is clean
            skip "$name (the conversation could not be started)"; continue
          fi
        fi
        started=$((started + 1)); begun+="$id "
      done
    } < "$SAVED"
    more=""
    [[ -n "$skipped" ]] && more+=" Not started: $skipped."
    (( damaged > 0 )) && more+=" Damaged entries ignored: $damaged."
    if (( started == 0 && open == 0 )); then
      [[ -n "$more" ]] && fail "Nothing was started.$more"
      fail "The saved file has no terminals in it."
    fi
    said="$started started"
    (( open > 0 )) && said+=", $open already open"
    ok "$said.$more"
    ;;
  --new)
    name="${2:-}"
    [[ "$name" =~ $NEW_NAME_OK ]] || fail "Use letters, digits, dashes and underscores (up to 64), starting with a letter or digit. tmux does not keep dots in names."
    exists "$name" && fail "There is already a terminal called \"$name\"."
    start_session "$name" "$HOME" || fail "tmux could not start the terminal."
    ok "$name"
    ;;
  --rename)
    old="${2:-}"; new="${3:-}"
    [[ "$old" =~ $NAME_OK ]] && exists "$old" || fail "That terminal is no longer running."
    [[ "$new" =~ $NEW_NAME_OK ]] || fail "Use letters, digits, dashes and underscores (up to 64), starting with a letter or digit. tmux does not keep dots in names."
    is_emdash "$old" && fail "This terminal belongs to Emdash, which finds it by its name. It cannot be renamed."
    [[ "$new" == "$old" ]] && ok "$new"
    exists "$new" && fail "There is already a terminal called \"$new\"."
    tmux rename-session -t "=$old" "$new" 2>/dev/null || fail "tmux could not rename the terminal."
    ok "$new"
    ;;
  --close)
    name="${2:-}"
    [[ "$name" =~ $NAME_OK ]] && exists "$name" || fail "That terminal is no longer running."
    is_emdash "$name" && fail "This terminal belongs to Emdash. Close it from Emdash."
    tmux kill-session -t "=$name" 2>/dev/null || fail "tmux could not close the terminal."
    # Closed here is closed for good: it comes off the saved list, so that a restore does not bring it back.
    (cd "$HERE/.." 2>/dev/null && /usr/bin/python3 -E -s -m pchealth forget-terminal "$name") > /dev/null 2>&1 || true
    ok "$name"
    ;;
  --at-login)
    # Only a marker is made or removed. What runs at login is pc-health-restore.service, and all it runs is
    # this script with --restore.
    case "${2:-}" in
      on) mkdir -p "${AT_LOGIN%/*}" 2>/dev/null && : > "$AT_LOGIN" || fail "The setting could not be written."; ok "on" ;;
      off) rm -f -- "$AT_LOGIN" 2>/dev/null || fail "The setting could not be written."; ok "off" ;;
      *) fail "Unknown request." ;;
    esac
    ;;
  --*)
    fail "Unknown request."
    ;;
esac

name="${1:-}"
if [[ ! "$name" =~ $NAME_OK ]] || ! exists "$name"; then
  echo "PC Health terminal"
  echo
  if [[ -n "$name" ]]; then echo "There is no tmux session named \"$name\"."; else echo "No session was chosen."; fi
  echo "Sessions running now:"
  tmux list-sessions -F '  #{session_name}' 2>/dev/null || echo "  (none)"
  echo
  echo "Open one from the PC Health page (\"Open terminal\" next to an agent)."
  sleep 20
  exit 1
fi
exec tmux attach-session -E -t "=$name"
