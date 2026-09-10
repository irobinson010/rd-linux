"""Minimal sd_notify(3) client: READY / WATCHDOG / STOPPING messages to systemd.

This is what lets the unit run as Type=notify with WatchdogSec=. systemd then
counts the service as started only once we say READY=1 -- after the portal
handshake, which on a first run waits for a human to approve the KDE share
dialog -- and restarts it if the event loop stops pinging. Restart=on-failure
can't see that failure mode: a process wedged in a blocking call is still
"active". Everything here is a no-op when not started by systemd (no
NOTIFY_SOCKET), e.g. the ./rd.sh direct-run fallback.

No dependency on python3-systemd: the protocol is one datagram on a unix socket.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from aiohttp import web

log = logging.getLogger("sdnotify")

_WATCHDOG_TASK = web.AppKey("sd_watchdog_task", asyncio.Task)


def notify(state: str) -> bool:
    """Send one notification (e.g. "READY=1"). False if not under systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):                 # abstract-namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError as e:
        log.debug("sd_notify(%r) failed: %s", state, e)
        return False


def watchdog_interval() -> float | None:
    """Ping interval implied by WATCHDOG_USEC (systemd sets it from WatchdogSec),
    or None when no watchdog is configured for this process."""
    usec = os.environ.get("WATCHDOG_USEC")
    pid = os.environ.get("WATCHDOG_PID")
    if not usec or (pid and pid != str(os.getpid())):
        return None
    try:
        # systemd suggests pinging at about half the timeout; a third leaves
        # slack for the event loop's own occasional multi-second stalls
        # (pipeline start-up, a portal re-negotiation).
        return max(1.0, int(usec) / 1e6 / 3)
    except ValueError:
        return None


def install(app: web.Application) -> None:
    """Hook READY / WATCHDOG / STOPPING into an aiohttp app's lifecycle."""

    async def on_startup(app: web.Application) -> None:
        if not notify("READY=1"):
            return                           # not under systemd: nothing to do
        interval = watchdog_interval()
        if interval is None:
            return
        log.info("systemd watchdog armed: pinging every %.0fs", interval)

        async def ping() -> None:
            while True:
                notify("WATCHDOG=1")
                await asyncio.sleep(interval)

        app[_WATCHDOG_TASK] = asyncio.get_running_loop().create_task(ping())

    async def on_cleanup(app: web.Application) -> None:
        task = app.get(_WATCHDOG_TASK)
        if task is not None:
            task.cancel()
        notify("STOPPING=1")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
