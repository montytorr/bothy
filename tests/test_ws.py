"""The WebSocket client, checked against RFC 6455's own examples.

This is the only honest way to test a protocol implementation you wrote
yourself: a test built from the same misunderstanding as the code will agree
with it happily. Every vector below is quoted from the RFC, not derived from
bothy.ws.
"""

from __future__ import annotations

import socket
import struct
import unittest

from bothy.ws import OP_BINARY, OP_CLOSE, OP_PING, OP_TEXT, WebSocket, WebSocketError, accept_key, encode_frame

RFC_MASK = bytes([0x37, 0xFA, 0x21, 0x3D])


def server_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """A frame as a SERVER would send it: unmasked. Written independently."""
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


class RfcVectorTests(unittest.TestCase):
    def test_handshake_accept_key(self) -> None:
        """RFC 6455 section 4.1."""
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_masked_text_frame(self) -> None:
        """RFC 6455 section 5.7: a single-frame masked text message, "Hello"."""
        self.assertEqual(
            encode_frame(OP_TEXT, b"Hello", mask_key=RFC_MASK),
            bytes([0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58]),
        )

    def test_masked_ping_frame(self) -> None:
        """RFC 6455 section 5.7: a masked ping request, "Hello"."""
        self.assertEqual(
            encode_frame(OP_PING, b"Hello", mask_key=RFC_MASK),
            bytes([0x89, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58]),
        )

    def test_length_encodings(self) -> None:
        """A client always masks, so the length byte always carries 0x80."""
        for size, expected in [
            (125, bytes([0x80 | 125])),
            (126, bytes([0x80 | 126]) + struct.pack("!H", 126)),
            (65535, bytes([0x80 | 126]) + struct.pack("!H", 65535)),
            (65536, bytes([0x80 | 127]) + struct.pack("!Q", 65536)),
        ]:
            with self.subTest(size=size):
                frame = encode_frame(OP_BINARY, b"\x00" * size, mask_key=b"\x00\x00\x00\x00")
                self.assertEqual(frame[1:1 + len(expected)], expected)


class ProtocolRuleTests(unittest.TestCase):
    """The two rules implementations get wrong."""

    def test_a_control_frame_may_not_exceed_125_bytes(self) -> None:
        with self.assertRaises(WebSocketError):
            encode_frame(OP_PING, b"x" * 126)

    def test_a_control_frame_may_not_be_fragmented(self) -> None:
        with self.assertRaises(WebSocketError):
            encode_frame(OP_PING, b"x", fin=False)

    def test_every_frame_gets_a_fresh_mask(self) -> None:
        self.assertNotEqual(encode_frame(OP_TEXT, b"Hello"), encode_frame(OP_TEXT, b"Hello"))

    def test_a_mask_key_is_exactly_four_bytes(self) -> None:
        with self.assertRaises(WebSocketError):
            encode_frame(OP_TEXT, b"x", mask_key=b"ab")


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server, client = socket.socketpair()
        self.ws = WebSocket(client)

    def test_fragments_reassemble(self) -> None:
        self.server.sendall(server_frame(OP_TEXT, b"Hel", fin=False))
        self.server.sendall(server_frame(0x0, b"lo", fin=True))
        self.server.sendall(server_frame(OP_CLOSE, struct.pack("!H", 1000)))
        self.assertEqual(list(self.ws.messages()), ["Hello"])

    def test_a_ping_is_answered_and_not_yielded(self) -> None:
        self.server.sendall(server_frame(OP_PING, b"are you there"))
        self.server.sendall(server_frame(OP_TEXT, b"payload"))
        self.server.sendall(server_frame(OP_CLOSE, struct.pack("!H", 1000)))
        self.assertEqual(list(self.ws.messages()), ["payload"])
        self.assertTrue(self.server.recv(64), "a pong was written back")

    def test_a_polite_close_ends_iteration_without_raising(self) -> None:
        self.server.sendall(server_frame(OP_CLOSE, struct.pack("!H", 1000)))
        self.assertEqual(list(self.ws.messages()), [])
        self.assertTrue(self.ws.closed)

    def test_an_oversized_frame_is_refused(self) -> None:
        self.ws.max_message_bytes = 16
        self.server.sendall(server_frame(OP_TEXT, b"x" * 64))
        with self.assertRaises(WebSocketError):
            self.ws.read_frame()


if __name__ == "__main__":
    unittest.main()
