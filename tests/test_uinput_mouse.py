#!/usr/bin/env python3
"""MANUAL test: does the uinput virtual pointer land on the right place across the
multi-monitor desktop? Run it and WATCH the cursor. Nothing here touches the
running server -- it just creates a virtual input device and moves the cursor to
known points so you can confirm the mapping before we wire it in.

    python3 tests/test_uinput_mouse.py
    python3 tests/test_uinput_mouse.py --dwell 2          # pause longer at each point
    python3 tests/test_uinput_mouse.py --type "rd test"   # also type (focus a field first)

Needs write access to /dev/uinput (your graphical session already has it via the
systemd-logind ACL). Move your real mouse at any time -- this only adds events.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdserver import keymap                       # noqa: E402
from rdserver.media import monitor_layout         # noqa: E402
from rdserver.uinput_inject import UinputInjector  # noqa: E402

# Minimal char -> KeyboardEvent.code for the optional --type smoke (lowercase only).
_CHAR_CODE = {" ": "Space"}
for c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_CODE[c] = "Key" + c.upper()
for d in "0123456789":
    _CHAR_CODE[d] = "Digit" + d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dwell", type=float, default=1.5,
                    help="seconds to pause at each point (default 1.5)")
    ap.add_argument("--type", dest="text", default="",
                    help="type this lowercase string after a 3s countdown")
    args = ap.parse_args()

    mons = monitor_layout()
    if not mons:
        print("no monitors from kscreen-doctor; assuming a single 1920x1080")
        mons = [{"name": "screen", "x": 0, "y": 0, "w": 1920, "h": 1080}]
    W = max(m["x"] + m["w"] for m in mons)
    H = max(m["y"] + m["h"] for m in mons)
    print(f"desktop logical size: {W}x{H}, {len(mons)} monitor(s):")
    for i, m in enumerate(mons):
        print(f"  [{i}] {m['name']} {m['w']}x{m['h']} at ({m['x']},{m['y']})")

    inj = UinputInjector()
    inj.set_bounds(W, H)
    print("\ncreated virtual pointer; giving the compositor ~1.5s to detect it...")
    time.sleep(1.5)

    def goto(label: str, x: float, y: float) -> None:
        print(f"  -> {label}: ({int(x)},{int(y)})")
        inj.pointer_motion_absolute(x, y)
        time.sleep(args.dwell)

    print("\nMoving the cursor -- WATCH your screens:")
    goto("desktop center", W / 2, H / 2)
    for i, m in enumerate(mons):
        goto(f"center of monitor [{i}] {m['name']}",
             m["x"] + m["w"] / 2, m["y"] + m["h"] / 2)
    goto("top-left corner", 2, 2)
    goto("top-right corner", W - 2, 2)
    goto("bottom-right corner", W - 2, H - 2)
    goto("bottom-left corner", 2, H - 2)
    goto("back to center", W / 2, H / 2)

    if args.text:
        print(f"\nTyping in 3s -- focus a text field NOW: {args.text!r}")
        time.sleep(3)
        for ch in args.text.lower():
            code = keymap.keycode_for(_CHAR_CODE.get(ch, ""))
            if code is None:
                continue
            inj.keyboard_keycode(code, True)
            inj.keyboard_keycode(code, False)
            time.sleep(0.04)
        print("done typing")

    inj.close()
    print("\nDone. Did the cursor land on the right screen/positions?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
