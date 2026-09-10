"""VRR guard (rdserver/vrr.py): parse kscreen-doctor output, and persist/restore
the saved policy across a crash. Pure -- kscreen-doctor is mocked, nothing on
the machine is touched. Runs standalone or under pytest."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdserver import vrr  # noqa: E402

_KSCREEN = json.dumps({"outputs": [
    {"name": "DP-1", "enabled": True, "vrrPolicy": 2},      # automatic -> VRR on
    {"name": "DP-2", "enabled": True, "vrrPolicy": 0},      # never -> leave alone
    {"name": "HDMI-A-2", "enabled": True},                  # no key -> incapable
    {"name": "DP-3", "enabled": False, "vrrPolicy": 2},     # disabled -> skip
]})


def test_outputs_with_vrr_parsing() -> None:
    assert vrr.outputs_with_vrr(_KSCREEN) == {"DP-1": "automatic"}
    assert vrr.outputs_with_vrr("not json") == {}
    assert vrr.outputs_with_vrr(json.dumps({"outputs": []})) == {}


def _mock_run(calls: list):
    async def run(*argv, timeout=10.0):
        calls.append(argv)
        return _KSCREEN if argv[1] == "-j" else "ok"
    return run


def test_suspend_persists_and_restores() -> None:
    calls: list = []
    vrr._run = _mock_run(calls)
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "vrr.json"
        g = vrr.VrrGuard(state_file=state)
        asyncio.run(g.suspend())
        # only the VRR-on output is touched, and it's recorded BEFORE the change
        assert ("kscreen-doctor", "output.DP-1.vrrpolicy.never") in calls
        assert not any("DP-2" in a[1] for a in calls if len(a) > 1)
        assert json.loads(state.read_text()) == {"DP-1": "automatic"}
        asyncio.run(g.restore())
        assert ("kscreen-doctor", "output.DP-1.vrrpolicy.automatic") in calls
        assert not state.exists()


def test_restore_leftover_after_crash() -> None:
    calls: list = []
    vrr._run = _mock_run(calls)
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "vrr.json"
        state.write_text(json.dumps({"DP-1": "automatic", "DP-9": "always"}))
        g = vrr.VrrGuard(state_file=state)          # fresh process after a crash
        asyncio.run(g.restore_leftover())
        assert ("kscreen-doctor", "output.DP-1.vrrpolicy.automatic") in calls
        assert ("kscreen-doctor", "output.DP-9.vrrpolicy.always") in calls
        assert not state.exists()


def test_suspend_idempotent_and_no_vrr() -> None:
    calls: list = []
    async def run(*argv, timeout=10.0):
        calls.append(argv)
        return json.dumps({"outputs": [{"name": "X", "enabled": True}]})  # none on
    vrr._run = run
    with tempfile.TemporaryDirectory() as d:
        g = vrr.VrrGuard(state_file=Path(d) / "v.json")
        asyncio.run(g.suspend())
        assert not any(len(a) > 1 and "vrrpolicy" in a[1] for a in calls)


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
