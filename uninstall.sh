#!/usr/bin/env bash
# uninstall.sh -- remove the systemd service and generated config/cache.
#
# Does NOT remove apt packages (they may be shared with other apps) or this source
# checkout. Run as your normal user:
#
#     ./uninstall.sh
set -uo pipefail
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "==> Stopping + disabling the service..."
systemctl --user stop rdserver rd-tray 2>/dev/null || true
systemctl --user disable rdserver rd-tray 2>/dev/null || true

for u in rdserver rd-tray; do
  f="$HOME/.config/systemd/user/$u.service"
  if [ -f "$f" ]; then rm -f "$f" && echo "  removed $f"; fi
done
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed rdserver rd-tray 2>/dev/null || true

echo "==> Removing generated config + cached cert/token..."
rm -rf "$HOME/.config/rdserver" && echo "  removed ~/.config/rdserver (access token)"
rm -rf "$HOME/.cache/rdserver"  && echo "  removed ~/.cache/rdserver (TLS cert, screencast restore token)"

cat <<EOF

Done. Left in place (remove by hand if you want):
  - apt packages           (shared; 'sudo apt remove <pkg>' if truly unused)
  - AV1 plugin             ~/.local/lib/gstreamer-1.0 (only if you ran install-av1.sh)
  - this source directory  (just delete the folder)

Note: removing the screencast restore token means the KDE share dialog will appear
again the next time you install and start.
EOF
