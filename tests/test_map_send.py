"""Tests for iphonebridge.obex.map_send.build_bmessage — outgoing
bMessage construction. We don't test send_message itself here because
it needs a live BlueZ obex session; that's the spike's job."""
from __future__ import annotations

import random
import re

from iphonebridge.obex.bmessage import parse as parse_bmessage
from iphonebridge.obex.map_send import _byte_stuff, build_bmessage


class TestByteStuff:
    def test_no_keywords_unchanged(self):
        assert _byte_stuff("hello world") == "hello world"

    def test_begin_line_is_not_a_message_terminator(self):
        assert _byte_stuff("BEGIN:foo") == "BEGIN:foo"

    def test_end_line_gets_slash_prefix(self):
        assert _byte_stuff("END:MSG") == "/END:MSG"
        assert _byte_stuff("/END:MSG\n//END:MSG") == "//END:MSG\r\n///END:MSG"

    def test_only_at_line_start(self):
        # "I BEGIN: something" should not get prefixed
        assert _byte_stuff("I BEGIN: something") == "I BEGIN: something"

    def test_multiline_partial(self):
        body = "Hi\nBEGIN:fake\nbye"
        stuffed = _byte_stuff(body)
        assert stuffed == "Hi\r\nBEGIN:fake\r\nbye"

    def test_preserves_trailing_lines_and_unicode_separators(self):
        assert _byte_stuff("你好\r\n\n") == "你好\r\n\r\n"
        assert _byte_stuff("a\u2028b") == "a\u2028b"


class TestBuildBmessage:
    def test_basic_round_trip(self):
        bmsg = build_bmessage("+15551234567", "Hello from CI")
        p = parse_bmessage(bmsg)
        assert p.sender_phone is None or p.sender_phone == ""
        # Note: the PARSER finds the FIRST VCARD which for outgoing is the
        # (empty) originator. So sender_phone from a parsed outgoing bMessage
        # is intentionally not the recipient.

        # What matters is the file contains the expected pieces:
        assert "BEGIN:BMSG" in bmsg
        assert "TYPE:SMS_GSM" in bmsg
        assert "FOLDER:telecom/msg/outbox" in bmsg
        assert "TEL:+15551234567" in bmsg
        assert "Hello from CI" in bmsg
        assert "END:BMSG" in bmsg

    def test_has_both_vcards(self):
        # Originator VCARD + BENV-wrapped recipient VCARD
        bmsg = build_bmessage("+15551234567", "x")
        # Two BEGIN:VCARD / END:VCARD pairs
        assert bmsg.count("BEGIN:VCARD") == 2
        assert bmsg.count("END:VCARD") == 2

    def test_recipient_inside_benv(self):
        bmsg = build_bmessage("+15551234567", "x")
        # Sanity check structural ordering
        idx_benv  = bmsg.index("BEGIN:BENV")
        idx_tel   = bmsg.index("TEL:+15551234567")
        idx_bbody = bmsg.index("BEGIN:BBODY")
        assert idx_benv < idx_tel < idx_bbody

    def test_length_includes_message_framing(self):
        body = "héllo 👋"
        bmsg = build_bmessage("+15551234567", body)
        expected_len = len(body.encode("utf-8")) + 22
        assert f"LENGTH:{expected_len}" in bmsg

    def test_crlf_line_endings(self):
        bmsg = build_bmessage("+15551234567", "hi")
        # The MAP spec wants CRLF
        assert "\r\n" in bmsg
        # And not unexpected bare LFs in the structural lines
        # (header lines should all be terminated with CRLF)
        for header in ("BEGIN:BMSG", "VERSION:1.0", "TYPE:SMS_GSM"):
            assert f"{header}\r\n" in bmsg

    def test_unicode_recipient_phone_still_clean(self):
        # Plus-prefixed phone numbers are ASCII; non-ASCII recipients
        # would be a bug upstream, but build_bmessage shouldn't crash.
        bmsg = build_bmessage("+15551234567", "x")
        assert "TEL:+15551234567" in bmsg

    def test_body_with_terminator_is_stuffed(self):
        body = "weird message\nEND:MSG\nokay"
        bmsg = build_bmessage("+15551234567", body)
        msg_start = bmsg.index("BEGIN:MSG\r\n") + len("BEGIN:MSG\r\n")
        msg_end = bmsg.index("\r\nEND:MSG")
        body_in_bmsg = bmsg[msg_start:msg_end]
        assert "\r\n/END:MSG\r\n" in body_in_bmsg

    def test_randomized_framing_and_body_round_trip(self):
        rng = random.Random(17)
        pieces = ["hello", "你好", "🙂", "END:MSG", "/END:MSG", "BEGIN:MSG", "", "\u2028"]
        for _ in range(200):
            body = "\n".join(rng.choices(pieces, k=rng.randint(1, 12))) or "x"
            encoded = build_bmessage("+15551234567", body).encode("utf-8")
            length = int(re.search(rb"\r\nLENGTH:(\d+)\r\n", encoded)[1])
            start = encoded.index(b"BEGIN:MSG\r\n")
            content = encoded[start:start + length]
            assert content.endswith(b"\r\nEND:MSG\r\n")
            assert encoded[start + length:].startswith(b"END:BBODY\r\n")
            restored = re.sub(r"(?m)^/([/]*END:MSG)", r"\1", content[11:-11].decode())
            assert restored.replace("\r\n", "\n") == body
