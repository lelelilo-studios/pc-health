#!/usr/bin/env bash
# Stop PC-health and remove what install.sh added. Your settings and history are left in place.
set -euo pipefail
systemctl --user disable --now pc-health.service pc-health-terminal.service 2>/dev/null || true
systemctl --user disable pc-health-restore.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/pc-health.service" "$HOME/.config/systemd/user/pc-health-terminal.service" \
      "$HOME/.config/systemd/user/pc-health-restore.service" "$HOME/.local/share/applications/pc-health.desktop"
systemctl --user daemon-reload
echo "PC Health is stopped and no longer starts at login."
echo "Settings (if any): ~/.config/pc-health/    History: ~/.local/state/pc-health/"
