"""GStreamer webrtcbin pipeline: capture the whole desktop -> crop one monitor ->
NVENC -> WebRTC, plus an input return path.

KDE's portal only ever hands back ONE stream covering the whole desktop (a logical
frame = bounding box of all monitors), regardless of what you pick. So we capture
that single stream and `videocrop` to the chosen monitor. Switching monitors is a
live crop change (one continuous stream, constant encode size -> no decoder freeze).
Resolution changes reconnect (client passes w/h). Monitor geometry comes from
kscreen-doctor; the crop is computed against the *actual* captured frame size read
at runtime, so it's robust to logical/physical pixel differences.

    pipewiresrc(desktop) -> videoconvert -> videocrop -> videoscale -> NV12 ->
    nvh264enc -> h264parse -> rtph264pay -> webrtcbin  (+ audio, + input channel)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
# NOTE: do NOT import GstVideo here -- on this Python 3.14 + GStreamer GI build it
# corrupts GstSDPMessage copying and segfaults set-local-description. Keyframes on
# monitor switch are requested by the browser via RTCP PLI instead.
from gi.repository import Gst, GstSdp, GstWebRTC  # noqa: E402

from rdserver import keymap
from rdserver.portal import Portal

log = logging.getLogger("media")

AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1

# Congestion-control floor: never let the GCC estimator starve the encoder below
# this (bits/sec). The ceiling is the configured/selected bitrate.
_GCC_MIN_BPS = 1_000_000


def encoder_available() -> str:
    if Gst.ElementFactory.find("nvh264enc"):
        return "nvh264enc"
    if Gst.ElementFactory.find("x264enc"):
        return "x264enc"
    return ""


def default_monitor() -> str | None:
    try:
        out = subprocess.run(["pactl", "get-default-sink"],
                             capture_output=True, text=True, timeout=2)
        sink = out.stdout.strip()
        return f"{sink}.monitor" if sink else None
    except Exception:
        return None


def monitor_layout() -> list[dict]:
    """Enabled outputs as [{name, x, y, w, h}] in logical coords (matching the
    portal's combined frame), normalised to a (0,0) origin, left-to-right."""
    try:
        out = subprocess.run(["kscreen-doctor", "-j"],
                             capture_output=True, text=True, timeout=3)
        data = json.loads(out.stdout)
    except Exception:
        return []
    mons = []
    for o in data.get("outputs", []):
        if not o.get("enabled"):
            continue
        pos = o.get("pos") or {}
        size = o.get("size") or {}
        scale = o.get("scale") or 1          # `size` is physical; pos/frame logical
        w = int(round(int(size.get("width", 0)) / scale))
        h = int(round(int(size.get("height", 0)) / scale))
        if w and h:
            mons.append({"name": o.get("name", "?"),
                         "x": int(pos.get("x", 0)), "y": int(pos.get("y", 0)),
                         "w": w, "h": h})
    mons.sort(key=lambda m: (m["x"], m["y"]))
    if mons:
        minx = min(m["x"] for m in mons)
        miny = min(m["y"] for m in mons)
        for m in mons:
            m["x"] -= minx
            m["y"] -= miny
    return mons


def fit_within(w: int, h: int, max_w: int, max_h: int) -> tuple[int, int]:
    if w <= 0 or h <= 0:
        return w, h
    scale = min(1.0, max_w / w, max_h / h)
    return max(2, (int(w * scale) // 2) * 2), max(2, (int(h * scale) // 2) * 2)


def _even(n: float) -> int:        # for dimensions (>= 2)
    return max(2, (int(round(n)) // 2) * 2)


def _even0(n: float) -> int:       # for crop offsets (>= 0)
    return max(0, (int(round(n)) // 2) * 2)


def _encoder_fragment(name: str, bitrate_kbps: int) -> str:
    if name == "nvh264enc":
        return (f"nvh264enc name=enc bitrate={bitrate_kbps} gop-size=30 "
                f"rc-mode=cbr preset=low-latency-hq zerolatency=true")
    return (f"x264enc name=enc tune=zerolatency speed-preset=ultrafast "
            f"bitrate={bitrate_kbps} key-int-max=30")


def _video_chain(vmode: str, h264_enc: str, bitrate_kbps: int) -> tuple[str, str]:
    """Encoder->payloader fragment for the requested video mode. Returns
    (fragment, effective mode). Modes:
      high     - H.264 High profile (CABAC). Best quality; needs hardware decode.
      baseline - H.264 constrained-baseline. Software-decodable, but Firefox and
                 Chromium-on-Linux/NVIDIA often have no usable H.264 in WebRTC.
      vp8      - VP8 (software encode). Decodes in EVERY browser; the universal
                 fallback for Linux browsers / NVIDIA boxes with no H.264 HW decode.
      av1      - NVENC AV1 (needs nvav1enc + rtpav1pay AND a HW-AV1 client).
    Anything unavailable falls through to H.264 high."""
    if (vmode == "av1" and Gst.ElementFactory.find("nvav1enc")
            and Gst.ElementFactory.find("rtpav1pay")):
        frag = (f"nvav1enc name=enc bitrate={bitrate_kbps} gop-size=30 rc-mode=cbr "
                f"preset=p4 tune=ultra-low-latency ! "
                f"av1parse ! rtpav1pay ! "
                f"application/x-rtp,media=video,encoding-name=AV1,payload=96")
        return frag, "av1"
    if (vmode == "vp8" and Gst.ElementFactory.find("vp8enc")
            and Gst.ElementFactory.find("rtpvp8pay")):
        # No NVENC VP8 -> software encode. deadline=1/cpu-used=6 keeps it realtime;
        # target-bitrate is in bits/sec (note: not the kbps that NVENC uses).
        frag = (f"videoconvert ! vp8enc name=enc deadline=1 cpu-used=6 "
                f"target-bitrate={bitrate_kbps * 1000} keyframe-max-dist=30 "
                f"error-resilient=default ! rtpvp8pay pt=96 ! "
                f"application/x-rtp,media=video,encoding-name=VP8,payload=96")
        return frag, "vp8"
    h264_profile = "constrained-baseline" if vmode == "baseline" else "high"
    frag = (f"{_encoder_fragment(h264_enc, bitrate_kbps)} ! "
            f"video/x-h264,profile={h264_profile} ! h264parse config-interval=-1 ! "
            f"rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 ! "
            f"application/x-rtp,media=video,encoding-name=H264,payload=96")
    return frag, ("baseline" if h264_profile == "constrained-baseline" else "high")


class MediaSession:
    def __init__(self, portal: Portal, *, send_cb: Callable[[dict], None],
                 bitrate_kbps: int = 20000, force_software: bool = False,
                 rtp_port_min: int = 50000, rtp_port_max: int = 50019,
                 max_width: int = 2560, max_height: int = 1440,
                 monitor_index: int = 0, audio: bool = False, vmode: str = "high",
                 congestion_control: bool = False, injector=None,
                 on_error: Callable[[str], None] | None = None):
        self.portal = portal
        self.send = send_cb
        self.on_error = on_error
        self._closed = False
        # Input backend: a drop-in injector (uinput, for unattended mode) that
        # mirrors the portal's injection API, or the portal itself (default).
        # Capture always comes from the portal regardless.
        self.injector = injector if injector is not None else portal

        enc = "x264enc" if force_software else (encoder_available() or "x264enc")
        if not Gst.ElementFactory.find(enc):
            raise RuntimeError("no usable H.264 encoder (nvh264enc/x264enc)")
        self.encoder_name = enc
        self.video_chain, self.codec = _video_chain(vmode, enc, bitrate_kbps)
        if vmode in ("av1", "vp8") and self.codec != vmode:
            log.warning("%s requested but unavailable; falling back to H.264", vmode)
        log.info("video mode: %s", self.codec)

        self.combined_node = portal.node_id
        self.frame_w = portal.width or max_width     # refined from real caps below
        self.frame_h = portal.height or max_height
        self.monitors = monitor_layout()
        if not self.monitors:
            self.monitors = [{"name": "screen", "x": 0, "y": 0,
                              "w": self.frame_w, "h": self.frame_h}]
        self.logical_w = max(m["x"] + m["w"] for m in self.monitors)
        self.logical_h = max(m["y"] + m["h"] for m in self.monitors)
        self.active = max(0, min(monitor_index, len(self.monitors) - 1))
        self.enc_w, self.enc_h = max_width, max_height
        # active-crop state (set by _apply_crop), used for input mapping:
        self._cl = self._ct = 0      # crop left/top in actual frame px
        self._cw = self.frame_w      # cropped width/height in actual frame px
        self._ch = self.frame_h
        self._scale_m = 1.0
        self._off_x = self._off_y = 0.0

        # Cap the framerate. Without a framerate cap, pipewiresrc negotiates a very
        # high rate (observed 240fps), which makes NVENC stamp H.264 *level 6.0*
        # (profile-level-id=42c03c) into the SDP. Browsers' WebRTC H.264 receiver
        # refuses level 6.0 and answers `m=video 0` (rejected) -> black screen, even
        # though capture/encode are fine. 60fps -> level 5.1, which browsers accept.
        self.fps = 60
        sized = (f"video/x-raw,format=NV12,width={self.enc_w},"
                 f"height={self.enc_h},pixel-aspect-ratio=1/1,framerate={self.fps}/1")
        fd = portal.open_pipewire_fd()
        # When audio is on, the PulseAudio device is the pipeline MASTER clock (set in
        # the audio branch below). The video source must then NOT provide a clock --
        # otherwise it wins the clock election and the audio, captured on a different
        # hardware clock that ticks slightly faster, is consumed at the video clock's
        # rate, so the audio queue fills and drops every buffer forever (the observed
        # constant "audio queue overrun"). videorate absorbs video against this clock.
        pw_clock = "provide-clock=false " if audio else ""
        parts = [
            f"pipewiresrc fd={fd} path={self.combined_node} do-timestamp=true "
            f"{pw_clock}keepalive-time=1000 ! queue leaky=downstream max-size-buffers=4 ! "
            f"videoconvert ! videocrop name=crop ! "
            f"videoscale add-borders=true ! videorate ! capsfilter name=outcaps ! "
            f"{self.video_chain} ! "
            f"webrtcbin name=webrtc bundle-policy=max-bundle latency=0",
        ]

        self.audio_on = False
        if audio:
            amon = default_monitor()
            if amon:
                parts.append(
                    # pulsesrc provides the pipeline MASTER clock, so audio is drained
                    # at exactly the rate it's captured -> no more continuous queue
                    # overrun/drops. The video source is provide-clock=false (above)
                    # so this wins the clock election.
                    f'pulsesrc name=asrc device="{amon}" provide-clock=true ! '
                    f"queue name=aqueue leaky=downstream max-size-time=200000000 "
                    f"max-size-buffers=0 max-size-bytes=0 ! "
                    f"audioconvert ! audioresample ! "
                    f"audio/x-raw,rate=48000,channels=2 ! "
                    f"opusenc bitrate=128000 ! rtpopuspay pt=97 ! "
                    f"application/x-rtp,media=audio,encoding-name=OPUS,payload=97 ! "
                    f"webrtc."
                )
                self.audio_on = True
                log.info("audio: capturing %s", amon)

        desc = "  ".join(parts)
        log.info("pipeline (desktop %dx%d logical %dx%d, %d monitor(s)): %s",
                 self.frame_w, self.frame_h, self.logical_w, self.logical_h,
                 len(self.monitors), desc)
        self.pipeline = Gst.parse_launch(desc)
        self.webrtc = self.pipeline.get_by_name("webrtc")
        self.crop = self.pipeline.get_by_name("crop")
        self.outcaps = self.pipeline.get_by_name("outcaps")
        self.outcaps.set_property("caps", Gst.Caps.from_string(sized))
        self.encoder = self.pipeline.get_by_name("enc")
        # Diagnostic: if the audio queue fills and starts leaking, that's a concrete
        # choppy-audio cause (downstream backpressure) -- log it (throttled).
        self._aq_overruns = 0
        aq = self.pipeline.get_by_name("aqueue")
        if aq is not None:
            aq.connect("overrun", self._on_audio_overrun)
        # Congestion control: the configured/selected bitrate is the CEILING; GCC
        # (rtpgccbwe, created by webrtcbin once transport-cc is negotiated) adapts the
        # encoder within [_GCC_MIN_BPS, ceiling] from its bandwidth estimate. If the
        # estimator isn't available, set_bitrate() drives the encoder directly.
        self.max_bitrate_kbps = bitrate_kbps
        self.congestion_control = congestion_control
        self._gcc = None
        self._gcc_last_kbps = 0
        self._apply_crop(self.active)

        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        for prop in ("notify::ice-connection-state", "notify::connection-state",
                     "notify::ice-gathering-state", "notify::signaling-state"):
            self.webrtc.connect(prop, self._log_state)
        # webrtcbin auto-instantiates rtpgccbwe deep in its send chain. We only
        # bind+drive it when adaptive bitrate is enabled: driving the encoder from
        # the GCC estimate churned the bitrate and glitched audio over tunneled
        # links (Twingate), so it's opt-in (--abr). When off, the encoder holds the
        # fixed/selected bitrate and webrtcbin keeps its default internal pacing
        # (the original, known-good audio behaviour).
        if self.congestion_control:
            self.webrtc.connect("deep-element-added", self._on_deep_element_added)

        self._start_bus_watch()

        self.pipeline.set_state(Gst.State.READY)
        self.channel = self.webrtc.emit("create-data-channel", "input", None)
        if self.channel is not None:
            self.channel.connect("on-message-string", self._on_input)
            self.channel.connect(
                "on-open", lambda _c: log.info("input datachannel OPEN"))
        else:
            log.warning("create-data-channel returned None; input disabled")

        self.pipeline.set_state(Gst.State.PLAYING)
        # Once PLAYING, read the REAL capture frame size and re-crop if it differs
        # from the portal-reported size. Done on this thread (never from a
        # streaming-thread pad probe -- mutating videocrop there segfaults).
        self.pipeline.get_state(3 * Gst.SECOND)
        if self.audio_on:
            clk = self.pipeline.get_clock()
            # Expect GstAudioClock here -> the sound card won the election (the fix).
            # GstSystemClock/other means audio is still slaved to another clock.
            log.info("pipeline master clock: %s (%s)",
                     clk.get_name() if clk else "none",
                     type(clk).__name__ if clk else "-")
        self._recrop_to_actual_frame()
        if hasattr(self.injector, "set_bounds"):
            # tell the uinput injector the capture-frame size to normalise against
            self.injector.set_bounds(self.frame_w, self.frame_h)
        self._announce_monitors()

    def _recrop_to_actual_frame(self) -> None:
        caps = self.crop.get_static_pad("sink").get_current_caps()
        if not caps:
            log.warning("no capture caps yet; crop assumes %dx%d",
                        self.frame_w, self.frame_h)
            return
        s = caps.get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        log.info("ACTUAL capture frame %sx%s (portal reported %dx%d)",
                 w, h, self.frame_w, self.frame_h)
        if w and h and (w, h) != (self.frame_w, self.frame_h):
            self.frame_w, self.frame_h = w, h
            self._apply_crop(self.active)

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._release_modifiers()
        except Exception:
            pass
        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

    def _start_bus_watch(self) -> None:
        bus = self.pipeline.get_bus()

        def watch() -> None:
            while not self._closed:
                msg = bus.timed_pop_filtered(
                    100 * Gst.MSECOND,
                    Gst.MessageType.ERROR | Gst.MessageType.EOS
                    | Gst.MessageType.WARNING)
                if msg is None:
                    continue
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    log.error("pipeline error: %s (%s)", err.message, dbg)
                    if self.on_error:
                        self.on_error(err.message)
                    return
                if msg.type == Gst.MessageType.WARNING:
                    err, dbg = msg.parse_warning()
                    log.warning("pipeline warning: %s (%s)", err.message, dbg)
                elif msg.type == Gst.MessageType.EOS:
                    log.info("pipeline EOS")
                    return

        threading.Thread(target=watch, name="gst-bus", daemon=True).start()

    # ----- signaling: offer/answer/ICE ------------------------------------

    def _log_state(self, element, pspec) -> None:
        log.info("webrtc %s = %s", pspec.name, element.get_property(pspec.name))

    def _on_negotiation_needed(self, _element) -> None:
        log.info("negotiation-needed -> creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        self.webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, _data) -> None:
        promise.wait()
        # Keep `reply` alive for the whole method. get_value("offer") returns a
        # value that BORROWS memory owned by this GstStructure; the common idiom
        # `promise.get_reply().get_value("offer")` frees the structure immediately,
        # leaving `offer` dangling, and set-local-description's deep copy then
        # segfaults on the freed GstSDPMessage (PyGObject use-after-free, verified
        # 12/12 crash vs 0/12 when the reply is held).
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        sdp_text = offer.sdp.as_text()
        if os.environ.get("RD_DUMP_SDP"):     # opt-in debug dump (off by default)
            try:
                open("/tmp/rd-offer.sdp", "w").write(sdp_text)
            except OSError:
                pass
        log.info("offer created -> sending to browser")
        self.send({"type": "offer", "sdp": sdp_text})

    def _on_ice_candidate(self, _element, mline_index: int, candidate: str) -> None:
        self.send({"type": "ice", "sdpMLineIndex": int(mline_index),
                   "candidate": candidate})

    def set_remote_answer(self, sdp_text: str) -> None:
        log.info("remote answer received -> set-remote-description")
        if os.environ.get("RD_DUMP_SDP"):     # opt-in debug dump (off by default)
            try:
                open("/tmp/rd-answer.sdp", "w").write(sdp_text)
            except OSError:
                pass
        _res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())

    def add_ice(self, mline_index: int, candidate: str) -> None:
        self.webrtc.emit("add-ice-candidate", int(mline_index), candidate)

    # ----- monitor crop / controls ----------------------------------------

    def _apply_crop(self, index: int) -> None:
        m = self.monitors[index]
        rx = self.frame_w / self.logical_w
        ry = self.frame_h / self.logical_h
        left = _even0(m["x"] * rx)
        top = _even0(m["y"] * ry)
        cw = min(_even(m["w"] * rx), self.frame_w - left)
        ch = min(_even(m["h"] * ry), self.frame_h - top)
        self.crop.set_property("left", left)
        self.crop.set_property("top", top)
        self.crop.set_property("right", max(0, self.frame_w - (left + cw)))
        self.crop.set_property("bottom", max(0, self.frame_h - (top + ch)))
        self.active = index
        self._cl, self._ct, self._cw, self._ch = left, top, cw, ch
        scale = min(self.enc_w / cw, self.enc_h / ch)
        self._scale_m = scale
        self._off_x = (self.enc_w - cw * scale) / 2.0
        self._off_y = (self.enc_h - ch * scale) / 2.0

    def switch_monitor(self, index: int) -> None:
        if not (0 <= index < len(self.monitors)):
            return
        self._apply_crop(index)   # browser PLI requests a keyframe for the new crop
        log.info("switched to monitor %d (%s)", index, self.monitors[index]["name"])

    def _on_deep_element_added(self, _bin, _sub_bin, element) -> None:
        factory = element.get_factory()
        if not factory or factory.get_name() != "rtpgccbwe":
            return
        max_bps = self.max_bitrate_kbps * 1000
        try:
            element.set_property("min-bitrate", _GCC_MIN_BPS)
            element.set_property("max-bitrate", max_bps)
            element.set_property("estimated-bitrate", min(max_bps, 8_000_000))
        except Exception as e:
            log.warning("gcc: could not bound estimator: %s", e)
        self._gcc = element
        element.connect("notify::estimated-bitrate", self._on_gcc_estimate)
        log.info("congestion control active (GCC): %d-%d kbps",
                 _GCC_MIN_BPS // 1000, max_bps // 1000)

    def _on_gcc_estimate(self, element, _pspec) -> None:
        bps = element.get_property("estimated-bitrate")
        kbps = max(500, bps // 1000)
        if self.audio_on:
            kbps = max(500, kbps - 150)        # leave headroom for the opus track
        # Apply only on a meaningful change, to avoid encoder churn / log spam.
        if (self._gcc_last_kbps
                and abs(kbps - self._gcc_last_kbps) < self._gcc_last_kbps * 0.05):
            return
        self._gcc_last_kbps = kbps
        self._set_encoder_bitrate(kbps)

    def _set_encoder_bitrate(self, kbps: int) -> None:
        try:
            if self.codec == "vp8":
                self.encoder.set_property("target-bitrate", kbps * 1000)  # bits/sec
            else:
                self.encoder.set_property("bitrate", kbps)                # kbps
            log.info("bitrate -> %d kbps", kbps)
        except Exception as e:
            log.warning("could not set bitrate: %s", e)

    def set_bitrate(self, kbps: int) -> None:
        # Client-driven bitrate = the ceiling. With GCC active, raise/lower the
        # estimator's max and let it adapt; otherwise drive the encoder directly.
        kbps = max(500, int(kbps))
        self.max_bitrate_kbps = kbps
        if self._gcc is not None:
            try:
                self._gcc.set_property("max-bitrate", kbps * 1000)
                log.info("gcc ceiling -> %d kbps", kbps)
            except Exception as e:
                log.warning("could not set gcc ceiling: %s", e)
            return
        self._set_encoder_bitrate(kbps)

    def _on_audio_overrun(self, _queue) -> None:
        # Audio queue full -> it's leaking (dropping) audio. A real choppy-audio
        # cause from downstream backpressure. Throttle so it can't spam the log.
        self._aq_overruns += 1
        if self._aq_overruns <= 3 or self._aq_overruns % 50 == 0:
            log.warning("audio queue overrun #%d -- dropping audio (backpressure)",
                        self._aq_overruns)

    def _announce_monitors(self) -> None:
        self.send({
            "type": "monitors",
            "active": self.active,
            "list": [{"index": i, "width": m["w"], "height": m["h"],
                      "name": m["name"]} for i, m in enumerate(self.monitors)],
        })

    # ----- input return path ----------------------------------------------

    def _on_input(self, _channel, message: str) -> None:
        try:
            ev = json.loads(message)
        except ValueError:
            return
        try:
            self._dispatch(ev)
        except Exception:
            log.exception("input dispatch failed for %r", ev)

    def _dispatch(self, ev: dict) -> None:
        t = ev.get("t")
        p = self.injector
        if t == "move":
            # Normalized [0,1] of the encoded frame -> cropped-region px -> absolute
            # px in the combined desktop frame (the single capture node).
            px = max(0.0, min(1.0, ev["x"])) * self.enc_w
            py = max(0.0, min(1.0, ev["y"])) * self.enc_h
            lx = max(0.0, min(float(self._cw), (px - self._off_x) / self._scale_m))
            ly = max(0.0, min(float(self._ch), (py - self._off_y) / self._scale_m))
            p.pointer_motion_absolute(self._cl + lx, self._ct + ly,
                                      node_id=self.combined_node)
        elif t == "monitor":
            self.switch_monitor(int(ev.get("index", 0)))
        elif t == "button":
            btn = keymap.button_for(ev["button"])
            if btn is not None:
                p.pointer_button(btn, bool(ev["pressed"]))
        elif t == "wheel":
            dy = ev.get("dy", 0.0)
            dx = ev.get("dx", 0.0)
            if dy:
                p.pointer_axis_discrete(AXIS_VERTICAL, -1 if dy > 0 else 1)
            if dx:
                p.pointer_axis_discrete(AXIS_HORIZONTAL, -1 if dx > 0 else 1)
        elif t == "key":
            kc = keymap.keycode_for(ev["code"])
            if kc is not None:
                p.keyboard_keycode(kc, bool(ev["pressed"]))
        elif t == "releaseall":
            self._release_modifiers()
        elif t == "bitrate":
            self.set_bitrate(int(ev.get("kbps", 20000)))

    def _release_modifiers(self) -> None:
        for kc in (29, 97, 42, 54, 56, 100, 125, 126):  # ctrl/shift/alt/meta L+R
            self.injector.keyboard_keycode(kc, False)
