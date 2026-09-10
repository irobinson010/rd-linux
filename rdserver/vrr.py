"""Turn adaptive sync (VRR) off while anyone is connected, back on after.

Why: any capture load on the host -- window grab, encode, audio -- adds small
frame-time hiccups to a fullscreen game, and a VRR panel shows those as black
frames (the panel briefly drops out of its refresh range). Measured 2026-09-08:
each piece of the load alone was fine, all of them together produced black
frames, and switching the monitor's VRR policy to Never made them vanish with
the same load. So while a session is connected the VRR-capable outputs are set
to "never"; when the last session ends (or the server stops) their previous
policy is restored. Uses KDE's kscreen-doctor, which applies live, no re-login.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("vrr")

# KScreen's VrrPolicy enum as kscreen-doctor -j reports it.
_POLICY_WORD = {0: "never", 1: "always", 2: "automatic"}


async def _run(*argv: str, timeout: float = 10.0) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("%s failed: %s", argv[0], e)
        return None
    return out.decode("utf-8", "replace") if proc.returncode == 0 else None


def outputs_with_vrr(kscreen_json: str) -> dict[str, str]:
    """{output name: current policy word} for enabled outputs that have VRR on."""
    found: dict[str, str] = {}
    try:
        data = json.loads(kscreen_json)
    except ValueError:
        return found
    for o in data.get("outputs", []):
        if not o.get("enabled") or "vrrPolicy" not in o:
            continue                      # off, or incapable (no vrrPolicy key)
        word = _POLICY_WORD.get(o["vrrPolicy"])
        if word and word != "never":
            found[o["name"]] = word
    return found


class VrrGuard:
    """The saved policies also live in a state file, so a crash (or a SIGKILL
    from the watchdog) between suspend and restore can't leave a monitor
    without adaptive sync: the next start puts them back before doing
    anything else."""

    def __init__(self, state_file: Path | None = None) -> None:
        self._saved: dict[str, str] = {}   # output -> policy to put back
        self._active = False
        self._state = state_file or (Path.home() / ".cache" / "rdserver" / "vrr-saved.json")

    async def restore_leftover(self) -> None:
        """Undo a previous run that died with VRR switched off."""
        try:
            saved = json.loads(self._state.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict) or not saved:
            self._forget()
            return
        log.warning("VRR: a previous run left adaptive sync off on %s; restoring",
                    ", ".join(saved))
        self._saved, self._active = dict(saved), True
        await self.restore()

    async def suspend(self) -> None:
        """Set every VRR-enabled output to 'never' (idempotent)."""
        if self._active:
            return
        self._active = True
        js = await _run("kscreen-doctor", "-j")
        if js is None:
            log.info("VRR: kscreen-doctor unavailable; leaving displays alone")
            return
        targets = outputs_with_vrr(js)
        if not targets:
            log.info("VRR: no output has adaptive sync on; nothing to do")
            return
        self._saved = dict(targets)
        self._remember()                   # BEFORE changing anything
        for name in targets:
            if await _run("kscreen-doctor", f"output.{name}.vrrpolicy.never") is not None:
                log.info("VRR: off on %s while streaming (was %s)", name, targets[name])
            else:
                log.warning("VRR: could not change policy on %s", name)

    async def restore(self) -> None:
        """Put the saved policies back (idempotent)."""
        if not self._active:
            return
        self._active = False
        saved, self._saved = self._saved, {}
        for name, word in saved.items():
            if await _run("kscreen-doctor", f"output.{name}.vrrpolicy.{word}") is not None:
                log.info("VRR: restored %s on %s", word, name)
            else:
                log.warning("VRR: could not restore %s on %s", word, name)
        self._forget()

    def _remember(self) -> None:
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            self._state.write_text(json.dumps(self._saved))
        except OSError as e:
            log.warning("VRR: could not write %s: %s", self._state, e)

    def _forget(self) -> None:
        try:
            self._state.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("VRR: could not remove %s: %s", self._state, e)
