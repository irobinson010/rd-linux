#!/usr/bin/env bash
# rd.sh -- start/stop/manage the remote-desktop server, correctly, including over SSH.
#
# The thing that makes SSH-starting work: a bare SSH shell has no link to your
# logged-in graphical session, so the screen-share portal can't find your saved
# grant and re-prompts. This script exports the right session env first, then drives
# the systemd --user service if installed (survives disconnect + auto-restart), or
# falls back to a detached direct run.
#
#   ./rd.sh install   # copy/update the systemd --user unit (do this once / after edits)
#   ./rd.sh start     # start (default if no argument)
#   ./rd.sh stop
#   ./rd.sh status
#   ./rd.sh log       # follow the log (shows the connect URL + token)
set -uo pipefail

# --- point this (possibly SSH) shell at the active user session ---------------
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
  for w in "$XDG_RUNTIME_DIR"/wayland-*; do
    [ -S "$w" ] && { export WAYLAND_DISPLAY="$(basename "$w")"; break; }
  done
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$HOME/.config/systemd/user/rdserver.service"
LOG="$PROJECT_DIR/rdserver.log"
# Options for the direct-run fallback. Load the generated token file if present (the
# same one the service uses); otherwise default to a RANDOM token (no --token), so
# nothing secret is hardcoded here. Run ./install.sh to generate a stable token.
RD_ENV="$HOME/.config/rdserver/rd.env"
[ -f "$RD_ENV" ] && . "$RD_ENV"
RD_OPTS="${RD_OPTS:---port 8098 --tls --audio --unattended}"

have_service() { [ -f "$UNIT" ]; }

show_url() {  # pull the connect URL out of recent logs
  local url
  url="$( { systemctl --user -q is-active rdserver >/dev/null 2>&1 \
              && journalctl --user -u rdserver -n 80 --no-pager 2>/dev/null \
              || tail -n 80 "$LOG" 2>/dev/null; } \
          | grep -oE 'https?://[^ ]*token=[^ ]+' | tail -1 )"
  [ -n "$url" ] && echo "connect: $url" || echo "(no URL yet -- check './rd.sh log')"
}

case "${1:-start}" in
  install)
    mkdir -p "$HOME/.config/systemd/user"
    cp "$PROJECT_DIR/deploy/rdserver.service" "$UNIT"
    systemctl --user daemon-reload
    echo "installed/updated $UNIT"
    echo "start now: ./rd.sh start   |   auto-start at login: systemctl --user enable rdserver"
    ;;
  start)
    if have_service; then
      echo "starting via systemd --user service..."
      systemctl --user restart rdserver || { echo "start failed -- ./rd.sh log"; exit 1; }
      sleep 1
    else
      echo "service not installed (./rd.sh install); running detached..."
      cd "$PROJECT_DIR"
      setsid nohup python3 -m rdserver $RD_OPTS >"$LOG" 2>&1 &
      sleep 2
    fi
    show_url
    ;;
  stop)
    if have_service; then
      systemctl --user stop rdserver && echo "stopped (service)" || echo "stop failed"
    else
      pkill -f "python3 -m rdserver" && echo "stopped (direct)" || echo "nothing running"
    fi
    ;;
  status)
    if have_service; then
      systemctl --user --no-pager status rdserver | head -n 12
    else
      pgrep -af "python3 -m rdserver" || echo "not running"
    fi
    ;;
  log)
    if have_service; then journalctl --user -u rdserver -n 40 -f
    else tail -n 40 -f "$LOG"; fi
    ;;
  *)
    echo "usage: $0 [install|start|stop|status|log]"; exit 2 ;;
esac
