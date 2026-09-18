"""The Discord gateway: a real protocol with state, driven against a fake."""

from __future__ import annotations

import json
import socket
import struct
import unittest
from typing import Any

from bothy.discord import (
    FATAL_CLOSE_CODES,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    RESUME_FAILURE_THRESHOLD,
    GatewayListener,
    GatewaySession,
    Intents,
    chunks,
    message_subject,
)
from bothy.ws import OP_TEXT, WebSocket


def server_frame(payload: bytes) -> bytes:
    """A text frame as a server sends it: unmasked. Written independently."""
    header = bytearray([0x80 | OP_TEXT])
    length = len(payload)
    if length < 126:
        header.append(length)
    else:
        header.append(126)
        header += struct.pack("!H", length)
    return bytes(header) + payload


def decode_client(raw: bytes) -> list[dict]:
    out: list[dict] = []
    index = 0
    while index + 2 <= len(raw):
        opcode = raw[index] & 0x0F
        length = raw[index + 1] & 0x7F
        index += 2
        if length == 126:
            length = struct.unpack("!H", raw[index:index + 2])[0]
            index += 2
        key = raw[index:index + 4]
        index += 4
        body = bytes(b ^ key[i % 4] for i, b in enumerate(raw[index:index + length]))
        index += length
        if opcode == OP_TEXT:
            out.append(json.loads(body))
    return out


class IntentTests(unittest.TestCase):
    def test_message_content_is_requested_explicitly(self) -> None:
        """Without it, messages arrive empty and the bot looks broken, not refused."""
        bits = Intents.default()
        self.assertTrue(bits & Intents.MESSAGE_CONTENT)
        self.assertIn("MESSAGE_CONTENT", Intents.describe(bits))

    def test_privileged_member_intent_is_off_unless_asked_for(self) -> None:
        self.assertFalse(Intents.default() & Intents.GUILD_MEMBERS)
        self.assertTrue(Intents.default(guild_members=True) & Intents.GUILD_MEMBERS)

    def test_the_bitfield_is_arithmetic_not_a_magic_number(self) -> None:
        expected = (Intents.GUILDS | Intents.GUILD_MESSAGES | Intents.DIRECT_MESSAGES
                    | Intents.GUILD_MESSAGE_REACTIONS | Intents.DIRECT_MESSAGE_REACTIONS
                    | Intents.MESSAGE_CONTENT)
        self.assertEqual(Intents.default(), expected)


class LaneTests(unittest.TestCase):
    def test_one_channel_is_one_lane(self) -> None:
        first = {"channel_id": "C1", "id": "m1"}
        second = {"channel_id": "C1", "id": "m2"}
        other = {"channel_id": "C9", "id": "m3"}
        self.assertEqual(message_subject(first), message_subject(second))
        self.assertNotEqual(message_subject(first), message_subject(other))

    def test_chunks_respect_discords_cap(self) -> None:
        big = "\n".join("x" * 300 for _ in range(50))
        parts = chunks(big)
        self.assertTrue(all(len(part) <= 1900 for part in parts))
        self.assertEqual("".join(parts), big)


class ProtocolTests(unittest.TestCase):
    """The frame handling, driven directly so state is observable."""

    def setUp(self) -> None:
        self.server, client = socket.socketpair()
        self.ws = WebSocket(client)
        self.stored: list[dict] = []
        self.errors: list[tuple[str, bool]] = []
        self.listener = GatewayListener(
            bot_token="token", on_message=self._store,
            on_error=lambda message, fatal: self.errors.append((message, fatal)),
        )

    def _store(self, message: dict[str, Any]) -> bool:
        self.stored.append(message)
        return True

    def sent(self) -> list[dict]:
        self.server.settimeout(0.3)
        raw = b""
        try:
            while True:
                chunk = self.server.recv(4096)
                if not chunk:
                    break
                raw += chunk
        except (TimeoutError, OSError):
            pass
        return decode_client(raw)

    def test_hello_leads_to_identify_on_a_fresh_session(self) -> None:
        self.listener.handle(self.ws, {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}})
        payloads = self.sent()
        identify = [p for p in payloads if p["op"] == OP_IDENTIFY]
        self.assertEqual(len(identify), 1)
        self.assertEqual(identify[0]["d"]["token"], "token")
        self.assertEqual(identify[0]["d"]["intents"], Intents.default())

    def test_hello_leads_to_resume_when_a_session_exists(self) -> None:
        self.listener.session = GatewaySession(session_id="s1", resume_url="wss://x", sequence=41)
        self.listener.handle(self.ws, {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}})
        resume = [p for p in self.sent() if p["op"] == OP_RESUME]
        self.assertEqual(resume[0]["d"], {"token": "token", "session_id": "s1", "seq": 41})

    def test_ready_records_what_a_resume_will_need(self) -> None:
        self.listener.handle(self.ws, {"op": OP_DISPATCH, "t": "READY", "s": 1,
                                       "d": {"session_id": "abc",
                                             "resume_gateway_url": "wss://resume.example"}})
        self.assertEqual(self.listener.session.session_id, "abc")
        self.assertEqual(self.listener.session.resume_url, "wss://resume.example")
        self.assertEqual(self.listener.session.sequence, 1)

    def test_an_out_of_band_heartbeat_request_is_answered_immediately(self) -> None:
        self.listener.session.sequence = 7
        self.listener.handle(self.ws, {"op": OP_HEARTBEAT, "d": None})
        beats = [p for p in self.sent() if p["op"] == OP_HEARTBEAT]
        self.assertEqual(beats[0]["d"], 7)

    def test_an_ack_clears_the_pending_flag(self) -> None:
        self.listener._awaiting_ack = True
        self.listener.handle(self.ws, {"op": OP_HEARTBEAT_ACK})
        self.assertFalse(self.listener._awaiting_ack)

    def test_a_message_is_handed_on_and_advances_the_sequence(self) -> None:
        self.listener.handle(self.ws, {"op": OP_DISPATCH, "t": "MESSAGE_CREATE", "s": 12,
                                       "d": {"channel_id": "C1", "content": "hello"}})
        self.assertEqual(len(self.stored), 1)
        self.assertEqual(self.listener.session.sequence, 12)

    def test_a_message_that_was_not_stored_does_not_advance_the_sequence(self) -> None:
        """So a RESUME replays it rather than skipping past it."""
        listener = GatewayListener(bot_token="t", on_message=lambda _: False)
        listener.session.sequence = 5
        listener.handle(self.ws, {"op": OP_DISPATCH, "t": "MESSAGE_CREATE", "s": 6,
                                  "d": {"channel_id": "C1"}})
        self.assertEqual(listener.session.sequence, 5)

    def test_a_handler_that_raises_does_not_drop_the_connection(self) -> None:
        def explode(_: dict) -> bool:
            raise RuntimeError("boom")

        listener = GatewayListener(bot_token="t", on_message=explode,
                                   on_error=lambda m, f: self.errors.append((m, f)))
        listener.handle(self.ws, {"op": OP_DISPATCH, "t": "MESSAGE_CREATE", "s": 3, "d": {}})
        self.assertTrue(any("boom" in message for message, _ in self.errors))

    def test_a_resumable_invalid_session_is_retried_before_being_abandoned(self) -> None:
        self.listener.session = GatewaySession(session_id="s1", sequence=3)
        self.listener.handle(self.ws, {"op": OP_INVALID_SESSION, "d": True})
        self.assertEqual(self.listener.session.session_id, "s1", "kept for one more try")
        self.assertEqual(self.listener.session.resume_failures, 1)

    def test_an_unresumable_session_is_thrown_away_at_once(self) -> None:
        self.listener.session = GatewaySession(session_id="s1", sequence=3)
        self.listener.handle(self.ws, {"op": OP_INVALID_SESSION, "d": False})
        self.assertIsNone(self.listener.session.session_id)

    def test_repeated_resume_failures_force_a_fresh_identify(self) -> None:
        """An unresumable session retried forever pins the bot offline."""
        self.listener.session = GatewaySession(session_id="s1", sequence=3)
        for _ in range(RESUME_FAILURE_THRESHOLD):
            self.listener.session.resume_url = "wss://x"
            self.listener.handle(self.ws, {"op": OP_INVALID_SESSION, "d": True})
        self.assertIsNone(self.listener.session.session_id)
        self.assertFalse(self.listener.session.can_resume())

    def test_a_reconnect_request_keeps_the_session(self) -> None:
        """Routine during Discord's own deploys; resuming is the point."""
        self.listener.session = GatewaySession(session_id="s1", sequence=9,
                                               resume_url="wss://resume")
        self.listener.handle(self.ws, {"op": OP_RECONNECT})
        self.assertTrue(self.listener.session.can_resume())


class BackoffTests(unittest.TestCase):
    def test_backoff_is_exponential_and_capped(self) -> None:
        listener = GatewayListener(bot_token="t", on_message=lambda _: True,
                                   min_backoff=2.0, max_backoff=30.0)
        self.assertEqual([listener.backoff_for(n) for n in range(5)], [2.0, 4.0, 8.0, 16.0, 30.0])
        self.assertEqual(listener.backoff_for(50), 30.0)

    def test_fatal_close_codes_are_named(self) -> None:
        """Bad token and disallowed intents are configuration, not weather."""
        self.assertIn(4004, FATAL_CLOSE_CODES, "authentication failed")
        self.assertIn(4014, FATAL_CLOSE_CODES, "disallowed intents")


class CloseCodeTests(unittest.TestCase):
    def test_the_socket_surfaces_the_peers_close_code(self) -> None:
        """A client that discards it retries a fatal error forever."""
        server, client = socket.socketpair()
        ws = WebSocket(client)
        payload = struct.pack("!H", 4004) + b"Authentication failed."
        frame = bytes([0x80 | 0x8, len(payload)]) + payload
        server.sendall(frame)
        list(ws.messages())
        self.assertEqual(ws.close_code, 4004)
        self.assertIn("Authentication", ws.close_reason)


if __name__ == "__main__":
    unittest.main()


class BridgeTests(unittest.TestCase):
    """The daemon's Discord bridge: deny by default, and never talk to itself."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from bothy.audit import AuditLog
        from bothy.daemon import DiscordBridge
        from bothy.wake import WakeStore

        self.dir = Path(tempfile.mkdtemp())
        self.store = WakeStore(self.dir)
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.bridge = DiscordBridge(
            bot_token="t", store=self.store, audit=self.audit,
            allow_from=["U_ALLOWED"], channels=["C_WATCHED"],
        )

    def message(self, **overrides: Any) -> dict[str, Any]:
        base = {"id": "m1", "channel_id": "C_WATCHED", "content": "do a thing",
                "author": {"id": "U_ALLOWED", "bot": False}}
        base.update(overrides)
        return base

    def test_an_allowed_speaker_in_a_watched_channel_queues_a_wake(self) -> None:
        self.assertTrue(self.bridge._store(self.message()))
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["route"], "discord")
        self.assertEqual(pending[0]["payload"]["prompt"], "do a thing")

    def test_its_own_messages_are_ignored(self) -> None:
        """A bot that answers itself is a loop with a bill attached."""
        self.bridge._store(self.message(author={"id": "U_BOT", "bot": True}))
        self.assertEqual(self.store.pending(), [])

    def test_a_stranger_is_declined(self) -> None:
        self.bridge._store(self.message(author={"id": "U_STRANGER", "bot": False}))
        self.assertEqual(self.store.pending(), [])
        refusals = [r for r in self.audit.records() if r["action"] == "ignored"]
        self.assertEqual(len(refusals), 1)

    def test_an_unwatched_channel_is_ignored(self) -> None:
        self.bridge._store(self.message(channel_id="C_OTHER"))
        self.assertEqual(self.store.pending(), [])

    def test_empty_content_points_at_the_privileged_intent(self) -> None:
        """The failure looks like being ignored, not like a missing permission."""
        self.bridge._store(self.message(content=""))
        records = [r for r in self.audit.records() if r["action"] == "empty"]
        self.assertIn("MESSAGE_CONTENT", records[0]["data"]["reason"])

    def test_the_lane_is_the_channel(self) -> None:
        self.bridge._store(self.message(id="m1"))
        self.bridge._store(self.message(id="m2"))
        subjects = {event["subject"] for event in self.store.pending()}
        self.assertEqual(len(subjects), 1, "two messages in one channel share a lane")
