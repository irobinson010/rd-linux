"""Translate browser input events to the evdev codes the portal expects.

The RemoteDesktop portal's NotifyKeyboardKeycode wants Linux evdev keycodes
(linux/input-event-codes.h), which map 1:1 to the browser's physical
KeyboardEvent.code values. Injecting physical keycodes (not characters) means the
*host's* keyboard layout decides the resulting character -- the right behaviour
when remote-controlling your own machine.
"""

from rdserver.portal import (BTN_EXTRA, BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_SIDE)

# KeyboardEvent.code -> evdev keycode
KEY_CODE_TO_EVDEV: dict[str, int] = {
    "Escape": 1,
    "Digit1": 2, "Digit2": 3, "Digit3": 4, "Digit4": 5, "Digit5": 6,
    "Digit6": 7, "Digit7": 8, "Digit8": 9, "Digit9": 10, "Digit0": 11,
    "Minus": 12, "Equal": 13, "Backspace": 14, "Tab": 15,
    "KeyQ": 16, "KeyW": 17, "KeyE": 18, "KeyR": 19, "KeyT": 20, "KeyY": 21,
    "KeyU": 22, "KeyI": 23, "KeyO": 24, "KeyP": 25,
    "BracketLeft": 26, "BracketRight": 27, "Enter": 28, "ControlLeft": 29,
    "KeyA": 30, "KeyS": 31, "KeyD": 32, "KeyF": 33, "KeyG": 34, "KeyH": 35,
    "KeyJ": 36, "KeyK": 37, "KeyL": 38, "Semicolon": 39, "Quote": 40,
    "Backquote": 41, "ShiftLeft": 42, "Backslash": 43,
    "KeyZ": 44, "KeyX": 45, "KeyC": 46, "KeyV": 47, "KeyB": 48, "KeyN": 49,
    "KeyM": 50, "Comma": 51, "Period": 52, "Slash": 53, "ShiftRight": 54,
    "NumpadMultiply": 55, "AltLeft": 56, "Space": 57, "CapsLock": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65,
    "F8": 66, "F9": 67, "F10": 68, "NumLock": 69, "ScrollLock": 70,
    "Numpad7": 71, "Numpad8": 72, "Numpad9": 73, "NumpadSubtract": 74,
    "Numpad4": 75, "Numpad5": 76, "Numpad6": 77, "NumpadAdd": 78,
    "Numpad1": 79, "Numpad2": 80, "Numpad3": 81, "Numpad0": 82,
    "NumpadDecimal": 83,
    "IntlBackslash": 86, "F11": 87, "F12": 88,
    "NumpadEnter": 96, "ControlRight": 97, "NumpadDivide": 98,
    "PrintScreen": 99, "AltRight": 100,
    "Home": 102, "ArrowUp": 103, "PageUp": 104, "ArrowLeft": 105,
    "ArrowRight": 106, "End": 107, "ArrowDown": 108, "PageDown": 109,
    "Insert": 110, "Delete": 111,
    "AudioVolumeMute": 113, "AudioVolumeDown": 114, "AudioVolumeUp": 115,
    "Pause": 119, "MetaLeft": 125, "MetaRight": 126, "ContextMenu": 127,
}

# MouseEvent.button -> evdev button code
MOUSE_BUTTON_TO_EVDEV: dict[int, int] = {
    0: BTN_LEFT,
    1: BTN_MIDDLE,
    2: BTN_RIGHT,
    3: BTN_SIDE,     # "back"
    4: BTN_EXTRA,    # "forward"
}


def keycode_for(code: str) -> int | None:
    return KEY_CODE_TO_EVDEV.get(code)


def button_for(button: int) -> int | None:
    return MOUSE_BUTTON_TO_EVDEV.get(button)
