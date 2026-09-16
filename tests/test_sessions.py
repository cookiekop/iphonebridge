import subprocess
from unittest.mock import Mock

import dbus.exceptions
import pytest

from iphonebridge.obex import sessions


@pytest.fixture
def client(monkeypatch):
    fake = Mock()
    fake.CreateSession.side_effect = lambda _phone, options, **_: f'/session/{options["Target"]}'
    adapter = Mock()
    adapter.Get.return_value = "01:23:45:67:89:AB"
    monkeypatch.setattr(sessions, "_client", lambda: fake)
    monkeypatch.setattr(sessions, "bluez", lambda *_: adapter)
    monkeypatch.setattr(sessions, "obex", lambda *_: fake)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("Must not restart obexd"))
    return fake


def error(name="org.bluez.obex.Error.Forbidden"):
    return dbus.exceptions.DBusException("Forbidden", name=name)


@pytest.mark.parametrize("name", ["org.bluez.obex.Error.Forbidden",
                                  "org.freedesktop.DBus.Error.NoReply"])
def test_failed_map_attempt_is_not_retried_immediately(client, name):
    manager = sessions.SessionManager(include_contacts=False)
    client.CreateSession.side_effect = error(name)
    with pytest.raises(sessions.SessionError, match=name):
        manager.open_all()
    assert client.CreateSession.call_count == 1
    assert manager.map is None
    assert not manager.is_healthy()
    client.RemoveSession.assert_not_called()


def test_permission_retry_recovers_and_reuses_live_map(client):
    manager = sessions.SessionManager(include_contacts=False)
    client.CreateSession.side_effect = [error(), "/session/MAP"]
    with pytest.raises(sessions.SessionError):
        manager.open_all()
    manager.open_all()
    original = manager.map
    manager.open_all()
    assert manager.map is original
    assert manager.is_healthy()
    assert manager.pbap is None
    assert client.CreateSession.call_count == 2
    client.RemoveSession.assert_not_called()


def test_contacts_retry_preserves_working_map(client):
    manager = sessions.SessionManager()
    client.CreateSession.side_effect = ["/session/MAP", error(), "/session/PBAP"]
    with pytest.raises(sessions.SessionError):
        manager.open_all()
    original = manager.map
    assert manager.is_healthy()
    manager.open_all()
    manager.open_all()
    assert manager.map is original
    assert [call.args[1]["Target"] for call in client.CreateSession.call_args_list] == ["MAP", "PBAP", "PBAP"]


def test_lost_session_is_reopened(client):
    manager = sessions.SessionManager(include_contacts=False)
    manager.open_all()
    client.GetAll.side_effect = error("org.freedesktop.DBus.Error.UnknownObject")
    assert not manager.is_healthy()
    manager.open_all()
    assert client.CreateSession.call_count == 2


def test_failed_reopen_drops_stale_handle(client):
    manager = sessions.SessionManager(include_contacts=False)
    manager.open_all()
    client.GetAll.side_effect = error()
    client.CreateSession.side_effect = error()
    with pytest.raises(sessions.SessionError):
        manager.open_all()
    assert manager.map is None


def test_close_removes_only_owned_sessions(client):
    manager = sessions.SessionManager()
    manager.open_all()
    manager.close_all()
    assert [call.args[0] for call in client.RemoveSession.call_args_list] == ["/session/MAP", "/session/PBAP"]
    assert manager.map is manager.pbap is None
    manager.close_all()
    assert client.RemoveSession.call_count == 2


def test_close_tolerates_obex_service_unavailable(client, monkeypatch):
    manager = sessions.SessionManager()
    manager.open_all()
    monkeypatch.setattr(sessions, "_client", Mock(side_effect=error("org.freedesktop.DBus.Error.ServiceUnknown")))
    manager.close_all()
    assert manager.map is manager.pbap is None


def test_daemon_retries_on_timer_and_stops_after_success(client, monkeypatch):
    from iphonebridge import daemon

    phone = daemon.Daemon(headless=True)
    timer = Mock(return_value=42)
    setup = Mock()
    monkeypatch.setattr(daemon.GLib, "timeout_add_seconds", timer)
    monkeypatch.setattr(phone, "_post_sessions_setup", setup)
    client.CreateSession.side_effect = [error(), error(), "/session/MAP"]
    phone._try_open_sessions(first_attempt=True)
    timer.assert_called_once_with(daemon.SESSION_RETRY_SEC, phone._retry_sessions)
    assert phone._retry_sessions() is True
    assert phone._session_retry_id == 42
    assert phone._retry_sessions() is False
    assert phone._session_retry_id is None
    setup.assert_called_once()
    assert client.CreateSession.call_count == 3
