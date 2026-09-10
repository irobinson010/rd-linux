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


async def _run(argv: list[str], stdin: bytes | None = None, *,
               capture: bool = True,
               timeout: float = 3.0) -> tuple[int, bytes] | None:
    """Run a wl-clipboard command; (returncode, stdout) or None on failure.

    A timed-out child is KILLED, not abandoned: asyncio.wait_for only cancels
    our wait, and the poller runs every second, so a compositor that stops
    answering (lock screen, a wedged session) would otherwise pile up one
    orphaned wl-paste per second until the service restarts.

    capture=False leaves stdout alone -- needed for wl-copy, which forks a
    background child to keep serving the clipboard; a captured stdout pipe
    would stay open in that child and never reach EOF."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
    except OSError as e:
        log.debug("%s failed to start: %s", argv[0], e)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        log.debug("%s hung for %.0fs; killing it", argv[0], timeout)
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return None
    return proc.returncode, out


async def read_clipboard() -> str | None:
    """Return the remote clipboard as text, or None if empty / non-text / error.

    No --type filter: wl-paste prefers a text flavour on its own, and the binary
    guard below drops anything that isn't valid, null-free UTF-8 (e.g. an image).
    """
    res = await _run(["wl-paste", "--no-newline"])
    if res is None:
        return None
    rc, out = res
    if rc != 0 or not out:
        return None                       # empty clipboard / no text on offer
    if len(out) > MAX_BYTES or b"\x00" in out:
        return None                       # too big, or binary (image etc.)
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def write_clipboard(text: str) -> None:
    """Set the remote clipboard to `text` (best-effort, never raises)."""
    data = text.encode("utf-8", "ignore")
    if len(data) > MAX_BYTES:
        log.debug("clipboard text over %d bytes; not setting", MAX_BYTES)
        return
    await _run(["wl-copy", "--type", "text/plain;charset=utf-8"], stdin=data,
               capture=False)
