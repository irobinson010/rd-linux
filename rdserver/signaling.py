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

from rdserver.media import MediaSession
from rdserver.portal import Portal

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
                 max_viewers: int = 4):
        self.portal = portal          # single capture of the whole desktop
        self.codec = codec
        self.token = token
        self.view_token = view_token   # optional static view-only token (--view-token)
        # All currently-valid view-only tokens: the static one (if any) plus any
        # generated on demand by a control session ("Share view" -> make_view_link).
        self.view_tokens: set[str] = {view_token} if view_token else set()
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
        self._controller: MediaSession | None = None
        self._viewers: list[MediaSession] = []
        self._auth_fail: dict[str, list] = {}   # ip -> [fail_count, window_start]

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
            media = MediaSession(
                self.portal, send_cb=send_cb, bitrate_kbps=self.bitrate_kbps,
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
            self._viewers.append(media)
        # Tell the client its role so it can show/hide the control UI.
        send_cb({"type": "role", "control": role == "control"})

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
                    # Only a controller may mint a view-only token; valid until restart.
                    t = secrets.token_urlsafe(16)
                    self.view_tokens.add(t)
                    log.info("control session generated a view-only token")
                    send_cb({"type": "view_link", "token": t})
        finally:
            log.info("client disconnected: %s (%s)", peer, role)
            media.close()
            if self._controller is media:
                self._controller = None
            elif media in self._viewers:
                self._viewers.remove(media)
        return ws
