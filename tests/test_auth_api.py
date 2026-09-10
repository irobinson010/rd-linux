"""Signaling auth + local control API (rdserver/signaling.py). No GStreamer:
these paths never build a MediaSession, so a fake portal is enough. Standalone
or pytest; self-skips if the GI typelibs signaling imports aren't present."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from rdserver import signaling
except Exception as e:  # noqa: BLE001  (missing gobject-introspection typelibs)
    print(f"SKIP  test_auth_api (cannot import signaling: {e})")
    if __name__ == "__main__":
        raise SystemExit(0)
    import pytest  # type: ignore
    pytest.skip("signaling import failed", allow_module_level=True)


class _FakePortal:
    capture_only = True
    node_id = 1
    width = 100
    height = 100
    session_handle = None
    cursor = True


def _server():
    return signaling.Server(_FakePortal(), token="ctl-tok", bitrate_kbps=1000,
                            force_software=True, view_token="view-tok",
                            base_url="https://host:8098")


class _Req:
    def __init__(self, token, remote="1.2.3.4"):
        self.query = {"token": token}
        self.remote = remote


def test_token_eq_bytesafe() -> None:
    assert signaling._token_eq("abc", "abc")
    assert not signaling._token_eq("abc", "abd")
    # non-ASCII must not raise (secrets.compare_digest on str would TypeError)
    assert not signaling._token_eq("é", "abc")


def test_role_and_tarpit() -> None:
    srv = _server()
    assert srv._role_for(_Req("ctl-tok")) == ("control", 0.0)
    assert srv._role_for(_Req("view-tok")) == ("view", 0.0)
    # failures return a growing delay, never a hard lockout
    assert srv._role_for(_Req("bad")) == (None, 0.5)
    assert srv._role_for(_Req("bad")) == (None, 1.0)
    assert srv._role_for(_Req("é")) == (None, 1.5)   # non-ASCII -> no 500
    for _ in range(20):
        r, d = srv._role_for(_Req("bad"))
    assert (r, d) == (None, 3.0)                          # capped
    # a correct token is never blocked, even mid-tarpit, and clears the record
    assert srv._role_for(_Req("ctl-tok")) == ("control", 0.0)
    assert "1.2.3.4" not in srv._auth_fail


def _client(srv):
    from aiohttp.test_utils import TestClient, TestServer
    return TestClient(TestServer(srv.app))


def test_http_headers_and_api() -> None:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        print("SKIP  test_http_headers_and_api (no aiohttp test utils)")
        return
    srv = _server()

    async def go():
        async with _client(srv) as c:
            r = await c.get("/")
            assert r.status == 200
            csp = r.headers.get("Content-Security-Policy", "")
            assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
            assert r.headers.get("X-Content-Type-Options") == "nosniff"
            assert r.headers.get("X-Frame-Options") == "DENY"
            # HEAD is what the client's reconnect logic probes with
            assert (await c.head("/")).status == 200

            # API requires the CONTROL token (view token is rejected)
            assert (await c.get("/api/status")).status == 403
            assert (await c.get("/api/status?token=view-tok")).status == 403
            assert (await c.get("/api/status?token=%C3%A9")).status == 403  # no 500
            r = await c.get("/api/status", headers={"X-RD-Token": "ctl-tok"})
            assert r.status == 200
            j = await r.json()
            assert j["ok"] and j["base_url"] == "https://host:8098"
            assert j["controller"] is False and j["view_links"] == 0

            r = await c.post("/api/view-link?token=ctl-tok")
            j = await r.json()
            assert j["url"].startswith("https://host:8098/?token=")
            assert j["ttl_s"] == 12 * 3600
            j = await (await c.get("/api/status?token=ctl-tok")).json()
            assert j["view_links"] == 1

            r = await c.post("/api/revoke-views", headers={"X-RD-Token": "ctl-tok"})
            assert (await r.json())["tokens"] == 1
            j = await (await c.get("/api/status?token=ctl-tok")).json()
            assert j["view_links"] == 0
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
