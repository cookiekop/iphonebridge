"""Long-lived MAP + PBAP OBEX sessions.

Per spike/RESULTS.md §2: the iPhone refuses repeat OBEX connects within a
short window. The daemon keeps one MAP session and one PBAP session open
for its lifetime, reopening only on observed failure.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

import dbus
import dbus.exceptions

from iphonebridge import config
from iphonebridge.bus import obex, bluez

log = logging.getLogger(__name__)


class SessionError(RuntimeError):
    pass


@dataclass(slots=True)
class ObexSession:
    """A live OBEX session against the iPhone (Target = "MAP" or "PBAP")."""

    target: str               # "MAP" or "PBAP"
    path: str                 # /org/bluez/obex/client/session{N}

    @property
    def message_access(self) -> dbus.Interface:
        return obex(self.path, "org.bluez.obex.MessageAccess1")

    @property
    def phonebook(self) -> dbus.Interface:
        return obex(self.path, "org.bluez.obex.PhonebookAccess1")

    @property
    def properties(self) -> dbus.Interface:
        return obex(self.path, "org.freedesktop.DBus.Properties")


def _client() -> dbus.Interface:
    return obex("/org/bluez/obex", "org.bluez.obex.Client1")


def _restart_obexd() -> None:
    """Restart user obex.service to clear stale state — see spike/RESULTS.md §2."""
    log.info("restarting user obex.service for a clean state")
    subprocess.run(["systemctl", "--user", "restart", "obex.service"],
                   check=False)
    time.sleep(1.0)


def _create_session(target: str, *, retry_on_forbidden: bool = True) -> ObexSession:
    log.info("creating OBEX session (Target=%s) to %s", target, config.IPHONE_MAC)
    try:
        source = str(bluez(f"/org/bluez/{config.ADAPTER}", "org.freedesktop.DBus.Properties").Get(
            "org.bluez.Adapter1", "Address"))
        path = str(_client().CreateSession(
            config.IPHONE_MAC, {"Target": target, "Source": source}, timeout=30.0
        ))
        return ObexSession(target=target, path=path)
    except dbus.exceptions.DBusException as e:
        msg = e.get_dbus_message() or ""
        if retry_on_forbidden and ("Forbidden" in msg or "0x43" in msg):
            log.warning("OBEX %s got Forbidden — restarting obexd and "
                        "retrying once", target)
            _restart_obexd()
            return _create_session(target, retry_on_forbidden=False)
        raise SessionError(f"CreateSession({target}) failed: {e.get_dbus_name()}: {msg}")


class SessionManager:
    """Opens and tracks one MAP and one PBAP session for the daemon lifetime."""

    def __init__(self, *, include_contacts: bool = True) -> None:
        self.include_contacts = include_contacts
        self.map: ObexSession | None = None
        self.pbap: ObexSession | None = None

    def open_all(self) -> None:
        # Restart obexd once at start to give us a known-clean baseline.
        # Idempotent — even if obexd was fine, this just re-creates it.
        _restart_obexd()
        self.map = _create_session("MAP")
        log.info("MAP session: %s", self.map.path)
        if self.include_contacts:
            self.pbap = _create_session("PBAP")
            log.info("PBAP session: %s", self.pbap.path)

    def close_all(self) -> None:
        client = _client()
        for sess in (self.map, self.pbap):
            if sess is None:
                continue
            try:
                client.RemoveSession(sess.path)
                log.info("closed %s session: %s", sess.target, sess.path)
            except dbus.exceptions.DBusException as e:
                log.debug("RemoveSession(%s): %s", sess.path, e.get_dbus_name())
        self.map = None
        self.pbap = None

    # Convenience accessors
    def is_healthy(self) -> bool:
        if self.map is None:
            return False
        try:
            self.map.properties.GetAll("org.bluez.obex.Session1", timeout=2.0)
            return True
        except dbus.exceptions.DBusException:
            return False

    @property
    def map_path(self) -> str:
        if self.map is None:
            raise SessionError("MAP session not open")
        return self.map.path

    @property
    def pbap_path(self) -> str:
        if self.pbap is None:
            raise SessionError("PBAP session not open")
        return self.pbap.path
