#!/usr/bin/env bash
# install.sh -- one-shot installer for the Wayland WebRTC remote-desktop server.
#
# Installs system dependencies, generates a random access token, and sets up the
# systemd --user service pointing at this checkout. Run as your NORMAL user from the
# repo directory -- it uses sudo only for the apt step:
#
#     ./install.sh
#
# KDE Plasma (Wayland) + NVIDIA/NVENC is the target; software x264 is a fallback.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 1. system packages ------------------------------------------------------
PKGS=(
  gstreamer1.0-tools                 # gst-launch/gst-inspect (debugging)
  gstreamer1.0-pipewire              # pipewiresrc: capture the portal's PipeWire node
  gstreamer1.0-plugins-base          # videoconvert, videoscale, audioconvert, opus
  gstreamer1.0-plugins-good          # rtp payloaders, pulsesrc, queue, videorate
  gstreamer1.0-plugins-bad           # webrtcbin, nvcodec (nvh264enc/NVENC), dtls/srtp
  gstreamer1.0-plugins-ugly          # x264enc (software fallback)
  gstreamer1.0-nice                  # libnice ICE backend for webrtcbin
  gstreamer1.0-libav                 # extra codecs / fallback
  gir1.2-gst-plugins-bad-1.0         # GstWebRTC + GstSdp typelibs (Python imports)
  python3-gi                         # PyGObject
  python3-aiohttp                    # HTTP + WebSocket signaling
  python3-evdev                      # virtual input device for --unattended mode
  pulseaudio-utils                   # pactl (find the default sink for --audio)
  openssl                            # self-signed TLS cert for --tls
)
echo "==> Installing system packages (sudo apt)..."
sudo apt-get update
sudo apt-get install -y "${PKGS[@]}"

# ---- 2. verify the critical GStreamer elements ------------------------------
echo
echo "==> Checking GStreamer elements:"
for el in pipewiresrc webrtcbin rtph264pay opusenc videoconvert nicesink; do
  if gst-inspect-1.0 "$el" >/dev/null 2>&1; then echo "  OK      $el"
  else echo "  MISSING $el   <-- investigate"; fi
done
if gst-inspect-1.0 nvh264enc >/dev/null 2>&1; then
  echo "  OK      nvh264enc (NVENC hardware H.264)"
else
  echo "  NOTE    nvh264enc missing -- needs the NVIDIA driver; will use software x264 (heavier)."
fi
command -v kscreen-doctor >/dev/null 2>&1 \
  || echo "  NOTE    kscreen-doctor not found -- multi-monitor cropping falls back to one screen"
echo "          (it ships with KDE Plasma; install your distro's libkfNscreen-bin to enable it)."

# ---- 3. generate a stable random token (kept OUT of the repo) ---------------
CFG_DIR="$HOME/.config/rdserver"
ENV_FILE="$CFG_DIR/rd.env"
mkdir -p "$CFG_DIR"
if [ -f "$ENV_FILE" ]; then
  echo
  echo "==> Keeping existing token in $ENV_FILE (delete it to regenerate)."
else
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
  ( umask 077; printf 'RD_OPTS="--port 8098 --tls --audio --unattended --token %s"\n' \
      "$TOKEN" > "$ENV_FILE" )
  chmod 600 "$ENV_FILE"
  echo
  echo "==> Generated a random access token -> $ENV_FILE (mode 600, not in the repo)."
fi

# ---- 4. install the systemd --user service ----------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
# Rewrite WorkingDirectory to THIS checkout so the unit is location-independent.
sed -E "s|^WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
  "$PROJECT_DIR/deploy/rdserver.service" > "$UNIT_DIR/rdserver.service"
systemctl --user daemon-reload 2>/dev/null || true
echo
echo "==> Installed systemd --user service (rdserver) -> $UNIT_DIR/rdserver.service"

# ---- 5. next steps ----------------------------------------------------------
cat <<EOF

Install complete.

  Start it:            ./rd.sh start
  Auto-start at login: systemctl --user enable rdserver
  Status / logs:       ./rd.sh status   |   ./rd.sh log
  Stop:                ./rd.sh stop

On first start, approve the KDE screen-share dialog ONCE -- it's remembered, so
later restarts (including over SSH via ./rd.sh) come up without prompting.

Optional: hardware AV1 (NVENC) support -> ./install-av1.sh
EOF
