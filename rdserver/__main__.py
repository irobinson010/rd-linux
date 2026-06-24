"""Entry point: python3 -m rdserver [options]

Negotiates the portal session once (KDE will show a share-screen dialog the first
time), then serves the WebRTC remote-desktop client.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import ssl
import subprocess
from pathlib import Path

# Make the locally-built gst-plugins-rs (rtpav1pay, for AV1) discoverable before
# GStreamer scans plugins. Harmless if the dir doesn't exist.
_LOCAL_GST = os.path.expanduser("~/.local/lib/gstreamer-1.0")
if os.path.isdir(_LOCAL_GST):
    os.environ["GST_PLUGIN_PATH"] = (
        _LOCAL_GST + os.pathsep + os.environ.get("GST_PLUGIN_PATH", ""))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from aiohttp import web  # noqa: E402

from rdserver.media import encoder_available  # noqa: E402
from rdserver.portal import Portal  # noqa: E402
from rdserver.signaling import ScrubAccessLogger, Server  # noqa: E402


def detect_monitor_count() -> int:
    """Number of enabled outputs (so we know how many share dialogs to request)."""
    try:
        out = subprocess.run(["kscreen-doctor", "-j"],
                             capture_output=True, text=True, timeout=3)
        n = sum(1 for o in json.loads(out.stdout).get("outputs", [])
                if o.get("enabled"))
        return max(1, n)
    except Exception:
        return 1


def primary_ip() -> str:
    """Best-effort primary outbound IPv4 (no traffic actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _ensure_self_signed(ip: str) -> tuple[str, str]:
    """Return (cert, key) paths, generating a cached self-signed pair if absent."""
    d = Path.home() / ".cache" / "rdserver"
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "cert.pem", d / "key.pem"
    if not (cert.exists() and key.exists()):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", str(key), "-out", str(cert), "-days", "825", "-nodes",
             "-subj", "/CN=rdserver",
             "-addext", f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost"],
            check=True, capture_output=True)
        key.chmod(0o600)
    return str(cert), str(key)


def main() -> int:
    ap = argparse.ArgumentParser(prog="rdserver")
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default: 0.0.0.0; Twingate gates access)")
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("--bitrate", type=int, default=20000,
                    help="initial video bitrate in kbps (default 20000; "
                         "changeable live from the browser)")
    ap.add_argument("--abr", action="store_true",
                    help="adaptive bitrate via WebRTC congestion control (GCC): the "
                         "encoder follows the live bandwidth estimate. OFF by default "
                         "-- the dynamic bitrate can glitch audio over tunneled links "
                         "(e.g. Twingate); when off, bitrate stays fixed at --bitrate "
                         "/ the browser's pick.")
    ap.add_argument("--token", default=None,
                    help="access token (default: random, printed at startup)")
    ap.add_argument("--udp-ports", default="50000-50019",
                    help="WebRTC media UDP port range LO-HI (open these in the "
                         "firewall). Default 50000-50019")
    ap.add_argument("--audio", action="store_true",
                    help="also stream system audio (taps the default sink's "
                         "monitor; does not affect local playback)")
    ap.add_argument("--av1", action="store_true",
                    help="use hardware AV1 (nvav1enc) instead of H.264 -- needs "
                         "the rtpav1pay plugin (install-av1.sh) and browser AV1 decode")
    ap.add_argument("--software", action="store_true",
                    help="force x264 software encoding instead of NVENC")
    ap.add_argument("--no-cursor", action="store_true",
                    help="do not embed the cursor in the video")
    ap.add_argument("--unattended", action="store_true",
                    help="SSH-startable mode: inject input via a virtual uinput "
                         "device (no RemoteDesktop portal) AND use a persistent "
                         "ScreenCast capture. Approve the share dialog once; "
                         "restarts restore silently. Needs /dev/uinput access "
                         "(your graphical session has it via logind).")
    ap.add_argument("--tls", action="store_true",
                    help="serve over HTTPS/WSS (self-signed cert auto-generated "
                         "and cached if --tls-cert/--tls-key are not given)")
    ap.add_argument("--tls-cert", help="TLS certificate PEM (use with --tls)")
    ap.add_argument("--tls-key", help="TLS private key PEM (use with --tls)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    Gst.init(None)
    enc = encoder_available()
    if not enc:
        print("ERROR: no H.264 encoder (nvh264enc/x264enc). Run install-deps.sh.")
        return 1
    print(f"encoder available: {enc}"
          + ("  (using x264 software, --software)" if args.software else ""))

    token = args.token or secrets.token_urlsafe(16)

    try:
        lo_s, hi_s = args.udp_ports.split("-", 1)
        udp_lo, udp_hi = int(lo_s), int(hi_s)
    except ValueError:
        print(f"ERROR: --udp-ports must be LO-HI, got {args.udp_ports!r}")
        return 1

    # Set up the uinput injector first (fail fast before bothering with a dialog).
    injector = None
    if args.unattended:
        try:
            from rdserver.uinput_inject import UinputInjector
            injector = UinputInjector()
            print("unattended: input via virtual uinput device "
                  "(no RemoteDesktop input approval needed)")
        except Exception as e:
            print(f"ERROR: --unattended needs /dev/uinput access ({e}). "
                  "Check python3-evdev is installed and /dev/uinput is writable.")
            return 1

    # KDE returns the whole desktop as one stream regardless of selection, so we
    # capture that once and crop per-monitor downstream. One share dialog.
    if args.unattended:
        print("\nNegotiating screen capture (unattended) -- approve the share dialog "
              "ONCE; it's remembered, so future restarts (incl. over SSH) won't "
              "prompt.\n")
    else:
        print("\nNegotiating screen capture -- approve the share dialog "
              "(it captures your whole desktop; individual screens are cropped).\n")
    portal = Portal(cursor=not args.no_cursor, capture_only=args.unattended)
    portal.negotiate()
    print(f"  capturing desktop {portal.width}x{portal.height} (node {portal.node_id})")

    server = Server(portal, token=token, bitrate_kbps=args.bitrate,
                    force_software=args.software,
                    rtp_port_min=udp_lo, rtp_port_max=udp_hi,
                    audio=args.audio, codec="av1" if args.av1 else "h264",
                    congestion_control=args.abr, injector=injector)

    ip = primary_ip()

    ssl_ctx = None
    scheme = "http"
    if args.tls:
        if args.tls_cert and args.tls_key:
            cert_path, key_path = args.tls_cert, args.tls_key
        else:
            cert_path, key_path = _ensure_self_signed(ip)
            print(f"TLS: self-signed cert at {cert_path} "
                  f"(the browser warns once on first connect -- accept it).")
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert_path, key_path)
        scheme = "https"

    print("\n" + "=" * 64)
    print("Remote desktop server ready. Open this in the laptop's browser")
    print("(over Twingate, use this machine's Twingate address instead of the LAN IP):")
    print(f"\n    {scheme}://{ip}:{args.port}/?token={token}\n")
    print(f"token: {token}")
    if not args.tls:
        print("note: signaling is plaintext -- add --tls for HTTPS/WSS so the "
              "token isn't exposed on the wire.")
    print(f"firewall: open TCP {args.port}, and UDP 32768-60999 "
          f"(WebRTC media uses ephemeral ports; pinning temporarily disabled)")
    print("=" * 64 + "\n")

    web.run_app(server.app, host=args.host, port=args.port, print=None,
                ssl_context=ssl_ctx, access_log_class=ScrubAccessLogger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
