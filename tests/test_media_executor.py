"""Pipeline construction must run OFF the event loop (single media-build thread)
so a slow build can't stall other sessions. Patches _start_media -- no real
GStreamer/portal. Self-skips without aiohttp. Standalone or pytest."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from rdserver import signaling
except Exception as e:  # noqa: BLE001
    print(f"SKIP  test_media_executor (cannot import signaling: {e})")
    if __name__ == "__main__":
        raise SystemExit(0)
    import pytest  # type: ignore
    pytest.skip("signaling import failed", allow_module_level=True)


class _FakePortal:
    capture_only = True
    node_id = 1; width = 100; height = 100; session_handle = None; cursor = True


class _StubMedia:
    window = None
    def close(self): pass
    def set_remote_answer(self, _sdp): pass
    def add_ice(self, _i, _c): pass


def test_build_off_loop_and_nonblocking() -> None:
    try:
        import aiohttp  # noqa: F401
        from aiohttp.test_utils import TestClient, TestServer
    except ImportError:
        print("SKIP  test_build_off_loop_and_nonblocking (no aiohttp)")
        return

    srv = signaling.Server(_FakePortal(), token="ctl", bitrate_kbps=1000,
                           force_software=True)
    seen: dict = {}
    started = threading.Event()
    release = threading.Event()

    def fake_start(**_kw):
        seen["thread"] = threading.current_thread().name
        started.set()
        assert release.wait(3), "build was never released"
        return _StubMedia()

    srv._start_media = fake_start

    async def go():
        async with TestClient(TestServer(srv.app)) as c:
            ws = await c.ws_connect("/ws?token=ctl&w=1280&h=720&vmode=vp8")
            # wait until the (blocking) build has begun on its worker thread
            for _ in range(300):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set(), "build never started"
            # ...and while it blocks, a concurrent request must still be served
            t0 = time.monotonic()
            r = await c.get("/api/status?token=ctl")
            dt = time.monotonic() - t0
            assert r.status == 200
            assert dt < 0.5, f"event loop was blocked by the build ({dt:.2f}s)"
            release.set()
            msg = await ws.receive(timeout=3)   # server sends role after build
            assert msg is not None
            await ws.close()
    asyncio.run(go())
    assert seen.get("thread", "").startswith("media-build"), seen


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
