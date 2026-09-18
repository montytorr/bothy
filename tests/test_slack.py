"""Slack: conversation lanes, and never acknowledging what was not stored."""

from __future__ import annotations

import json
import socket
import struct
import unittest

from bothy.slack import FATAL, TEXT_LIMIT, SocketModeListener, chunks, envelope_subject, envelope_text
from bothy.ws import OP_CLOSE, OP_TEXT, WebSocket


def server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    header.append(len(payload))
    return bytes(header) + payload


def decode_client_frames(raw: bytes) -> list[dict]:
    """Decode masked client frames, written independently of bothy.ws."""
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
        body = bytes(byte ^ key[position % 4] for position, byte in enumerate(raw[index:index + length]))
        index += length
        if opcode == OP_TEXT:
            out.append(json.loads(body))
    return out


class LaneTests(unittest.TestCase):
    def test_a_thread_shares_a_lane_with_its_parent(self) -> None:
        """A follow-up must join the run already working, not start a second."""
        first = {"payload": {"event": {"channel": "C1", "ts": "1.1"}}}
        reply = {"payload": {"event": {"channel": "C1", "thread_ts": "1.1", "ts": "2.2"}}}
        self.assertEqual(envelope_subject(first), envelope_subject(reply))

    def test_different_channels_are_different_lanes(self) -> None:
        one = {"payload": {"event": {"channel": "C1", "ts": "1.1"}}}
        two = {"payload": {"event": {"channel": "C9", "ts": "1.1"}}}
        self.assertNotEqual(envelope_subject(one), envelope_subject(two))

    def test_text_is_extracted(self) -> None:
        self.assertEqual(envelope_text({"payload": {"event": {"text": "hello"}}}), "hello")


class ChunkTests(unittest.TestCase):
    def test_chunks_respect_slacks_limit(self) -> None:
        big = "\n".join(f"line {index} " + "x" * 200 for index in range(200))
        parts = chunks(big)
        self.assertTrue(all(len(part) <= TEXT_LIMIT for part in parts))
        self.assertEqual("".join(parts), big, "nothing is lost")


class AckTests(unittest.TestCase):
    """Slack redelivers what it has not seen acknowledged. That only helps if
    what we acknowledged actually survived."""

    def _drive(self, stored: bool) -> list[dict]:
        server, client = socket.socketpair()
        socket_ws = WebSocket(client)
        listener = SocketModeListener(app_token="xapp-test", on_envelope=lambda _: stored)
        server.sendall(server_frame(OP_TEXT, json.dumps({"type": "hello"}).encode()))
        server.sendall(server_frame(OP_TEXT, json.dumps({
            "envelope_id": "env-1", "type": "events_api",
            "payload": {"event": {"channel": "C1", "ts": "1.1", "text": "do a thing"}}}).encode()))
        server.sendall(server_frame(OP_CLOSE, struct.pack("!H", 1000)))
        listener._serve(socket_ws)
        server.settimeout(0.4)
        received = b""
        try:
            while True:
                chunk = server.recv(4096)
                if not chunk:
                    break
                received += chunk
        except (TimeoutError, OSError):
            pass
        return decode_client_frames(received)

    def test_a_stored_envelope_is_acknowledged(self) -> None:
        self.assertIn({"envelope_id": "env-1"}, self._drive(stored=True))

    def test_an_unstored_envelope_is_not_acknowledged(self) -> None:
        """Staying silent invites the redelivery, which is exactly what we want."""
        self.assertNotIn({"envelope_id": "env-1"}, self._drive(stored=False))


class FatalErrorTests(unittest.TestCase):
    def test_unfixable_errors_are_named(self) -> None:
        """Retrying these forever produces a service that looks alive and does nothing."""
        for error in ("invalid_auth", "token_revoked", "missing_scope", "account_inactive"):
            self.assertIn(error, FATAL)


if __name__ == "__main__":
    unittest.main()
