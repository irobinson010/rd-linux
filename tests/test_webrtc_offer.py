"""Regression tests for two production bugs we hit and fixed:

  1. PyGObject use-after-free: `offer = promise.get_reply().get_value("offer")`
     freed the GstStructure, so set-local-description's deep copy segfaulted.
     Guard: generating an offer + set-local-description must NOT crash
     (a crash shows as a non-zero / signal exit of the subprocess).

  2. Black screen: with no framerate cap, webrtcbin negotiated 240fps, so NVENC
     advertised H.264 *level 6.0* (profile-level-id=42c03c), which browsers'
     WebRTC H.264 receiver rejects. Guard: at the capped 60fps the offered level
     must stay <= 5.2; and (sanity) 240fps must exceed it, proving the cap matters.
     Checked for each hardware encoder present (NVENC, VA-API), since each
     computes its own level.

Runs standalone (`python3 tests/test_webrtc_offer.py`) and under pytest. Needs a
working GStreamer + an H.264 encoder (nvh264enc, va*h264enc or x264enc).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "_gen_offer.py")
_UNAVAILABLE = 3          # _gen_offer's exit code for "no such encoder here"


def _gen_offer(fps: int, encoder: str = "auto") -> tuple[int, str]:
    """Run the offer generator in a subprocess; return (exit_code, sdp_text)."""
    proc = subprocess.run(
        [sys.executable, HELPER, "--fps", str(fps), "--encoder", encoder],
        capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout


def _offers(fps: int) -> dict[str, str]:
    """{family: offer SDP} for each hardware encoder that works on this box,
    or the auto pick (software) when none does."""
    out = {}
    for fam in ("nvenc", "vaapi"):
        code, sdp = _gen_offer(fps, fam)
        if code == _UNAVAILABLE:
            continue
        assert code == 0, f"{fam}: offer generation failed (exit {code})"
        out[fam] = sdp
    if not out:
        code, sdp = _gen_offer(fps)
        assert code == 0, f"offer generation failed (exit {code})"
        out["auto"] = sdp
    return out


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
    for fam, sdp in _offers(60).items():
        lvl = _level_idc(sdp)
        assert lvl <= 52, f"{fam}: H.264 level_idc {lvl} (>{52}) -- browsers reject this"


def test_uncapped_high_fps_would_regress():
    """Sanity: 240fps pushes the level above 5.2, proving the cap is what saves us."""
    for fam, sdp in _offers(240).items():
        assert _level_idc(sdp) > 52, f"{fam}: expected 240fps to exceed level 5.2"


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
