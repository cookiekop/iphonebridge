"""MAP outgoing message send (SMS + iMessage on iOS 26.5).

Per spike/07_map_send.py and spike/RESULTS.md §6, MessageAccess1.PushMessage
with a properly-formed bMessage works on iOS 26.5 — and when the recipient
is iMessage-capable, iOS routes the outgoing as iMessage automatically
(blue bubble). The same code path handles SMS too.

Public surface:
    send_message(session_path, recipient_phone, body) -> str (transfer path)

Caller (typically the daemon's DBus service) owns the MAP session and
just passes its path here.
"""
from __future__ import annotations

import logging
import math
import re
import tempfile
import time
from pathlib import Path

import dbus
import dbus.exceptions

log = logging.getLogger(__name__)


class SendFailed(RuntimeError):
    """The phone transfer was explicitly rejected or failed."""


class SendOutcomeUnknown(RuntimeError):
    """The phone may have accepted the message; do not resend automatically."""


def obex(path: str, interface: str) -> dbus.Interface:
    from iphonebridge.bus import obex as get_interface
    return get_interface(path, interface)


def _byte_stuff(body: str) -> str:
    """Apply MAP bMessage byte-stuffing.

    Lines in the message body that start with `BEGIN:`, `END:`, or other
    keywords the bMessage parser would intercept must be prefixed with a
    single space. Conservative implementation: prefix any line starting
    with `BEGIN:` or `END:`.
    """
    return "\n".join(
        (" " + line) if line.startswith(("BEGIN:", "END:")) else line
        for line in body.splitlines()
    )


def build_bmessage(recipient: str, body: str) -> str:
    """Return a complete bMessage suitable for MAP PushMessage.

    Structure per Bluetooth MAP 1.4 spec:
      • Originator VCARD (empty for outgoing — iPhone fills in)
      • BENV → recipient VCARD + BBODY → MSG body
    """
    if re.fullmatch(r"\+?[0-9]{1,80}", recipient) is None:
        raise ValueError("Recipient must be digits with an optional leading +")
    if not body:
        raise ValueError("Message body must not be empty")
    stuffed = _byte_stuff(body)
    encoded_len = len(stuffed.encode("utf-8"))
    lines = [
        "BEGIN:BMSG",
        "VERSION:1.0",
        "STATUS:UNREAD",
        "TYPE:SMS_GSM",
        "FOLDER:telecom/msg/outbox",
        # Originator
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        "TEL:",
        "END:VCARD",
        "BEGIN:BENV",
        # Recipient
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        f"TEL:{recipient}",
        "END:VCARD",
        "BEGIN:BBODY",
        "CHARSET:UTF-8",
        f"LENGTH:{encoded_len}",
        "BEGIN:MSG",
        stuffed,
        "END:MSG",
        "END:BBODY",
        "END:BENV",
        "END:BMSG",
    ]
    return "\r\n".join(lines) + "\r\n"


def send_message(
    session_path: str,
    recipient: str,
    body: str,
    *,
    folder: str = "telecom/msg/outbox",
    poll_timeout_s: float = 30.0,
) -> str:
    """Push a message via the given MAP session.

    Return only after confirmed transfer completion, not carrier delivery.
    Timeout or lost acknowledgement raises SendOutcomeUnknown, never success.
    """
    if not math.isfinite(poll_timeout_s) or poll_timeout_s <= 0:
        raise ValueError("Transfer timeout must be positive and finite")
    bmsg = build_bmessage(recipient, body)
    with tempfile.TemporaryDirectory(prefix="ibridge_send_") as directory:
        tmp = Path(directory) / "message.bmsg"
        tmp.write_text(bmsg, encoding="utf-8")
        map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
        deadline = time.monotonic() + poll_timeout_s
        log.info("Submitting MAP transfer (%d bytes)", len(body.encode("utf-8")))
        try:
            ret = map_iface.PushMessage(str(tmp), folder, {}, timeout=poll_timeout_s)
        except dbus.exceptions.DBusException as e:
            if e.get_dbus_name() in {
                "org.bluez.obex.Error.InvalidArguments",
                "org.bluez.obex.Error.NotAuthorized",
                "org.bluez.obex.Error.Forbidden",
                "org.bluez.obex.Error.NotSupported",
            }:
                raise SendFailed("MAP submission was rejected") from e
            raise SendOutcomeUnknown("MAP submission was not acknowledged; do not automatically resend") from e

        if not isinstance(ret, (tuple, list)) or len(ret) != 2 or not isinstance(ret[1], dict):
            raise SendOutcomeUnknown("Invalid MAP acknowledgement; do not automatically resend")
        transfer_path = str(ret[0])
        status = str(ret[1].get("Status", "queued"))
        try:
            while True:
                if status == "complete":
                    log.info("MAP transfer complete; carrier delivery is unconfirmed")
                    return transfer_path
                if status == "error":
                    raise SendFailed("MAP transfer reported an error")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SendOutcomeUnknown("MAP transfer timed out; do not automatically resend")
                try:
                    tprops = obex(transfer_path, "org.freedesktop.DBus.Properties")
                    status = str(tprops.Get("org.bluez.obex.Transfer1", "Status", timeout=remaining))
                except dbus.exceptions.DBusException as e:
                    raise SendOutcomeUnknown("MAP transfer acknowledgement was lost; do not automatically resend") from e
                if status not in ("complete", "error"):
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        except SendOutcomeUnknown:
            # Cancellation cannot prove that a carrier send did not already occur.
            try:
                obex(transfer_path, "org.bluez.obex.Transfer1").Cancel(timeout=1.0)
            except dbus.exceptions.DBusException:
                pass
            raise
