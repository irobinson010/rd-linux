"""Direct input injection via virtual evdev/uinput devices.

This is the unattended-mode input backend: instead of the RemoteDesktop portal
(whose session is the un-persistable bit that forces a per-start "Allow" dialog),
input goes straight through the kernel's uinput device. No portal, no dialog ever.

Needs write access to /dev/uinput. systemd-logind grants the *active* graphical
session user an ACL (`user:<you>:rw-`) on it, so there's no setup when the server
runs as a --user service inside your session.

It mirrors the subset of rdserver.portal.Portal's injection API that MediaSession
calls, so it drops in as `injector` with no changes to the dispatch logic:
    pointer_motion_absolute(x, y, node_id=None)   # x,y in capture-frame px
    pointer_button(evdev_btn, pressed)
    pointer_axis_discrete(axis, steps)            # axis 0=vert, 1=horiz
    keyboard_keycode(evdev_key, pressed)

Two devices are created (keyboard + absolute pointer) so libinput classifies them
cleanly, the same shape a VM "absolute pointing device" uses.
"""

from __future__ import annotations

import logging

from evdev import AbsInfo, UInput
from evdev import ecodes as e

log = logging.getLogger("uinput")

# Absolute coordinate range. KWin/libinput maps an absolute device's [min,max] onto
# the full desktop extent, so we report position as a fraction of this range.
_ABS_MAX = 65535

# Mouse buttons the pointer device advertises (evdev codes from portal.py).
_BTNS = [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA]

# Keyboard keycodes to advertise: the standard keyboard span (KEY_ESC=1 ..
# KEY_MICMUTE=248) covers everything keymap.py can emit, like a real keyboard.
_KEYS = list(range(1, 249))


class UinputInjector:
    """A virtual keyboard + absolute pointer, injecting into the live session."""

    def __init__(self) -> None:
        abs_info = AbsInfo(value=0, min=0, max=_ABS_MAX, fuzz=0, flat=0, resolution=0)
        # Absolute pointer: ABS_X/ABS_Y position + buttons + wheels. No
        # INPUT_PROP_DIRECT and real buttons -> libinput treats it as an absolute
        # mouse (moves the system cursor), not a touchscreen.
        self.ptr = UInput(
            {e.EV_ABS: [(e.ABS_X, abs_info), (e.ABS_Y, abs_info)],
             e.EV_KEY: _BTNS,
             e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL]},
            name="rd-virtual-pointer")
        self.kbd = UInput({e.EV_KEY: _KEYS}, name="rd-virtual-keyboard")
        # Normalisation bounds = the capture-frame size (the whole desktop). Set by
        # MediaSession once the real frame size is known.
        self._w = 1
        self._h = 1
        log.info("uinput devices created (virtual pointer + keyboard)")

    def set_bounds(self, w: int, h: int) -> None:
        """Frame size that incoming absolute coords are expressed in."""
        self._w = max(1, int(w))
        self._h = max(1, int(h))

    # ----- injection API (mirrors Portal) ---------------------------------

    def pointer_motion_absolute(self, x: float, y: float,
                                node_id: int | None = None) -> None:
        # x,y are capture-frame px (the frame spans the whole desktop), so the
        # fraction x/_w maps directly onto the desktop's horizontal extent.
        ax = min(_ABS_MAX, max(0, round(x / self._w * _ABS_MAX)))
        ay = min(_ABS_MAX, max(0, round(y / self._h * _ABS_MAX)))
        self.ptr.write(e.EV_ABS, e.ABS_X, ax)
        self.ptr.write(e.EV_ABS, e.ABS_Y, ay)
        self.ptr.syn()

    def pointer_button(self, button: int, pressed: bool) -> None:
        self.ptr.write(e.EV_KEY, int(button), 1 if pressed else 0)
        self.ptr.syn()

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        code = e.REL_WHEEL if int(axis) == 0 else e.REL_HWHEEL
        self.ptr.write(e.EV_REL, code, int(steps))
        self.ptr.syn()

    def keyboard_keycode(self, keycode: int, pressed: bool) -> None:
        self.kbd.write(e.EV_KEY, int(keycode), 1 if pressed else 0)
        self.kbd.syn()

    def close(self) -> None:
        for d in (getattr(self, "ptr", None), getattr(self, "kbd", None)):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
