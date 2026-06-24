#!/usr/bin/env bash
# Build the AV1 RTP payloader (rtpav1pay/rtpav1depay) from gst-plugins-rs.
# Ubuntu doesn't package the Rust GStreamer plugins, so we build the rtp plugin.
# Your RTX 4080 already has hardware AV1 (nvav1enc); this provides the missing
# WebRTC packetiser. Takes a few minutes (compiles a lot of Rust crates).
set -euo pipefail

echo "==> Installing build tools (Rust + cargo-c + GStreamer dev headers)"
sudo apt-get update
sudo apt-get install -y cargo cargo-c git pkg-config \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev

SRC="$HOME/src/gst-plugins-rs"
mkdir -p "$HOME/src"
if [ ! -d "$SRC" ]; then
  echo "==> Cloning gst-plugins-rs"
  git clone --depth 1 https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$SRC"
fi
cd "$SRC"

PLUGDIR="$HOME/.local/lib/gstreamer-1.0"
echo "==> Building + installing the rtp plugin into $PLUGDIR"
cargo cinstall -p gst-plugin-rtp --release \
  --prefix="$HOME/.local" --libdir="$HOME/.local/lib"

echo
echo "==> Verifying (GStreamer must scan $PLUGDIR)"
export GST_PLUGIN_PATH="$PLUGDIR:${GST_PLUGIN_PATH:-}"
if gst-inspect-1.0 rtpav1pay >/dev/null 2>&1; then
  echo "rtpav1pay: OK"
  echo
  echo "Add this to your shell rc (and the server's environment) so it's found:"
  echo "    export GST_PLUGIN_PATH=$PLUGDIR:\$GST_PLUGIN_PATH"
else
  echo "rtpav1pay still missing -- paste the build output and I'll sort the"
  echo "version match (gst-plugins-rs branch vs your GStreamer 1.26)."
fi
