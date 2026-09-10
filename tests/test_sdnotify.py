"""sd_notify client (rdserver/sdnotify.py): READY/WATCHDOG/STOPPING over a unix
socket, and the WATCHDOG_USEC interval math. No real systemd -- we bind our own
NOTIFY_SOCKET. Standalone or pytest."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdserver import sdnotify  # noqa: E402


def _clear_env() -> None:
    for k in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        os.environ.pop(k, None)


def test_notify_noop_without_systemd() -> None:
    _clear_env()
    assert sdnotify.notify("READY=1") is False


def test_notify_sends_datagram() -> None:
    _clear_env()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "n.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(path); s.settimeout(1)
        os.environ["NOTIFY_SOCKET"] = path
        try:
            assert sdnotify.notify("READY=1") is True
            assert s.recv(64) == b"READY=1"
        finally:
            s.close(); _clear_env()


def test_watchdog_interval() -> None:
    _clear_env()
    assert sdnotify.watchdog_interval() is None
    os.environ["WATCHDOG_USEC"] = "3000000"
    os.environ["WATCHDOG_PID"] = str(os.getpid())
    assert sdnotify.watchdog_interval() == 1.0        # third of the timeout
    os.environ["WATCHDOG_PID"] = "1"                  # meant for another pid
    assert sdnotify.watchdog_interval() is None
    _clear_env()


def test_install_lifecycle() -> None:
    try:
        from aiohttp import web
        from aiohttp.test_utils import TestServer
    except ImportError:
        print("SKIP  test_install_lifecycle (no aiohttp)")
        return
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "n.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(path); s.setblocking(False)
        os.environ["NOTIFY_SOCKET"] = path
        os.environ["WATCHDOG_USEC"] = "3000000"
        os.environ["WATCHDOG_PID"] = str(os.getpid())
        app = web.Application()
        sdnotify.install(app)

        async def run():
            async with TestServer(app):
                await asyncio.sleep(1.6)              # >= 1 watchdog interval
            got = []
            while True:
                try:
                    got.append(s.recv(64))
                except BlockingIOError:
                    break
            return got
        got = asyncio.run(run())
        s.close(); _clear_env()
        assert got and got[0] == b"READY=1", got
        assert got.count(b"WATCHDOG=1") >= 1, got
        assert got[-1] == b"STOPPING=1", got


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1; print(f"FAIL  {name}: {e}")
    print(f"\n{'all passed' if not fails else str(fails) + ' failed'}")
    raise SystemExit(1 if fails else 0)
