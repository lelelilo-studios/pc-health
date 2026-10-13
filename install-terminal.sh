#!/usr/bin/env bash
# Optional: open tmux sessions from the PC Health page and type in them.
# Needs ttyd first:   sudo apt install ttyd
# Switch off with:    systemctl --user disable --now pc-health-terminal
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$HOME/.config/systemd/user/pc-health-terminal.service"

if ! command -v ttyd >/dev/null; then
  echo "ttyd is not installed. Run this first, then run me again:"
  echo "  sudo apt install ttyd"
  exit 1
fi
for flag in --writable --check-origin --url-arg --base-path --interface; do
  ttyd --help 2>&1 | grep -q -- "$flag" || { echo "This ttyd ($(ttyd --version 2>&1)) lacks $flag; version 1.7.4 or newer is needed."; exit 1; }
done

SOCKET="$(cd "$HERE" && python3 -c "from pchealth import config; print(config.load()['terminal_socket'])")"
mkdir -p "$(dirname "$UNIT")"
sed -e "s|@DIR@|$HERE|g" -e "s|@SOCKET@|$SOCKET|g" "$HERE/pc-health-terminal.service" > "$UNIT"
chmod 600 "$UNIT"
# Bringing the saved terminals back at login: installed here, and idle until you switch it on in Settings.
RESTORE_UNIT="$HOME/.config/systemd/user/pc-health-restore.service"
sed -e "s|@DIR@|$HERE|g" "$HERE/pc-health-restore.service" > "$RESTORE_UNIT"
chmod 600 "$RESTORE_UNIT"
systemctl --user daemon-reload
systemctl --user enable pc-health-terminal.service pc-health-restore.service
systemctl --user restart pc-health-terminal.service
systemctl --user --no-pager --lines=0 status pc-health-terminal.service || true

echo
echo "Terminals are on. Open one from the PC Health page: the \"Terminals\" section lists your tmux sessions."
echo "Which devices may open them is set by terminal_type in ~/.config/pc-health/config.toml"
echo "(\"local\" = this PC only, \"lan\" = other devices on your home network too)."
