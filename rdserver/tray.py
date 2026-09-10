"""System-tray indicator for rdserver: is it live, who's connected, and a
one-click view-only link.

Runs as a SEPARATE process from the server (it pulls in GTK/AppIndicator, which
the server must not). It reads the server's config from ~/.config/rdserver/rd.env
-- the port from RD_OPTS, the control token from RD_TOKEN -- and talks to the
server's local API (/api/status, /api/view-link, /api/revoke-views) over
localhost. The API is guarded by the control token, which the tray already holds.

    python3 -m rdserver.tray        # (usually run by the rd-tray.service unit)

Icon:  grey  = server not reachable
       blue  = live, nobody connected
       green = someone connected (controller and/or viewers)
Menu:  status lines, "Create view-only link" (mints + copies + notifies),
       "Revoke view links", "Copy control link", "Restart server", "Quit".
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import threading
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):        # older systems ship the non-Ayatana one
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator
from gi.repository import GLib, Gtk  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tray")

ENV_FILE = Path.home() / ".config" / "rdserver" / "rd.env"
POLL_SECONDS = 4

# Themed icon names (present in the KDE/Breeze + most freedesktop themes).
ICON_DOWN = "user-offline"
ICON_IDLE = "video-display"
ICON_ACTIVE = "user-available"


def _read_env() -> dict[str, str]:
    """Parse rd.env: RD_TOKEN plus --port out of RD_OPTS. Best-effort."""
    port, token = 8098, ""
    try:
        text = ENV_FILE.read_text()
    except OSError:
        return {"port": str(port), "token": token}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RD_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("RD_OPTS="):
            m = re.search(r"--port\s+(\d+)", line)
            if m:
                port = int(m.group(1))
    return {"port": str(port), "token": token}


class Tray:
    def __init__(self) -> None:
        env = _read_env()
        self.port = env["port"]
        self.token = env["token"]
        # Poll over localhost; the cert is self-signed, so don't verify (we are
        # talking to our own machine, and the token is the real gate).
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        self._base = f"https://127.0.0.1:{self.port}"

        self.ind = AppIndicator.Indicator.new(
            "rdserver", ICON_DOWN,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_title("Remote desktop")

        self.menu = Gtk.Menu()
        self._status_item = Gtk.MenuItem(label="Checking…")
        self._status_item.set_sensitive(False)
        self.menu.append(self._status_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        view_item = self._add("Create view-only link", self._on_view_link)
        self._add("Revoke view links", self._on_revoke)
        self._add("Copy control link", self._on_copy_control)
        self.menu.append(Gtk.SeparatorMenuItem())
        self._add("Restart server", self._on_restart)
        self._add("Quit tray", lambda _w: Gtk.main_quit())
        self.menu.show_all()
        self.ind.set_menu(self.menu)
        # Plasma's Wayland systray has a long-standing bug where an icon's
        # RIGHT-CLICK context menu dismisses itself as the pointer moves onto
        # it (affects every tray icon, not just ours). So give a menu-free path:
        # MIDDLE-click the icon mints + copies a view-only link directly, no
        # popup involved. (Left-click still opens the menu, which is far less
        # affected than right-click.)
        try:
            self.ind.set_secondary_activate_target(view_item)
        except Exception:          # noqa: BLE001  (older API)
            pass

        # Applied UI state -- so a poll that changed nothing touches nothing.
        # (Plasma dismisses an OPEN tray menu when the item's DBusMenu layout
        # updates, so a needless set_label() every few seconds is exactly what
        # made the menu vanish under the cursor.)
        self._cur_icon = None
        self._cur_label = None
        self._base_url = ""
        self._menu_open = False
        self._polling = False
        # Skip the background poll while the menu is open (belt and braces with
        # the change-guard above), and force one fresh poll the moment it opens.
        self.menu.connect("show", self._on_menu_show)
        self.menu.connect("hide", self._on_menu_hide)

        self._tick()               # first poll (async)
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    def _add(self, label: str, cb) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", cb)
        self.menu.append(item)
        return item

    def _on_menu_show(self, _w) -> None:
        self._menu_open = True

    def _on_menu_hide(self, _w) -> None:
        self._menu_open = False

    # ----- server API -------------------------------------------------------

    def _api(self, path: str, method: str = "GET"):
        req = urllib.request.Request(
            f"{self._base}{path}", method=method,
            headers={"X-RD-Token": self.token})
        with urllib.request.urlopen(req, timeout=4, context=self._ssl) as r:
            return json.load(r)

    def _tick(self) -> bool:
        # Don't disturb the menu while it's open, and never run two polls at
        # once. The HTTP call runs on a WORKER THREAD -- doing it on the GTK
        # main loop froze the whole tray (and the just-opened menu) for up to
        # the timeout whenever the server was slow or down.
        if self._menu_open or self._polling:
            return True
        self._polling = True
        threading.Thread(target=self._poll_worker, daemon=True).start()
        return True                # keep the timer

    def _poll_worker(self) -> None:
        try:
            st = self._api("/api/status")
        except Exception:          # noqa: BLE001  (down, refused, timeout)
            st = None
        GLib.idle_add(self._apply, st)

    def _apply(self, st) -> bool:
        if st is None:
            self._set_icon(ICON_DOWN, "server not reachable")
            self._set_label("rdserver: not running")
            self._polling = False
            return False
        conn = bool(st.get("controller")) or st.get("viewers", 0) > 0
        self._set_icon(ICON_ACTIVE if conn else ICON_IDLE,
                       "connected" if conn else "live, idle")
        who = []
        if st.get("controller"):
            src = st.get("game") or st.get("source")
            who.append(f"controller ({src})" if src else "controller")
        if st.get("viewers"):
            who.append(f"{st['viewers']}/{st.get('max_viewers', '?')} viewers")
        label = "rdserver: live"
        if who:
            label += " — " + ", ".join(who)
        if st.get("view_links"):
            label += f"  [{st['view_links']} view link(s)]"
        self._set_label(label)
        self._base_url = st.get("base_url") or ""
        self._polling = False
        return False               # one-shot idle callback

    def _set_icon(self, name: str, desc: str) -> None:
        if name != self._cur_icon:
            self._cur_icon = name
            self.ind.set_icon_full(name, desc)

    def _set_label(self, label: str) -> None:
        if label != self._cur_label:
            self._cur_label = label
            self._status_item.set_label(label)

    # ----- menu actions -----------------------------------------------------

    def _on_view_link(self, _w) -> None:
        try:
            res = self._api("/api/view-link", method="POST")
        except Exception as e:     # noqa: BLE001
            self._notify("Remote desktop", f"Could not create link: {e}")
            return
        url = res.get("url")
        if not url:
            self._notify("Remote desktop", "Link created but no URL "
                         "(set --public-host so links are shareable).")
            return
        self._copy(url)
        ttl = res.get("ttl_s")
        when = f"expires in ~{round(ttl / 3600)} h" if ttl else "no expiry"
        self._notify("View-only link copied", f"{when}\n{url}")

    def _on_revoke(self, _w) -> None:
        try:
            res = self._api("/api/revoke-views", method="POST")
        except Exception as e:     # noqa: BLE001
            self._notify("Remote desktop", f"Could not revoke: {e}")
            return
        self._notify("View links revoked",
                     f"{res.get('tokens', 0)} link(s), "
                     f"{res.get('viewers', 0)} viewer(s) disconnected")

    def _on_copy_control(self, _w) -> None:
        base = getattr(self, "_base_url", "")
        if not base:
            self._notify("Remote desktop", "No control URL yet (server down?)")
            return
        self._copy(f"{base}/?token={self.token}")
        self._notify("Control link copied",
                     "Full control of this machine — treat it like a password.")

    def _on_restart(self, _w) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=None, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Restart the remote-desktop server?")
        dlg.format_secondary_text("Any active session will be dropped "
                                  "(it will reconnect on its own).")
        if dlg.run() == Gtk.ResponseType.OK:
            subprocess.Popen(["systemctl", "--user", "restart", "rdserver"])
            self._notify("Remote desktop", "Restarting…")
        dlg.destroy()

    # ----- helpers ----------------------------------------------------------

    def _copy(self, text: str) -> None:
        try:
            subprocess.run(["wl-copy"], input=text.encode(), timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            clip = Gtk.Clipboard.get_default(self.menu.get_display())
            clip.set_text(text, -1)
            clip.store()

    def _notify(self, title: str, body: str) -> None:
        try:
            subprocess.Popen(["notify-send", "-a", "Remote desktop",
                              "-i", ICON_IDLE, title, body])
        except OSError:
            log.info("%s: %s", title, body)


def main() -> int:
    Tray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
