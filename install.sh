#!/usr/bin/env bash
# Install PC-health as a per-user service that starts when you log in. Undo with ./uninstall.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
APPS_DIR="$HOME/.local/share/applications"

mkdir -p "$UNIT_DIR" "$APPS_DIR"
sed "s|@DIR@|$HERE|g" "$HERE/pc-health.service" > "$UNIT_DIR/pc-health.service"

URL="$(cd "$HERE" && python3 -m pchealth url | head -n 1)"
cat > "$APPS_DIR/pc-health.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PC Health
Comment=Live health of this PC and its coding agents
Exec=xdg-open $URL
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
DESKTOP

systemctl --user daemon-reload
systemctl --user enable pc-health.service
systemctl --user restart pc-health.service
systemctl --user --no-pager --lines=0 status pc-health.service || true

echo
echo "PC Health is running. Open:"
(cd "$HERE" && python3 -m pchealth url | sed 's/^/  /')
echo
echo "It is also in your applications menu as \"PC Health\"."
