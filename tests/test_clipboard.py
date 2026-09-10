"""Clipboard helper (rdserver/clipboard.py): a hung child is KILLED on timeout
(not leaked once per poll), and the capture=False path doesn't stall on a child
that backgrounds with stdout open. Uses coreutils (sleep/cat). Standalone/pytest."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdserver import clipboard  # noqa: E402


def test_timeout_kills_child() -> None:
    async def go():
        # a unique fractional duration is the marker pgrep looks for
        marker = f"31.{os.getpid() % 100000}"
        t0 = time.monotonic()
        res = await clipboard._run(["sleep", marker], timeout=0.5)
        assert res is None, f"expected None on timeout, got {res!r}"
        assert time.monotonic() - t0 < 2.0, "wait_for did not return promptly"
        await asyncio.sleep(0.2)
        left = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f",
                               f"sleep {marker}"],
                              capture_output=True, text=True).stdout.strip()
        assert not left, f"timed-out child still alive: {left}"
    asyncio.run(go())


def test_normal_run_returns_output() -> None:
    async def go():
        rc, out = await clipboard._run(["cat"], stdin=b"hello world")
        assert rc == 0 and out == b"hello world"
    asyncio.run(go())


def test_capture_false_does_not_stall() -> None:
    async def go():
        t0 = time.monotonic()
        # child backgrounds a process that keeps a copy of stdout open; with
        # capture=True this would wait for that grandchild's EOF (wl-copy shape)
        res = await clipboard._run(
            ["sh", "-c", "sleep 5 & cat >/dev/null"], stdin=b"x",
            capture=False, timeout=3.0)
        assert res is not None and res[0] == 0
        assert time.monotonic() - t0 < 2.5
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "^sleep 5$"])
    asyncio.run(go())


def test_missing_binary_is_none() -> None:
    async def go():
        assert await clipboard._run(["rd-nonexistent-binary-xyz"]) is None
    asyncio.run(go())


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
