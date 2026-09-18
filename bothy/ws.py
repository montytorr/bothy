"""A minimal WebSocket client, because the alternative was a dependency.

Slack's Socket Mode and Discord's gateway both need a WebSocket, and the Python
standard library does not have one. Bothy's whole pitch at a client site is
"copy the directory and run it" — no virtualenv, no package index, nothing to
resolve on a machine that may not reach one. Trading that for a library felt
like the wrong bargain when a client is a few hundred lines of well-specified
framing.

So this implements exactly the client half of RFC 6455 and nothing else. It is
verified against the RFC's OWN example frames (section 5.7), which is the only
honest way to test a protocol implementation you wrote yourself: a test built
from the same misunderstanding as the code will agree with it happily.

WHAT IT DOES NOT DO, deliberately: no server role, no extensions, no
permessage-deflate, no continuation across more than memory allows. Those are
real parts of the spec and none of them are needed to hold a Socket Mode
connection open.

Two rules the spec is strict about and implementations get wrong:
  a client MUST mask every frame it sends, with a fresh random key
  a control frame MUST be <= 125 bytes and MUST NOT be fragmented
Both are enforced here rather than assumed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import ssl
import struct
from typing import Iterator, NamedTuple
from urllib.parse import urlparse

__all__ = ["WebSocket", "WebSocketError", "Frame", "OP_TEXT", "OP_BINARY", "OP_CLOSE", "OP_PING", "OP_PONG"]

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_CONTROL = {OP_CLOSE, OP_PING, OP_PONG}
MAX_CONTROL_PAYLOAD = 125
DEFAULT_MAX_MESSAGE = 8 * 1024 * 1024


class WebSocketError(RuntimeError):
    """The connection failed or the peer broke the protocol."""


class Frame(NamedTuple):
    fin: bool
    opcode: int
    payload: bytes


def accept_key(key: str) -> str:
    """The value a server must return for a given Sec-WebSocket-Key."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(opcode: int, payload: bytes, *, fin: bool = True, mask_key: bytes | None = None) -> bytes:
    """Encode one client frame. Always masked, because a client must be.

    ``mask_key`` exists so the RFC's example frames can be reproduced exactly in
    tests; in real use it is left None and a fresh random key is generated per
    frame, which is what the masking is for.
    """
    if opcode in _CONTROL:
        if len(payload) > MAX_CONTROL_PAYLOAD:
            raise WebSocketError(f"control frame of {len(payload)} bytes exceeds the 125-byte limit")
        if not fin:
            raise WebSocketError("control frames may not be fragmented")
    key = mask_key if mask_key is not None else secrets.token_bytes(4)
    if len(key) != 4:
        raise WebSocketError("a mask key is exactly four bytes")

    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    header += key
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


class WebSocket:
    """One client connection. Blocking, single-threaded, no magic."""

    def __init__(self, sock: socket.socket, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE) -> None:
        self._sock = sock
        self._buffer = bytearray()
        self.max_message_bytes = max_message_bytes
        self.closed = False

    # ---- connecting ----------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE,
    ) -> "WebSocket":
        """Open a connection and complete the upgrade handshake."""
        parsed = urlparse(url)
        secure = parsed.scheme == "wss"
        if parsed.scheme not in {"ws", "wss"}:
            raise WebSocketError(f"not a websocket url: {url!r}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if secure else 80)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {host}" + (f":{port}" if parsed.port else ""),
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: bothy/0.1",
        ]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

        socket_ws = cls(raw, max_message_bytes=max_message_bytes)
        response = socket_ws._read_until(b"\r\n\r\n")
        head, _, rest = response.partition(b"\r\n\r\n")
        socket_ws._buffer = bytearray(rest)
        text = head.decode("latin-1")
        status = text.split("\r\n", 1)[0]
        if " 101" not in status:
            raise WebSocketError(f"upgrade refused: {status}")
        returned = ""
        for line in text.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                returned = value.strip()
        if returned != accept_key(key):
            # Proves the peer actually spoke WebSocket rather than a proxy
            # cheerfully returning 101 for something else entirely.
            raise WebSocketError("server did not return a valid Sec-WebSocket-Accept")
        return socket_ws

    # ---- reading -------------------------------------------------------

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buffer += chunk
        index = self._buffer.index(marker) + len(marker)
        out = bytes(self._buffer[:index])
        del self._buffer[:index]
        return out

    def _read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed mid-frame")
            self._buffer += chunk
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    def read_frame(self) -> Frame:
        """Read one frame. Blocks until a whole frame has arrived."""
        first, second = self._read_exactly(2)
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketError("reserved bits set but no extension was negotiated")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exactly(8))[0]
        if length > self.max_message_bytes:
            raise WebSocketError(f"frame of {length} bytes exceeds the {self.max_message_bytes}-byte limit")
        if opcode in _CONTROL and (length > MAX_CONTROL_PAYLOAD or not fin):
            raise WebSocketError("peer sent an oversized or fragmented control frame")
        key = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(length)
        if masked:
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        return Frame(fin, opcode, payload)

    def messages(self) -> Iterator[str]:
        """Yield complete text messages, handling control frames transparently.

        Fragmentation is reassembled, pings are answered immediately, and a
        close frame ends the iteration rather than raising — a peer closing
        politely is not an error.
        """
        pending = bytearray()
        pending_op: int | None = None
        while not self.closed:
            try:
                frame = self.read_frame()
            except WebSocketError:
                self.closed = True
                return
            if frame.opcode == OP_PING:
                self.send_frame(OP_PONG, frame.payload)
                continue
            if frame.opcode == OP_PONG:
                continue
            if frame.opcode == OP_CLOSE:
                self.closed = True
                try:
                    self.send_frame(OP_CLOSE, frame.payload[:2])
                except WebSocketError:
                    pass
                return
            if frame.opcode == OP_CONT:
                if pending_op is None:
                    raise WebSocketError("continuation frame with nothing to continue")
                pending += frame.payload
            else:
                pending = bytearray(frame.payload)
                pending_op = frame.opcode
            if len(pending) > self.max_message_bytes:
                raise WebSocketError("reassembled message exceeds the size limit")
            if frame.fin:
                if pending_op == OP_TEXT:
                    yield pending.decode("utf-8", "replace")
                pending = bytearray()
                pending_op = None

    # ---- writing -------------------------------------------------------

    def send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketError("connection is closed")
        try:
            self._sock.sendall(encode_frame(opcode, payload))
        except OSError as exc:
            self.closed = True
            raise WebSocketError(f"send failed: {exc}") from exc

    def send(self, text: str) -> None:
        self.send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        self.send_frame(OP_PING, payload)

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            self.send_frame(OP_CLOSE, struct.pack("!H", code))
        except WebSocketError:
            pass
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
