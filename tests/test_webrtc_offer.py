"""Regression tests for two production bugs we hit and fixed:

  1. PyGObject use-after-free: `offer = promise.get_reply().get_value("offer")`
     freed the GstStructure, so set-local-description's deep copy segfaulted.
     Guard: generating an offer + set-local-description must NOT crash
     (a crash shows as a non-zero / signal exit of the subprocess).

  2. Black screen: with no framerate cap, webrtcbin negotiated 240fps, so NVENC
     advertised H.264 *level 6.0* (profile-level-id=42c03c), which browsers'
     WebRTC H.264 receiver rejects. Guard: at the capped 60fps the offered level
     must stay <= 5.2; and (sanity) 240fps must exceed it, proving the cap matters.

Runs standalone (`python3 tests/test_webrtc_offer.py`) and under pytest. Needs a
working GStreamer + an H.264 encoder (nvh264enc or x264enc).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "_gen_offer.py")


def _gen_offer(fps: int) -> tuple[int, str]:
    """Run the offer generator in a subprocess; return (exit_code, sdp_text)."""
    proc = subprocess.run(
        [sys.executable, HELPER, "--fps", str(fps)],
        capture_output=True, text=True, timeout=40)
    return proc.returncode, proc.stdout


def _level_idc(sdp: str) -> int:
    m = re.search(r"profile-level-id=[0-9a-fA-F]{4}([0-9a-fA-F]{2})", sdp)
    assert m, f"no H.264 profile-level-id in offer:\n{sdp[:500]}"
    return int(m.group(1), 16)


def test_offer_does_not_segfault():
    """set-local-description on the promise offer must not crash (UAF regression)."""
    code, sdp = _gen_offer(60)
    # A SIGSEGV surfaces as a negative return code (-11) or 139.
    assert code == 0, f"offer generation crashed/failed (exit {code})"
    assert "m=video" in sdp, "offer has no video media section"


def test_h264_level_capped_at_60fps():
    """The 60fps cap must keep the advertised H.264 level <= 5.2 (browser-safe)."""
    code, sdp = _gen_offer(60)
    assert code == 0, f"offer generation failed (exit {code})"
    lvl = _level_idc(sdp)
    assert lvl <= 52, f"H.264 level_idc {lvl} (>{52}) -- browsers reject this"


def test_uncapped_high_fps_would_regress():
    """Sanity: 240fps pushes the level above 5.2, proving the cap is what saves us."""
    code, sdp = _gen_offer(240)
    assert code == 0, f"offer generation failed (exit {code})"
    assert _level_idc(sdp) > 52, "expected 240fps to exceed level 5.2"


def main() -> int:
    tests = [test_offer_does_not_segfault,
             test_h264_level_capped_at_60fps,
             test_uncapped_high_fps_would_regress]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
