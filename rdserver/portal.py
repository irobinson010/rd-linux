"""xdg-desktop-portal RemoteDesktop + ScreenCast integration.

This is the piece that makes "control the *current* Wayland session" possible:
KWin (via xdg-desktop-portal-kde) hands us a PipeWire node that mirrors the live
session, plus a session handle we can inject pointer/keyboard events into. No new
X server, no separate login session -- the thing RDP/xrdp cannot do on Wayland.

All D-Bus work goes through GLib's GDBus (Gio). The negotiation handshake uses the
portal Request/Response pattern and is driven synchronously with a nested main loop
at startup. Input injection afterwards is fire-and-forget (GDBus has its own I/O
worker thread, so no running main loop is required for it).
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE_REMOTE = "org.freedesktop.portal.RemoteDesktop"
IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
IFACE_REQUEST = "org.freedesktop.portal.Request"
IFACE_SESSION = "org.freedesktop.portal.Session"

log = logging.getLogger("portal")

# ScreenCast source types (bitmask)
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4

# ScreenCast cursor modes
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2   # cursor drawn into the video -- what we want
CURSOR_METADATA = 4

# RemoteDesktop device types (bitmask)
DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
DEVICE_TOUCHSCREEN = 4

# evdev button codes (linux/input-event-codes.h)
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114

KEY_RELEASED = 0
KEY_PRESSED = 1


class PortalError(RuntimeError):
    pass


class Portal:
    """Owns one combined RemoteDesktop+ScreenCast session for the whole server."""

    def __init__(self, *, cursor: bool = True, capture_only: bool = False):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        name = self.conn.get_unique_name()  # e.g. ":1.42"
        self._sender = name[1:].replace(".", "_")  # -> "1_42"
        self._token_seq = 0
        self._cursor = cursor
        self.cursor = cursor          # public: other capture paths honour it too
        # capture_only: ScreenCast-only + persistence (input handled by uinput).
        # Lets the capture grant persist so restarts don't re-prompt (unattended).
        # Public: signaling checks it to decide whether a dead session can be
        # re-negotiated silently (restore token) or would pop a dialog.
        self.capture_only = capture_only

        self.session_handle: str | None = None
        self.streams: list[dict] = []          # [{node_id, width, height}, ...]
        self.node_id: int | None = None        # default/active stream node id
        self.width: int = 0
        self.height: int = 0

    # ----- low-level Request/Response helper -------------------------------

    def _next_token(self) -> str:
        self._token_seq += 1
        return f"rd{self._token_seq}"

    def _request_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/request/{self._sender}/{token}"

    def _request(self, iface: str, method: str, body: GLib.Variant,
                 options: dict, timeout_s: float | None = None) -> dict:
        """Call a portal method that returns a Request, wait for its Response.

        `body` is everything *before* the trailing options dict; `options` is the
        a{sv} we augment with a handle_token. Returns the results dict (a{sv}).

        `timeout_s` bounds BOTH the method call and the wait for its Response
        signal. None means wait indefinitely for the Response (right for the
        interactive first start, where a human has to click the share dialog);
        the method call itself then still gets GDBus's default 25 s. Callers on
        the server's event loop must pass a timeout: a portal that accepted the
        request and then died would otherwise park the whole process here.
        """
        token = self._next_token()
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        req_path = self._request_path(token)

        loop = GLib.MainLoop()
        holder: dict = {}

        def on_response(_c, _s, _p, _i, _sig, params, *_u):
            code, results = params.unpack()
            holder["code"] = code
            holder["results"] = results
            loop.quit()

        def on_timeout() -> bool:
            holder["timed_out"] = True
            loop.quit()
            return False               # one-shot: the source removes itself

        sub = self.conn.signal_subscribe(
            PORTAL_BUS, IFACE_REQUEST, "Response", req_path, None,
            Gio.DBusSignalFlags.NONE, on_response, None)

        # Splice the options a{sv} onto the end of the call body tuple.
        full = _append_options(body, options)
        call_timeout_ms = int(timeout_s * 1000) if timeout_s else -1

        timer = None
        try:
            self.conn.call_sync(
                PORTAL_BUS, PORTAL_PATH, iface, method, full,
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
                call_timeout_ms, None)
            if timeout_s:
                timer = GLib.timeout_add(call_timeout_ms, on_timeout)
            loop.run()
        except GLib.Error as e:
            raise PortalError(f"{iface}.{method} call failed: {e}") from e
        finally:
            self.conn.signal_unsubscribe(sub)
            if timer is not None and not holder.get("timed_out"):
                GLib.source_remove(timer)

        if holder.get("timed_out"):
            raise PortalError(
                f"{iface}.{method}: no Response from the portal within "
                f"{timeout_s:g}s (is xdg-desktop-portal running?)")
        if holder.get("code") != 0:
            raise PortalError(
                f"{iface}.{method} denied/cancelled (response={holder.get('code')}). "
                "If a permission dialog appeared, it must be approved.")
        return holder.get("results", {})

    # ----- the negotiation handshake ---------------------------------------

    def negotiate(self, timeout_s: float | None = None) -> None:
        """Create + start the portal session. Re-callable: if the existing session
        dies (PipeWire or xdg-desktop-portal restarted), calling this again builds
        a fresh one -- silently in capture-only mode thanks to the restore token.

        `timeout_s` caps each portal request (see _request). Pass one when
        re-negotiating from the server's event loop; leave None at startup."""
        self._close_session()          # best-effort: drop a dead/half-built one
        try:
            if self.capture_only:
                self._negotiate_capture_only(timeout_s)
            else:
                self._negotiate_with_input(timeout_s)
        except PortalError:
            self._close_session()      # don't leave a half-built session behind
            raise

    def _close_session(self) -> None:
        """Ask the portal to close the current session (fire-and-forget)."""
        if not self.session_handle:
            return
        try:
            self.conn.call(
                PORTAL_BUS, self.session_handle, IFACE_SESSION, "Close",
                None, None, Gio.DBusCallFlags.NONE, 1000, None, None)
        except GLib.Error:
            pass
        self.session_handle = None

    def _negotiate_with_input(self, timeout_s: float | None = None) -> None:
        # Combined RemoteDesktop (input) + ScreenCast (capture) on one session.
        # NB: KDE forbids persist_mode/restore_token on a RemoteDesktop session
        # ("Remote desktop sessions cannot persist"), so this path's share dialog
        # appears on every server start by design. (capture-only mode persists.)

        # 1) Create the RemoteDesktop session (ScreenCast rides on the same handle).
        sess_token = self._next_token()
        res = self._request(
            IFACE_REMOTE, "CreateSession",
            GLib.Variant("()", ()),
            {"session_handle_token": GLib.Variant("s", sess_token)}, timeout_s)
        self.session_handle = res["session_handle"]

        # 2) Pick screen sources (ScreenCast interface, same session).
        src_opts = {
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant(
                "u", CURSOR_EMBEDDED if self._cursor else CURSOR_HIDDEN),
        }
        self._request(IFACE_SCREENCAST, "SelectSources",
                      _obj(self.session_handle), src_opts, timeout_s)

        # 3) Pick input devices (RemoteDesktop interface).
        self._request(IFACE_REMOTE, "SelectDevices",
                      _obj(self.session_handle),
                      {"types": GLib.Variant("u", DEVICE_KEYBOARD | DEVICE_POINTER)},
                      timeout_s)

        # 4) Start -- this is what pops the KDE "share your screen" dialog.
        start_res = self._request(
            IFACE_REMOTE, "Start",
            GLib.Variant("(os)", (self.session_handle, "")),
            {}, timeout_s)
        self._parse_streams(start_res.get("streams") or [])

    def _negotiate_capture_only(self, timeout_s: float | None = None) -> None:
        # ScreenCast-only (no RemoteDesktop -- input is via the uinput injector).
        # KDE *does* allow persistence for plain ScreenCast, so we request
        # persist_mode=PERSISTENT and reuse a saved restore_token: the dialog
        # appears once, then restarts restore SILENTLY -> true unattended/SSH start.
        restore_token = self._read_restore_token()
        sess_token = self._next_token()
        res = self._request(
            IFACE_SCREENCAST, "CreateSession",
            GLib.Variant("()", ()),
            {"session_handle_token": GLib.Variant("s", sess_token)}, timeout_s)
        self.session_handle = res["session_handle"]

        src_opts = {
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant(
                "u", CURSOR_EMBEDDED if self._cursor else CURSOR_HIDDEN),
            "persist_mode": GLib.Variant("u", 2),     # 2 = persist until revoked
        }
        if restore_token:
            src_opts["restore_token"] = GLib.Variant("s", restore_token)
            log.info("screencast: reusing saved restore token (expect no dialog)")
        self._request(IFACE_SCREENCAST, "SelectSources",
                      _obj(self.session_handle), src_opts, timeout_s)

        start_res = self._request(
            IFACE_SCREENCAST, "Start",
            GLib.Variant("(os)", (self.session_handle, "")),
            {}, timeout_s)
        new_token = start_res.get("restore_token")
        if new_token:
            self._write_restore_token(new_token)
        self._parse_streams(start_res.get("streams") or [])

    def _parse_streams(self, streams: list) -> None:
        if not streams:
            raise PortalError("portal returned no streams")
        # One entry per shared monitor: {node_id, width, height, x, y}.
        # x/y are the monitor's position in the compositor's global layout
        # (used for edge-crossing); may be absent on some compositors.
        self.streams = []
        for node_id, props in streams:
            size = props.get("size") or (0, 0)
            pos = props.get("position") or (0, 0)
            self.streams.append({
                "node_id": int(node_id),
                "width": int(size[0]),
                "height": int(size[1]),
                "x": int(pos[0]),
                "y": int(pos[1]),
            })
        # Default "active" stream is the first one (used as the injection default).
        self.node_id = self.streams[0]["node_id"]
        self.width = self.streams[0]["width"]
        self.height = self.streams[0]["height"]
        log.info("portal shared %d monitor(s): %s", len(self.streams),
                 ", ".join(f'{s["width"]}x{s["height"]}' for s in self.streams))

    # ----- restore-token persistence (capture-only mode) -------------------

    def _restore_token_path(self) -> Path:
        return Path.home() / ".cache" / "rdserver" / "screencast.token"

    def _read_restore_token(self) -> str | None:
        try:
            return self._restore_token_path().read_text().strip() or None
        except OSError:
            return None

    def _write_restore_token(self, token: str) -> None:
        try:
            p = self._restore_token_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(token)
            p.chmod(0o600)
            log.info("screencast: saved restore token for unattended restarts")
        except OSError as e:
            log.warning("could not save restore token: %s", e)

    def open_pipewire_fd(self) -> int:
        """Open a fresh PipeWire remote fd (call once per GStreamer pipeline)."""
        if not self.session_handle:
            # A PortalError (not an assert) so the signaling self-heal path
            # treats "no session" like "dead session" and re-negotiates.
            raise PortalError("no portal session (previous negotiation failed)")
        ret, fdlist = self.conn.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, IFACE_SCREENCAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_handle, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        idx = ret.unpack()[0]
        return fdlist.get(idx)

    # ----- input injection (fire-and-forget) -------------------------------

    def _notify(self, method: str, sig: str, *args) -> None:
        params = GLib.Variant(
            f"(oa{{sv}}{sig})", (self.session_handle, {}, *args))
        self.conn.call(
            PORTAL_BUS, PORTAL_PATH, IFACE_REMOTE, method, params,
            None, Gio.DBusCallFlags.NONE, -1, None, None)

    def pointer_motion_absolute(self, x: float, y: float,
                                node_id: int | None = None) -> None:
        self._notify("NotifyPointerMotionAbsolute", "udd",
                     self.node_id if node_id is None else int(node_id),
                     float(x), float(y))

    def pointer_motion_relative(self, dx: float, dy: float) -> None:
        self._notify("NotifyPointerMotion", "dd", float(dx), float(dy))

    def pointer_button(self, button: int, pressed: bool) -> None:
        self._notify("NotifyPointerButton", "iu",
                     int(button), KEY_PRESSED if pressed else KEY_RELEASED)

    def pointer_axis(self, dx: float, dy: float) -> None:
        self._notify("NotifyPointerAxis", "dd", float(dx), float(dy))

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        # axis: 0 = vertical, 1 = horizontal
        self._notify("NotifyPointerAxisDiscrete", "ui", int(axis), int(steps))

    def keyboard_keycode(self, keycode: int, pressed: bool) -> None:
        self._notify("NotifyKeyboardKeycode", "iu",
                     int(keycode), KEY_PRESSED if pressed else KEY_RELEASED)

    def keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        self._notify("NotifyKeyboardKeysym", "iu",
                     int(keysym), KEY_PRESSED if pressed else KEY_RELEASED)


def _obj(path: str) -> GLib.Variant:
    """A one-element tuple variant carrying just an object path (for *_obj calls)."""
    return GLib.Variant("(o)", (path,))


def _append_options(body: GLib.Variant, options: dict) -> GLib.Variant:
    """Return a new tuple variant = body's children + (options as a{sv})."""
    children = [body.get_child_value(i) for i in range(body.n_children())]
    children.append(GLib.Variant("a{sv}", options))
    return GLib.Variant.new_tuple(*children)
