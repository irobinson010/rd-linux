"""Capture rate vs. monitor refresh (media.capture_rate + monitor_layout's hz).

Regression: with --capture-fps 120 and the fastest monitor at 119.88 Hz, KWin
offered the desktop stream up to 119 fps only, pipewiresrc failed to negotiate
("no more input formats") and every connect was a black screen. The request
must never exceed the fastest monitor's refresh rate truncated to whole Hz.
Patches kscreen-doctor -- no real display. Standalone or pytest."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdserver import media  # noqa: E402

# Trimmed `kscreen-doctor -j` from the dev box after moving the displays to
# the AMD iGPU: 3440x1440@99.98 + 3840x2160@119.88 (scale 1.5), one disabled.
_KSCREEN = {"outputs": [
    {"name": "HDMI-A-3", "enabled": True, "pos": {"x": 0, "y": 50}, "scale": 1,
     "size": {"width": 3440, "height": 1440}, "currentModeId": "2",
     "modes": [{"id": "1", "refreshRate": 59.97},
               {"id": "2", "refreshRate": 99.98200225830078}]},
    {"name": "DP-4", "enabled": True, "pos": {"x": 3440, "y": 0}, "scale": 1.5,
     "size": {"width": 3840, "height": 2160}, "currentModeId": "7",
     "modes": [{"id": "7", "refreshRate": 119.87999725341797}]},
    {"name": "DP-1", "enabled": False, "pos": {"x": 0, "y": 0}, "scale": 1,
     "size": {"width": 1920, "height": 1080}, "currentModeId": "9",
     "modes": [{"id": "9", "refreshRate": 240.0}]},
]}


def _layout() -> list[dict]:
    done = subprocess.CompletedProcess([], 0, stdout=json.dumps(_KSCREEN))
    with mock.patch.object(media.subprocess, "run", return_value=done):
        return media.monitor_layout()


def test_layout_reports_current_refresh() -> None:
    hz = {m["name"]: m["hz"] for m in _layout()}
    assert set(hz) == {"HDMI-A-3", "DP-4"}, hz     # disabled DP-1 left out
    assert round(hz["HDMI-A-3"], 2) == 99.98, hz    # current mode, not the first
    assert round(hz["DP-4"], 2) == 119.88, hz


def test_clamped_to_fastest_monitor() -> None:
    assert media.capture_rate(120, _layout()) == 119    # the black-screen case


def test_lower_request_untouched() -> None:
    assert media.capture_rate(60, _layout()) == 60
    assert media.capture_rate(30, _layout()) == 30


def test_unknown_refresh_keeps_request() -> None:
    assert media.capture_rate(120, [{"name": "screen", "w": 1, "h": 1}]) == 120
    assert media.capture_rate(120, []) == 120


def test_still_bounded() -> None:
    assert media.capture_rate(500, [{"hz": 240.0}]) == media._MAX_CAPTURE_FPS
    assert media.capture_rate(0, [{"hz": 60.0}]) == 1


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1; print(f"FAIL  {name}: {e}")
    print(f"\n{'all passed' if not fails else str(fails) + ' failed'}")
    raise SystemExit(1 if fails else 0)
