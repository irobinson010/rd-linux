"""Per-monitor capture probe (no WebRTC, no network).

Run after picking BOTH screens in the share dialog:

    python3 -m rdserver.smoketest

It captures + NVENC-encodes EACH shared monitor for a few seconds and reports how
many frames each produced. Use it to tell whether a black screen is a capture
problem (0 frames for that monitor) or something downstream.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from rdserver.media import encoder_available, fit_within  # noqa: E402
from rdserver.portal import Portal  # noqa: E402

MAX_W, MAX_H = 2560, 1440


def run_capture(portal: Portal, stream: dict, seconds: float = 3.0) -> tuple[int, str | None]:
    fd = portal.open_pipewire_fd()
    w, h = fit_within(stream["width"] or MAX_W, stream["height"] or MAX_H, MAX_W, MAX_H)
    enc = encoder_available() or "x264enc"
    encfrag = ("nvh264enc rc-mode=cbr zerolatency=true" if enc == "nvh264enc"
               else "x264enc tune=zerolatency speed-preset=ultrafast")
    desc = (
        f"pipewiresrc fd={fd} path={stream['node_id']} do-timestamp=true "
        f"keepalive-time=1000 ! queue leaky=downstream max-size-buffers=4 ! "
        f"videoconvert ! videoscale add-borders=true ! "
        f"video/x-raw,format=NV12,width={w},height={h} ! "
        f"{encfrag} ! video/x-h264,profile=constrained-baseline ! "
        f"h264parse ! fakesink name=sink sync=false"
    )
    pipeline = Gst.parse_launch(desc)
    count = {"n": 0}
    pipeline.get_by_name("sink").get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER,
        lambda _p, _i: (count.__setitem__("n", count["n"] + 1), Gst.PadProbeReturn.OK)[1])
    loop = GLib.MainLoop()
    err = {"msg": None}
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err["msg"] = msg.parse_error()[0].message
            loop.quit()
    hid = bus.connect("message", on_msg)
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add(int(seconds * 1000), lambda: (loop.quit(), False)[1])
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    bus.disconnect(hid)
    bus.remove_signal_watch()
    return count["n"], err["msg"]


def main() -> int:
    Gst.init(None)
    print("Negotiating portal (pick BOTH screens, then Allow)...")
    portal = Portal(cursor=True)
    portal.negotiate()
    print(f"shared {len(portal.streams)} monitor(s)\n")

    all_ok = True
    for i, s in enumerate(portal.streams):
        sys.stdout.write(f"  monitor {i}: {s['width']}x{s['height']} "
                         f"(node {s['node_id']}, pos {s['x']},{s['y']}) ... ")
        sys.stdout.flush()
        n, errmsg = run_capture(portal, s)
        if n > 0:
            print(f"OK  ({n} frames)")
        else:
            all_ok = False
            print(f"NO FRAMES  [{errmsg or 'no error reported'}]")

    print("\n" + ("All monitors capture OK." if all_ok else
                  "A monitor produced no frames -- that's the black screen."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
