"""Headless WebRTC offer generator used by the regression tests.

Builds a webrtcbin H.264 send pipeline (mirroring rdserver/media.py's encoder +
framerate cap), creates an offer, applies set-local-description, and prints the
offer SDP to stdout. No desktop capture, no browser, no network.

It deliberately uses the *fixed* offer-handling idiom (holding the promise reply)
so that a regression of the PyGObject use-after-free shows up as a SIGSEGV exit.

  python3 tests/_gen_offer.py --fps 60
"""
from __future__ import annotations

import argparse
import os
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC, GLib  # noqa: E402,F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rdserver.media import _encoder_fragment, select_encoder  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--width", type=int, default=2560)
    ap.add_argument("--height", type=int, default=1440)
    ap.add_argument("--encoder", default="auto")
    args = ap.parse_args()

    Gst.init(None)
    enc = select_encoder(args.encoder)
    if not enc and args.encoder != "auto":
        return 3                                 # that encoder family isn't here
    encfrag = _encoder_fragment(enc or "x264enc", 8000)

    desc = (
        f"videotestsrc is-live=true ! videoconvert ! videoscale ! videorate ! "
        f"video/x-raw,format=NV12,width={args.width},height={args.height},"
        f"framerate={args.fps}/1 ! "
        f"{encfrag} ! video/x-h264,profile=high ! h264parse config-interval=-1 ! "
        f"rtph264pay config-interval=-1 pt=96 ! "
        f"application/x-rtp,media=video,encoding-name=H264,payload=96 ! "
        f"webrtcbin name=wb bundle-policy=max-bundle latency=0"
    )
    pipe = Gst.parse_launch(desc)
    wb = pipe.get_by_name("wb")
    loop = GLib.MainLoop()
    out: dict = {}

    def on_offer(promise, _):
        promise.wait()
        reply = promise.get_reply()              # hold the reply (UAF fix)
        offer = reply.get_value("offer")
        wb.emit("set-local-description", offer, Gst.Promise.new())  # crashes if regressed
        out["sdp"] = offer.sdp.as_text()
        loop.quit()

    wb.connect(
        "on-negotiation-needed",
        lambda _e: wb.emit("create-offer", None,
                           Gst.Promise.new_with_change_func(on_offer, None)))
    pipe.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
    loop.run()
    pipe.set_state(Gst.State.NULL)

    if "sdp" not in out:
        sys.stderr.write("no offer produced\n")
        return 2
    sys.stdout.write(out["sdp"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
