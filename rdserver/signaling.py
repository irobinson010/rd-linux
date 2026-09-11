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
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.abc import AbstractAccessLogger
from gi.repository import GLib

from rdserver import clipboard
from rdserver.media import MediaSession
from rdserver.portal import Portal, PortalError

log = logging.getLogger("signaling")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Failed-auth throttle: each bad-token attempt from one IP within the window
# answers a little slower (a tarpit), up to a cap. It never refuses to check a
# token -- see Server._role_for for why a hard lockout is the wrong tool here.
_AUTH_WINDOW_S = 30.0
_AUTH_DELAY_STEP_S = 0.5
_AUTH_DELAY_MAX_S = 3.0
_AUTH_MAX_TRACKED = 256        # bound on the per-IP table

# Largest encode size a client may ask for (8K). The request is otherwise
# unbounded, and videoscale would allocate gigabytes per frame for an absurd
# size before the encoder ever got the chance to refuse it.
_MAX_ENC_W, _MAX_ENC_H = 7680, 4320

# How long a silent portal re-negotiation (self-heal) may wait on each D-Bus
# request. It runs on the event loop, so a portal that never answers would
# otherwise freeze every client -- and still look "active" to systemd.
_PORTAL_HEAL_TIMEOUT_S = 10.0

# WebSocket close codes the client understands (4000-4999 = application use).
WS_VIEW_REVOKED = 4001         # controller revoked minted view links
WS_REPLACED = 4002             # another control session took over
WS_SOURCE_CHANGED = 4003       # capture source flipped (game window <-> desktop)

# Debounce for fullscreen-window changes: a game starting up toggles state and
# geometry a few times in quick succession; one reconnect is plenty.
_GAME_DEBOUNCE_S = 0.4

# How long to wait with zero sessions before giving adaptive sync back. A
# capture-source flip reconnects everyone within a second; toggling VRR twice
# for that would blank the monitor twice.
_VRR_RESTORE_GRACE_S = 3.0


def _token_eq(a: str, b: str) -> bool:
    """Constant-time equality that tolerates any input. secrets.compare_digest
    raises TypeError on non-ASCII *str* arguments, which would turn a stray
    character in ?token= into an HTTP 500 (and skip the failure counter);
    comparing the UTF-8 bytes is defined for everything."""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class ScrubAccessLogger(AbstractAccessLogger):
    """Logs method + PATH only -- never the query string, which carries ?token=.
    Keeps the access token out of the logs."""

    def log(self, request, response, time_taken):  # noqa: A002
        self.logger.info("%s %s %s -> %s (%.3fs)",
                         request.remote, request.method, request.path,
                         response.status, time_taken)


@web.middleware
async def security_headers(request: web.Request, handler):
    """Browser-hardening headers on every non-WebSocket response.

    The CSP pins scripts and styles to this origin (nothing inline), the
    signaling WebSocket to this host, and forbids framing -- clickjacking a
    remote-desktop page would hand an attacker real clicks on the host. No HSTS:
    with the default self-signed cert it would make the one-time certificate
    warning impossible to click through."""
    resp = await handler(request)
    if resp.prepared:            # WebSocket upgrade: headers already on the wire
        return resp
    host = request.host          # what the client connected to (LAN IP, Twingate)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' wss://{host} ws://{host}; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'; object-src 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


class Server:
    def __init__(self, portal: Portal, *, token: str, bitrate_kbps: int,
                 force_software: bool, audio: bool = False,
                 codec: str = "h264", congestion_control: bool = False,
                 injector=None, view_token: str | None = None,
                 max_viewers: int = 4, view_ttl_s: int = 12 * 3600,
                 capture_fps: int = 60, base_url: str = ""):
        self.portal = portal          # single capture of the whole desktop
        self.codec = codec
        self.capture_fps = capture_fps
        # Public base of the connect URL ("https://host:port"), for building
        # shareable links in the local API (the browser client uses its own
        # location.origin instead). Empty -> the API omits full URLs.
        self.base_url = base_url.rstrip("/")
        # Game-window capture (see gamewatch.py): while an X11 window is
        # fullscreen and active, every session captures THAT window instead of
        # the desktop, so KWin's screencast goes idle and the game stops
        # flickering. _game holds the window with its rect in logical px.
        self._game: dict | None = None
        self._game_pending: dict | None = None
        self._game_timer: asyncio.TimerHandle | None = None
        self._watcher = None
        # Adaptive sync off while anyone is connected (see vrr.py); None = keep.
        self._vrr = None
        self._vrr_timer: asyncio.TimerHandle | None = None
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
        self.audio = audio
        self.congestion_control = congestion_control
        self.injector = injector       # uinput injector (unattended) or None=portal
        # One controller (full input) + up to max_viewers view-only sessions. Each
        # connection gets its own MediaSession/pipeline (WebRTC is point-to-point).
        # Every session keeps its WebSocket so it can be closed from elsewhere:
        # a replaced controller is told it was replaced, and revocation kicks
        # minted-link viewers immediately (not just blocks new connects) while
        # leaving static-token viewers alone.
        self._controller: dict | None = None   # {media, ws}
        self._viewers: list[dict] = []          # {media, ws, static}
        self._tasks: set[asyncio.Task] = set()  # fire-and-forget closes
        self._auth_fail: dict[str, list] = {}   # ip -> [fail_count, window_start]
        # Pipelines are built here so their several seconds of blocking work
        # (subprocesses, the PLAYING wait, portal self-heal) never stall the
        # event loop. Single-threaded: builds serialize, so concurrent connects
        # never run two portal GLib main loops at once.
        self._media_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="media-build")
        # Clipboard sync (controller only). One shared remote clipboard; _clip_last
        # is the last text seen in EITHER direction, so our own watcher doesn't
        # echo back a value the browser just pushed (and vice-versa).
        self.clipboard_enabled = clipboard.clipboard_available()
        self._clip_last: str | None = None
        log.info("clipboard sync %s",
                 "enabled" if self.clipboard_enabled
                 else "disabled (install wl-clipboard to enable)")

        self.app = web.Application(middlewares=[security_headers])
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/app.js", self._appjs),
            web.get("/style.css", self._stylecss),
            web.get("/favicon.ico", self._favicon),
            web.get("/ws", self._ws),
            # Local control API (tray indicator etc.). Guarded by the CONTROL
            # token, which already grants full control, so this adds no power;
            # it just lets a non-browser tool see status and mint view links.
            web.get("/api/status", self._api_status),
            web.post("/api/view-link", self._api_view_link),
            web.post("/api/revoke-views", self._api_revoke_views),
        ])
        self.app.on_cleanup.append(self._shutdown_executor)

    async def _shutdown_executor(self, _app) -> None:
        self._media_executor.shutdown(wait=False, cancel_futures=True)

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

    def _role_for(self, request: web.Request) -> tuple[str | None, float]:
        """Constant-time token check. Returns (role, delay_s): role is
        'control', 'view', or None for an invalid token; delay_s is how long
        the caller should stall before answering a failure.

        Failures are slowed down, never locked out. The throttle is keyed by
        source IP, but behind the Twingate connector (which runs on this host)
        every remote client shares ONE address -- a hard lockout would let a
        single mistyped token block everybody for the window. A growing delay
        on failed attempts keeps guessing slow, while a correct token always
        gets straight in."""
        ip = request.remote or "?"
        now = time.monotonic()
        # Drop expired minted view tokens before matching against them.
        for vt, deadline in list(self.view_tokens.items()):
            if deadline is not None and now > deadline:
                del self.view_tokens[vt]
                log.info("view-only token expired")
        token = request.query.get("token", "")
        role = None
        if _token_eq(token, self.token):
            role = "control"
        elif token and any(_token_eq(token, vt) for vt in self.view_tokens):
            role = "view"
        if role is not None:
            self._auth_fail.pop(ip, None)
            return role, 0.0
        rec = self._auth_fail.get(ip)
        if not rec or now - rec[1] >= _AUTH_WINDOW_S:
            if len(self._auth_fail) >= _AUTH_MAX_TRACKED:
                # Bound the table: forget every entry outside the window.
                self._auth_fail = {k: v for k, v in self._auth_fail.items()
                                   if now - v[1] < _AUTH_WINDOW_S}
                if len(self._auth_fail) >= _AUTH_MAX_TRACKED:
                    self._auth_fail.clear()
            rec = self._auth_fail[ip] = [0, now]
        rec[0] += 1
        log.warning("auth failed for %s (%d in window)", ip, rec[0])
        return None, min(_AUTH_DELAY_MAX_S, _AUTH_DELAY_STEP_S * rec[0])

    def _kick(self, ws: web.WebSocketResponse, code: int, reason: bytes) -> None:
        """Close ANOTHER connection's WebSocket without waiting on it. close()
        waits (up to its timeout) for the peer's close frame, and a phone that
        has gone to sleep would stall the caller's own session for that long."""
        async def go() -> None:
            try:
                await ws.close(code=code, message=reason)
            except Exception:          # already gone
                pass
        task = asyncio.get_running_loop().create_task(go())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- adaptive sync (VRR) guard ------------------------------------------

    def enable_vrr_guard(self) -> None:
        """VRR off on capable monitors while any session is connected."""
        from rdserver.vrr import VrrGuard
        self._vrr = VrrGuard()

        async def on_startup(_app) -> None:
            await self._vrr.restore_leftover()   # a crashed run may have left it off

        async def on_cleanup(_app) -> None:
            if self._vrr_timer:
                self._vrr_timer.cancel()
            await self._vrr.restore()
        self.app.on_startup.append(on_startup)
        self.app.on_cleanup.append(on_cleanup)

    def _session_count(self) -> int:
        return (1 if self._controller else 0) + len(self._viewers)

    def _vrr_wanted_off(self) -> bool:
        """VRR goes off only while a fullscreen game is being streamed. On a
        plain desktop 'automatic' VRR isn't engaged, so there is nothing to
        gain -- and changing the policy makes KWin reconfigure the output,
        which kills the portal screencast a desktop session depends on (seen
        as 'error set output format' + a reconnect). With a game fullscreen
        every session is on the Xwayland grab, which that can't touch."""
        return self._session_count() > 0 and self._game is not None

    def _vrr_update(self) -> None:
        """Reconcile the VRR policy with the current sessions + game state."""
        if self._vrr is None:
            return
        if self._vrr_wanted_off():
            if self._vrr_timer:             # a restore was pending: keep it off
                self._vrr_timer.cancel()
                self._vrr_timer = None
            self._spawn(self._vrr.suspend())
        elif self._vrr_timer is None:
            # Grace period: a capture-source flip reconnects everyone within a
            # second; toggling VRR for that would blank the monitor twice.
            self._vrr_timer = asyncio.get_running_loop().call_later(
                _VRR_RESTORE_GRACE_S, self._vrr_restore_if_idle)

    def _vrr_restore_if_idle(self) -> None:
        self._vrr_timer = None
        if not self._vrr_wanted_off():
            self._spawn(self._vrr.restore())

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- game-window capture ---------------------------------------------

    def enable_game_capture(self) -> None:
        """Watch for a fullscreen X11 window once the event loop runs."""
        async def on_startup(_app) -> None:
            from rdserver.gamewatch import FullscreenWatcher
            loop = asyncio.get_running_loop()
            watcher = FullscreenWatcher(
                lambda info: loop.call_soon_threadsafe(self._on_fullscreen, info))
            if watcher.start():
                self._watcher = watcher
        self.app.on_startup.append(on_startup)

    def _on_fullscreen(self, info: dict | None) -> None:
        """Watcher callback (on the loop): convert X px -> logical desktop px,
        debounce, then flip sessions whose capture source no longer matches."""
        game = None
        if info and self._watcher and self.portal.width and self._watcher.root_w:
            sx = self._watcher.root_w / self.portal.width       # Xwayland scale
            sy = (self._watcher.root_h / self.portal.height
                  if self.portal.height and self._watcher.root_h else sx)
            game = {"xid": info["xid"], "title": info["title"],
                    "x": int(round(info["x"] / sx)), "y": int(round(info["y"] / sy)),
                    "w": max(1, int(round(info["w"] / sx))),
                    "h": max(1, int(round(info["h"] / sy)))}
        self._game_pending = game
        if self._game_timer:
            self._game_timer.cancel()
        self._game_timer = asyncio.get_running_loop().call_later(
            _GAME_DEBOUNCE_S, self._apply_game)

    def _apply_game(self) -> None:
        self._game_timer = None
        game = self._game_pending
        if game == self._game:
            return
        self._game = game
        if game:
            log.info("fullscreen window active: %r (xid 0x%x) at %d,%d %dx%d -> "
                     "capturing the window", game["title"], game["xid"],
                     game["x"], game["y"], game["w"], game["h"])
        else:
            log.info("no fullscreen window -> capturing the desktop")
        sessions = ([self._controller] if self._controller else []) + list(self._viewers)
        flipped = 0
        for s in sessions:
            have = s["media"].window
            if bool(have) != bool(game) or (have and have["xid"] != game["xid"]):
                self._kick(s["ws"], WS_SOURCE_CHANGED, b"capture source changed")
                flipped += 1
        if flipped:
            log.info("switching %d session(s) to the new capture source", flipped)
        self._vrr_update()

    def _start_media(self, **kw) -> MediaSession:
        """Create a MediaSession; if the portal capture session has died, rebuild
        it once and retry.

        The portal session is negotiated once at server startup, but PipeWire or
        xdg-desktop-portal can restart underneath us (crash, audio-stack recovery,
        session hiccup). The session handle then stays invalid forever -- every
        connect fails with GDBus "Invalid session" until the service is restarted.
        In --unattended mode the saved restore token lets us re-negotiate with NO
        dialog, so a dead capture heals transparently on the next connect."""
        if self._game:
            try:
                return MediaSession(self.portal, window=self._game, **kw)
            except Exception as e:                   # noqa: BLE001
                # The window may have vanished a moment ago; the watcher will
                # report that shortly. Serve the desktop meanwhile.
                log.warning("window capture of %r failed (%s); using the desktop",
                            self._game["title"], e)
        try:
            return MediaSession(self.portal, **kw)
        except (GLib.Error, PortalError) as e:
            if not self.portal.capture_only:
                # Interactive session: re-negotiating pops the KDE share dialog,
                # which no remote user can click -- keep the plain failure.
                raise
            log.warning("portal capture session is dead (%s) -- re-negotiating "
                        "from the saved grant and retrying", e)
            self.portal.negotiate(timeout_s=_PORTAL_HEAL_TIMEOUT_S)
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
        role, delay = self._role_for(request)
        if role is None:
            if delay:
                await asyncio.sleep(delay)   # tarpit the guesser, not the server
            return web.Response(status=403, text="invalid or missing token")

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        peer = request.remote
        log.info("client connected: %s (%s)", peer, role)

        if role == "control":
            # One controller at a time: a new control connection replaces the
            # old -- which is TOLD (close code 4002) so its UI can say so instead
            # of sitting on a frozen frame, and so it doesn't auto-reconnect and
            # fight the new device for control.
            old = self._controller
            if old is not None:
                self._controller = None
                old["media"].close()
                self._kick(old["ws"], WS_REPLACED,
                           b"replaced by a new control session")
        else:  # view
            if len(self._viewers) >= self.max_viewers:
                await ws.send_str(json.dumps(
                    {"type": "error",
                     "message": f"viewer limit reached ({self.max_viewers})"}))
                await ws.close()
                return ws

        # Initial encode resolution chosen by the client (it reconnects to change
        # it, which is far more reliable than reconfiguring mid-stream), clamped
        # to something a real display could be.
        try:
            req_w = int(request.query.get("w", 0))
            req_h = int(request.query.get("h", 0))
        except ValueError:
            req_w = req_h = 0
        max_w = min(req_w, _MAX_ENC_W) if req_w >= 320 else 2560
        max_h = min(req_h, _MAX_ENC_H) if req_h >= 240 else 1440
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
            # Build the pipeline OFF the event loop. MediaSession construction
            # blocks for up to several seconds (kscreen-doctor + pactl
            # subprocesses, the PLAYING state wait, a D-Bus fd open, and any
            # portal self-heal); doing it inline froze every other session's
            # heartbeats and messages meanwhile. The executor is single-threaded
            # on purpose, so two concurrent connects build sequentially and no
            # two portal handshakes ever run a GLib main loop at once.
            media = await loop.run_in_executor(self._media_executor, partial(
                self._start_media,
                send_cb=send_cb, bitrate_kbps=self.bitrate_kbps,
                force_software=self.force_software, on_error=on_error,
                audio=self.audio, max_width=max_w, max_height=max_h,
                monitor_index=monitor_index, vmode=vmode,
                congestion_control=self.congestion_control,
                injector=self.injector, allow_input=(role == "control"),
                capture_fps=self.capture_fps))
        except Exception as e:
            log.exception("failed to start media session")
            await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
            await ws.close()
            return ws

        if role == "control":
            self._controller = {"media": media, "ws": ws}
        else:
            # Static-token viewers survive "revoke view links" (their token is
            # config, not a minted link) -- remember which kind this one is.
            tok = request.query.get("token", "")
            is_static = bool(self.view_token and _token_eq(tok, self.view_token))
            self._viewers.append({"media": media, "ws": ws, "static": is_static})
        self._vrr_update()
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
                if not isinstance(data, dict):
                    continue
                kind = data.get("type")
                try:
                    await self._handle(media, role, kind, data, send_cb)
                except (KeyError, TypeError, ValueError) as e:
                    # A malformed message must not take down the sender's own
                    # session (it would propagate out of the loop and close it).
                    log.warning("bad %r message from %s: %s", kind, peer, e)
        finally:
            if clip_task:
                clip_task.cancel()
            log.info("client disconnected: %s (%s)", peer, role)
            media.close()
            if self._controller is not None and self._controller["media"] is media:
                self._controller = None
            else:
                self._viewers = [v for v in self._viewers
                                 if v["media"] is not media]
            self._vrr_update()
        return ws

    # ----- view-link mint / revoke (shared by WS and the local API) --------

    def _mint_view_token(self, *, via: str) -> str:
        """Create a view-only token that expires after view_ttl_s (0 = never)."""
        t = secrets.token_urlsafe(16)
        self.view_tokens[t] = (time.monotonic() + self.view_ttl_s
                               if self.view_ttl_s else None)
        log.info("view-only token minted (%s)%s", via,
                 f", expires in {self.view_ttl_s // 3600}h"
                 if self.view_ttl_s else "")
        return t

    def _revoke_view_links(self, *, via: str) -> tuple[int, int]:
        """Forget every minted view token and disconnect its viewers. The
        static --view-token (config) and its viewers are left alone.
        Returns (tokens revoked, viewers kicked)."""
        minted = [vt for vt in self.view_tokens if vt != self.view_token]
        for vt in minted:
            del self.view_tokens[vt]
        kicked = [v for v in self._viewers if not v["static"]]
        for v in kicked:
            self._kick(v["ws"], WS_VIEW_REVOKED, b"view access revoked")
        log.info("revoked %d view token(s), disconnected %d viewer(s) (%s)",
                 len(minted), len(kicked), via)
        return len(minted), len(kicked)

    # ----- local control API (token-guarded JSON) --------------------------

    def _api_ok(self, request: web.Request) -> bool:
        """The control token, from ?token= or an X-RD-Token header."""
        tok = request.query.get("token") or request.headers.get("X-RD-Token", "")
        return _token_eq(tok, self.token)

    async def _api_status(self, request: web.Request) -> web.Response:
        if not self._api_ok(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        game = self._game
        return web.json_response({
            "ok": True,
            "controller": self._controller is not None,
            "viewers": len(self._viewers),
            "max_viewers": self.max_viewers,
            "source": "window" if game else "desktop",
            "game": game["title"] if game else None,
            "view_links": sum(1 for t in self.view_tokens if t != self.view_token),
            "base_url": self.base_url,
        })

    async def _api_view_link(self, request: web.Request) -> web.Response:
        if not self._api_ok(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        t = self._mint_view_token(via="local API")
        return web.json_response({
            "token": t, "ttl_s": self.view_ttl_s or None,
            "url": f"{self.base_url}/?token={t}" if self.base_url else None})

    async def _api_revoke_views(self, request: web.Request) -> web.Response:
        if not self._api_ok(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        tokens, viewers = self._revoke_view_links(via="local API")
        return web.json_response({"tokens": tokens, "viewers": viewers})

    async def _handle(self, media: MediaSession, role: str, kind: str | None,
                      data: dict, send_cb) -> None:
        """One signaling message from an authenticated client."""
        if kind == "answer":
            media.set_remote_answer(str(data["sdp"]))
        elif kind == "ice":
            media.add_ice(int(data.get("sdpMLineIndex", 0)), str(data["candidate"]))
        elif kind == "make_view_link" and role == "control":
            t = self._mint_view_token(via="control session")
            send_cb({"type": "view_link", "token": t,
                     "ttl_s": self.view_ttl_s or None})
        elif kind == "revoke_view_links" and role == "control":
            tokens, viewers = self._revoke_view_links(via="control session")
            send_cb({"type": "view_revoked", "tokens": tokens,
                     "viewers": viewers})
        elif kind == "clipboard" and role == "control":
            # Browser -> remote: set the host clipboard. Record it as
            # _clip_last first so the watcher doesn't immediately read it
            # back and echo it to the browser.
            text = data.get("text", "")
            if isinstance(text, str) and self.clipboard_enabled:
                self._clip_last = text
                await clipboard.write_clipboard(text)
