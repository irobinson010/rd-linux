"""Remote (Wayland) clipboard access, for clipboard sync with the browser.

Shells out to wl-clipboard (wl-copy / wl-paste) against the live session's
WAYLAND_DISPLAY -- the same session the screen capture comes from. Everything is
async (asyncio subprocesses) so a slow clipboard never blocks the signaling loop.

Text only: images and other MIME types are ignored. If wl-clipboard isn't
installed the whole feature quietly disables itself (clipboard_available() is
False) -- nothing else in the server depends on it.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("clipboard")

# Cap what we shuttle over the WebSocket in either direction. Clipboards can hold
# megabytes (a copied file, a huge selection); a remote-desktop *text* sync
# doesn't need that, and the cap keeps one giant paste from stalling signaling.
MAX_BYTES = 256 * 1024


def clipboard_available() -> bool:
    return bool(shutil.which("wl-copy") and shutil.which("wl-paste"))


async def read_clipboard() -> str | None:
    """Return the remote clipboard as text, or None if empty / non-text / error.

    No --type filter: wl-paste prefers a text flavour on its own, and the binary
    guard below drops anything that isn't valid, null-free UTF-8 (e.g. an image).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "wl-paste", "--no-newline",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except (OSError, asyncio.TimeoutError) as e:
        log.debug("wl-paste failed: %s", e)
        return None
    if proc.returncode != 0 or not out:
        return None                       # empty clipboard / no text on offer
    if len(out) > MAX_BYTES or b"\x00" in out:
        return None                       # too big, or binary (image etc.)
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def write_clipboard(text: str) -> None:
    """Set the remote clipboard to `text` (best-effort, never raises)."""
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        log.debug("clipboard text over %d bytes; not setting", MAX_BYTES)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "wl-copy", "--type", "text/plain;charset=utf-8",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=3)
    except (OSError, asyncio.TimeoutError) as e:
        log.debug("wl-copy failed: %s", e)
