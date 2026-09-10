"""Fullscreen-window watcher (Xwayland side), for game-window capture.

Why this exists: while KWin is screencasting a monitor, a fullscreen game on it
flickers between direct scanout and compositing (the KDE bug 517961 family, on
NVIDIA). An ordinary process can't stream a *window* from KWin (its screencast
protocol is privileged) and the portal's window capture needs its picker dialog
every time. But Steam/Proton games are X11 windows under Xwayland, and an X
window can be read straight out of Xwayland with GStreamer's ximagesrc -- no
KWin screencast involved, no flicker (measured here: 60 fps, 3% CPU for us,
Xwayland +20% of a core).

This watches the active X window and reports when it is a mapped fullscreen
window -- and when that stops being true -- with its root-relative geometry in
X pixels. Xwayland applies one global scale (KWin's [Xwayland] Scale), so
logical desktop coords = X coords / (X root width / desktop width); the server
does that conversion. No X display -> nothing is reported, feature off.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

log = logging.getLogger("gamewatch")

Info = dict  # {"xid", "title", "wm_class", "x", "y", "w", "h"}  (X px, root coords)


class FullscreenWatcher:
    def __init__(self, on_change: Callable[[Info | None], None]) -> None:
        self._on_change = on_change   # called from the watcher THREAD
        self._d = None
        self._root = None
        self._atoms: dict[str, int] = {}
        self._tracked = None          # X window object we subscribed to
        self._last_key = None
        self.root_w = 0
        self.root_h = 0

    # ----- setup ------------------------------------------------------------

    def start(self) -> bool:
        """Connect to the X display and start watching. False if unavailable."""
        if not os.environ.get("DISPLAY"):
            log.info("game-window capture off: no DISPLAY (no Xwayland)")
            return False
        try:
            from Xlib import X, display  # python3-xlib
            self._X = X
            self._d = display.Display()
        except Exception as e:                       # noqa: BLE001
            log.info("game-window capture off: cannot open X display (%s)", e)
            return False
        # Errors on requests without a reply (e.g. selecting events on a
        # window that just closed) arrive asynchronously; python-xlib's default
        # handler prints them to stderr. Log quietly instead.
        self._d.set_error_handler(
            lambda err, _req=None: log.debug("X async error (window gone?): %s", err))
        scr = self._d.screen()
        self._root = scr.root
        self.root_w, self.root_h = scr.width_in_pixels, scr.height_in_pixels
        for name in ("_NET_ACTIVE_WINDOW", "_NET_WM_STATE",
                     "_NET_WM_STATE_FULLSCREEN", "_NET_WM_NAME", "UTF8_STRING"):
            self._atoms[name] = self._d.intern_atom(name)
        self._root.change_attributes(event_mask=X.PropertyChangeMask)
        self._d.flush()
        threading.Thread(target=self._run, name="gamewatch", daemon=True).start()
        log.info("game-window capture armed (X root %dx%d)", self.root_w, self.root_h)
        return True

    # ----- evaluation ---------------------------------------------------------

    def current(self) -> Info | None:
        """The active window if it is a mapped fullscreen X window, else None."""
        try:
            return self._evaluate()
        except Exception as e:                       # noqa: BLE001  (BadWindow etc.)
            log.debug("evaluate failed: %s", e)
            return None

    def _evaluate(self) -> Info | None:
        from Xlib import Xatom
        X = self._X
        prop = self._root.get_full_property(self._atoms["_NET_ACTIVE_WINDOW"], Xatom.WINDOW)
        xid = int(prop.value[0]) if prop and prop.value else 0
        if not xid:
            self._track(None)
            return None                    # a Wayland-native window is active
        win = self._d.create_resource_object("window", xid)
        self._track(win)
        state = win.get_full_property(self._atoms["_NET_WM_STATE"], Xatom.ATOM)
        if not state or self._atoms["_NET_WM_STATE_FULLSCREEN"] not in state.value:
            return None
        if win.get_attributes().map_state != X.IsViewable:
            return None
        geo = win.get_geometry()
        at = self._root.translate_coords(win, 0, 0)
        name = win.get_full_property(self._atoms["_NET_WM_NAME"], self._atoms["UTF8_STRING"])
        title = name.value.decode("utf-8", "replace") if name and name.value else ""
        if not title:
            wm_name = win.get_wm_name()
            title = wm_name if isinstance(wm_name, str) else (wm_name or b"").decode("latin-1", "replace") if wm_name else ""
        cls = win.get_wm_class() or ("", "")
        return {"xid": xid, "title": title or cls[1] or f"0x{xid:x}",
                "wm_class": cls[1], "x": int(at.x), "y": int(at.y),
                "w": int(geo.width), "h": int(geo.height)}

    def _track(self, win) -> None:
        """Get PropertyNotify/StructureNotify from the active window too, so a
        fullscreen toggle, resize or close of the game is seen immediately."""
        if win is self._tracked or (win is not None and self._tracked is not None
                                    and win.id == self._tracked.id):
            return
        X = self._X
        try:
            if self._tracked is not None:
                self._tracked.change_attributes(event_mask=X.NoEventMask)
        except Exception:                            # noqa: BLE001  (it's gone)
            pass
        self._tracked = win
        if win is not None:
            try:
                win.change_attributes(
                    event_mask=X.PropertyChangeMask | X.StructureNotifyMask)
            except Exception:                        # noqa: BLE001
                pass
        self._d.flush()

    # ----- event loop -----------------------------------------------------------

    def _run(self) -> None:
        X = self._X
        interesting_props = {self._atoms["_NET_ACTIVE_WINDOW"], self._atoms["_NET_WM_STATE"]}
        self._emit(self.current())
        try:
            while True:
                ev = self._d.next_event()
                if ev.type == X.PropertyNotify:
                    if ev.atom not in interesting_props:
                        continue
                elif ev.type not in (X.ConfigureNotify, X.UnmapNotify,
                                     X.MapNotify, X.DestroyNotify):
                    continue
                self._emit(self.current())
        except Exception as e:                       # noqa: BLE001
            log.warning("game-window watcher stopped: %s", e)

    def _emit(self, info: Info | None) -> None:
        key = (info["xid"], info["x"], info["y"], info["w"], info["h"]) if info else None
        if key == self._last_key:
            return
        self._last_key = key
        self._on_change(info)
