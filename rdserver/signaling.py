"""HTTP + WebSocket signaling server (aiohttp).

Serves the browser client and relays WebRTC SDP/ICE between it and the GStreamer
MediaSession. One active controller at a time: a new connection replaces the old.
Auth is a shared token checked on the WebSocket upgrade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.abc import AbstractAccessLogger
from gi.repository import GLib

from rdserver import clipboard
from rdserver.media import MediaSession
from rdserver.portal import Portal, PortalError

log = logging.getLogger("signaling")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Failed-auth throttle: after this many bad-token attempts from one IP within the
# window, reject further attempts (a cheap brake on token guessing).
_AUTH_MAX_FAILS = 8
_AUTH_WINDOW_S = 30.0


class ScrubAccessLogger(AbstractAccessLogger):
    """Logs method + PATH only -- never the query string, which carries ?token=.
    Keeps the access token out of the logs."""

    def log(self, request, response, time_taken):  # noqa: A002
        self.logger.info("%s %s %s -> %s (%.3fs)",
                         request.remote, request.method, request.path,
                         response.status, time_taken)


class Server:
    def __init__(self, portal: Portal, *, token: str, bitrate_kbps: int,
                 force_software: bool, rtp_port_min: int = 50000,
                 rtp_port_max: int = 50019, audio: bool = False,
                 codec: str = "h264", congestion_control: bool = False,
                 injector=None, view_token: str | None = None,
                 max_viewers: int = 4, view_ttl_s: int = 12 * 3600):
        self.portal = portal          # single capture of the whole desktop
        self.codec = codec
        self.token = token
        self.view_token = view_token   # optional static view-only token (--view-token)
        # All currently-valid view-only tokens -> expiry deadline (time.monotonic)
        # or None for "no expiry". The static --view-token never expires (it's
        # config); tokens minted by a control session ("Share view") live for
        # view_ttl_s and can be revoked in bulk from the control UI.
        self.view_tokens: dict[str, float | None] = (
            {view_token: None} if view_token else {})
        self.view_ttl_s = view_ttl_s
        self.max_viewers = max_viewers
        self.bitrate_kbps = bitrate_kbps
        self.force_software = force_software
        self.rtp_port_min = rtp_port_min
        self.rtp_port_max = rtp_port_max
        self.audio = audio
        self.congestion_control = congestion_control
        self.injector = injector       # uinput injector (unattended) or None=portal
        # One controller (full input) + up to max_viewers view-only sessions. Each
        # connection gets its own MediaSession/pipeline (WebRTC is point-to-point).
        # Viewers keep their WebSocket + whether they used the static token, so
        # revocation can kick minted-link viewers immediately (not just block
        # new connects) while leaving static-token viewers alone.
        self._controller: MediaSession | None = None
        self._viewers: list[dict] = []   # {media, ws, static}
        self._auth_fail: dict[str, list] = {}   # ip -> [fail_count, window_start]
        # Clipboard sync (controller only). One shared remote clipboard; _clip_last
        # is the last text seen in EITHER direction, so our own watcher doesn't
        # echo back a value the browser just pushed (and vice-versa).
        self.clipboard_enabled = clipboard.clipboard_available()
        self._clip_last: str | None = None
        log.info("clipboard sync %s",
                 "enabled" if self.clipboard_enabled
                 else "disabled (install wl-clipboard to enable)")

        self.app = web.Application()
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/app.js", self._appjs),
            web.get("/style.css", self._stylecss),
            web.get("/favicon.ico", self._favicon),
            web.get("/ws", self._ws),
        ])

    # Never cache the client: avoids stale JS/CSS during iteration.
    _NOCACHE = {"Cache-Control": "no-store, must-revalidate"}

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers=self._NOCACHE)

    async def _appjs(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "app.js", headers=self._NOCACHE)

    async def _stylecss(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "style.css", headers=self._NOCACHE)

    async def _favicon(self, _request: web.Request) -> web.StreamResponse:
        return web.Response(status=204)

    def _role_for(self, request: web.Request) -> str | None:
        """Constant-time token check with a per-IP failed-attempt throttle.
        Returns 'control', 'view', or None (invalid / throttled)."""
        ip = request.remote or "?"
        now = time.monotonic()
        rec = self._auth_fail.get(ip)
        if rec and now - rec[1] < _AUTH_WINDOW_S and rec[0] >= _AUTH_MAX_FAILS:
            log.warning("auth throttled for %s", ip)
            return None
        # Drop expired minted view tokens before matching against them.
        for vt, deadline in list(self.view_tokens.items()):
            if deadline is not None and now > deadline:
                del self.view_tokens[vt]
                log.info("view-only token expired")
        token = request.query.get("token", "")
        role = None
        if secrets.compare_digest(token, self.token):
            role = "control"
        elif token and any(secrets.compare_digest(token, vt)
                           for vt in self.view_tokens):
            role = "view"
        if role is not None:
            self._auth_fail.pop(ip, None)
            return role
        if not rec or now - rec[1] >= _AUTH_WINDOW_S:
            self._auth_fail[ip] = [1, now]
        else:
            rec[0] += 1
        return None

    def _start_media(self, **kw) -> MediaSession:
        """Create a MediaSession; if the portal capture session has died, rebuild
        it once and retry.

        The portal session is negotiated once at server startup, but PipeWire or
        xdg-desktop-portal can restart underneath us (crash, audio-stack recovery,
        session hiccup). The session handle then stays invalid forever -- every
        connect fails with GDBus "Invalid session" until the service is restarted.
        In --unattended mode the saved restore token lets us re-negotiate with NO
        dialog, so a dead capture heals transparently on the next connect."""
        try:
            return MediaSession(self.portal, **kw)
        except (GLib.Error, PortalError) as e:
            if not self.portal.capture_only:
                # Interactive session: re-negotiating pops the KDE share dialog,
                # which no remote user can click -- keep the plain failure.
                raise
            log.warning("portal capture session is dead (%s) -- re-negotiating "
                        "from the saved grant and retrying", e)
            self.portal.negotiate()
            return MediaSession(self.portal, **kw)

    async def _clipboard_watch(self, ws: web.WebSocketResponse) -> None:
        """Poll the remote clipboard and push text changes to this controller.

        Primes _clip_last with the current clipboard WITHOUT sending it, so simply
        connecting never clobbers the browser's local clipboard -- only changes
        made on the remote after connecting propagate. Runs until the ws closes."""
        self._clip_last = await clipboard.read_clipboard()
        try:
            while not ws.closed:
                await asyncio.sleep(1.0)
                text = await clipboard.read_clipboard()
                if text is not None and text != self._clip_last:
                    self._clip_last = text
                    await ws.send_str(json.dumps({"type": "clipboard",
                                                  "text": text}))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, RuntimeError):
            pass   # ws went away between the closed-check and the send

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        role = self._role_for(request)
        if role is None:
            return web.Response(status=403, text="invalid or missing token")

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        peer = request.remote
        log.info("client connected: %s (%s)", peer, role)

        if role == "control":
            # One controller at a time: a new control connection replaces the old.
            if self._controller is not None:
                self._controller.close()
                self._controller = None
        else:  # view
            if len(self._viewers) >= self.max_viewers:
                await ws.send_str(json.dumps(
                    {"type": "error",
                     "message": f"viewer limit reached ({self.max_viewers})"}))
                await ws.close()
                return ws

        # Initial encode resolution chosen by the client (it reconnects to change
        # it, which is far more reliable than reconfiguring mid-stream).
        try:
            req_w = int(request.query.get("w", 0))
            req_h = int(request.query.get("h", 0))
        except ValueError:
            req_w = req_h = 0
        max_w = req_w if req_w >= 320 else 2560
        max_h = req_h if req_h >= 240 else 1440
        try:
            monitor_index = int(request.query.get("monitor", 0))
        except ValueError:
            monitor_index = 0
        vmode = request.query.get("vmode", "high")
        if vmode not in ("high", "baseline", "vp8"):
            vmode = "high"
        if self.codec == "av1":          # server forced AV1 via --av1
            vmode = "av1"

        loop = asyncio.get_running_loop()

        def send_cb(msg: dict) -> None:
            # Called from GStreamer threads -> marshal onto the asyncio loop.
            asyncio.run_coroutine_threadsafe(ws.send_str(json.dumps(msg)), loop)

        def on_error(message: str) -> None:
            asyncio.run_coroutine_threadsafe(
                ws.close(code=1011, message=message.encode()[:120]), loop)

        try:
            media = self._start_media(
                send_cb=send_cb, bitrate_kbps=self.bitrate_kbps,
                force_software=self.force_software, on_error=on_error,
                rtp_port_min=self.rtp_port_min, rtp_port_max=self.rtp_port_max,
                audio=self.audio, max_width=max_w, max_height=max_h,
                monitor_index=monitor_index, vmode=vmode,
                congestion_control=self.congestion_control,
                injector=self.injector, allow_input=(role == "control"))
        except Exception as e:
            log.exception("failed to start media session")
            await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
            await ws.close()
            return ws

        if role == "control":
            self._controller = media
        else:
            # Static-token viewers survive "revoke view links" (their token is
            # config, not a minted link) -- remember which kind this one is.
            tok = request.query.get("token", "")
            is_static = bool(self.view_token
                             and secrets.compare_digest(tok, self.view_token))
            self._viewers.append({"media": media, "ws": ws, "static": is_static})
        # Tell the client its role so it can show/hide the control UI. Clipboard
        # sync is a controller-only capability (a viewer must not read/write the
        # host clipboard); advertise it so the client shows the clipboard UI.
        send_cb({"type": "role", "control": role == "control",
                 "clipboard": self.clipboard_enabled and role == "control"})

        # Push remote clipboard changes to the controller while it's connected.
        clip_task = None
        if role == "control" and self.clipboard_enabled:
            clip_task = asyncio.create_task(self._clipboard_watch(ws))

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "answer":
                    media.set_remote_answer(data["sdp"])
                elif kind == "ice":
                    media.add_ice(data.get("sdpMLineIndex", 0), data["candidate"])
                elif kind == "make_view_link" and role == "control":
                    # Only a controller may mint a view-only token. It expires
                    # after view_ttl_s (0 = lives until the server restarts).
                    t = secrets.token_urlsafe(16)
                    deadline = (time.monotonic() + self.view_ttl_s
                                if self.view_ttl_s else None)
                    self.view_tokens[t] = deadline
                    log.info("control session generated a view-only token%s",
                             f" (expires in {self.view_ttl_s // 3600}h)"
                             if self.view_ttl_s else "")
                    send_cb({"type": "view_link", "token": t,
                             "ttl_s": self.view_ttl_s or None})
                elif kind == "revoke_view_links" and role == "control":
                    # Kill every minted view link: forget the tokens AND kick the
                    # viewers using them now. The static --view-token (config) and
                    # its viewers are untouched.
                    minted = [vt for vt, dl in self.view_tokens.items()
                              if vt != self.view_token]
                    for vt in minted:
                        del self.view_tokens[vt]
                    kicked = [v for v in self._viewers if not v["static"]]
                    for v in kicked:
                        await v["ws"].close(
                            code=4001, message=b"view access revoked")
                    log.info("control revoked %d view token(s), disconnected "
                             "%d viewer(s)", len(minted), len(kicked))
                    send_cb({"type": "view_revoked", "tokens": len(minted),
                             "viewers": len(kicked)})
                elif kind == "clipboard" and role == "control":
                    # Browser -> remote: set the host clipboard. Record it as
                    # _clip_last first so the watcher below doesn't immediately
                    # read it back and echo it to the browser.
                    text = data.get("text", "")
                    if isinstance(text, str) and self.clipboard_enabled:
                        self._clip_last = text
                        await clipboard.write_clipboard(text)
        finally:
            if clip_task:
                clip_task.cancel()
            log.info("client disconnected: %s (%s)", peer, role)
            media.close()
            if self._controller is media:
                self._controller = None
            else:
                self._viewers = [v for v in self._viewers
                                 if v["media"] is not media]
        return ws
