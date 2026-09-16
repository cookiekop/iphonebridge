from pathlib import Path

import dbus.exceptions
import pytest

from iphonebridge.obex import map_send


class FakeTransfer:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.clock = 0.0
        self.cancelled = False
        self.files: list[Path] = []
        self.push_calls = 0
        self.initial_status = "queued"
        self.push_error = None

    def sleep(self, seconds):
        self.clock += seconds

    def PushMessage(self, filename, folder, options, *, timeout):
        self.push_calls += 1
        path = Path(filename)
        self.files.append(path)
        assert path.is_file()
        assert path.parent.stat().st_mode & 0o077 == 0
        if self.push_error is not None:
            raise self.push_error
        return ("/transfer/1", {"Status": self.initial_status})

    def Get(self, interface, name, *, timeout):
        assert timeout > 0
        value = next(self.statuses, "active")
        if isinstance(value, Exception):
            raise value
        return value

    def Cancel(self, *, timeout):
        self.cancelled = True


@pytest.fixture
def transfer(monkeypatch):
    def create(statuses):
        fake = FakeTransfer(statuses)
        monkeypatch.setattr(map_send, "obex", lambda *_: fake)
        monkeypatch.setattr(map_send.time, "monotonic", lambda: fake.clock)
        monkeypatch.setattr(map_send.time, "sleep", fake.sleep)
        return fake
    return create


def send():
    return map_send.send_message("/session/1", "+15551234567", "private OTP 817293", poll_timeout_s=0.3)


def test_complete_is_required_and_body_is_not_logged(transfer, caplog):
    fake = transfer(["active", "complete"])
    with caplog.at_level("DEBUG"):
        assert send() == "/transfer/1"
    assert "817293" not in caplog.text
    assert fake.push_calls == 1
    assert not fake.cancelled
    assert all(not path.exists() for path in fake.files)


def test_completion_in_initial_reply_needs_no_poll(transfer):
    fake = transfer([AssertionError("must not poll")])
    fake.initial_status = "complete"
    assert send() == "/transfer/1"


def test_explicit_transfer_error_is_failure(transfer):
    fake = transfer(["error"])
    with pytest.raises(map_send.SendFailed):
        send()
    assert all(not path.exists() for path in fake.files)


def test_timeout_is_unknown_and_never_resubmits(transfer):
    fake = transfer(["active"])
    with pytest.raises(map_send.SendOutcomeUnknown, match="timed out"):
        send()
    assert fake.push_calls == 1
    assert fake.cancelled
    assert all(not path.exists() for path in fake.files)


def test_disappearing_transfer_is_unknown(transfer):
    fake = transfer([dbus.exceptions.DBusException("Object disappeared")])
    with pytest.raises(map_send.SendOutcomeUnknown, match="acknowledgement was lost"):
        send()
    assert fake.push_calls == 1
    assert fake.cancelled


def test_missing_push_reply_is_unknown(transfer):
    fake = transfer([])
    fake.push_error = dbus.exceptions.DBusException("No reply", name="org.freedesktop.DBus.Error.NoReply")
    with pytest.raises(map_send.SendOutcomeUnknown, match="not acknowledged"):
        send()
    assert fake.push_calls == 1
    assert all(not path.exists() for path in fake.files)


def test_explicit_push_rejection_is_failure(transfer):
    fake = transfer([])
    fake.push_error = dbus.exceptions.DBusException("Not authorized", name="org.bluez.obex.Error.NotAuthorized")
    with pytest.raises(map_send.SendFailed, match="rejected"):
        send()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_does_not_submit(transfer, timeout):
    fake = transfer([])
    with pytest.raises(ValueError):
        map_send.send_message("/session/1", "+15551234567", "message", poll_timeout_s=timeout)
    assert fake.push_calls == 0


@pytest.mark.parametrize("number", ["", "+", "123\r\nEND:VCARD", "１２３", "1" * 81])
def test_invalid_recipient_is_rejected(number):
    with pytest.raises(ValueError):
        map_send.build_bmessage(number, "message")
