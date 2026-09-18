"""Discord over the gateway, on the WebSocket client we already own.

Slack's Socket Mode hands you a connection and gets out of the way. Discord's
gateway is a real protocol with state: an IDENTIFY carrying an intents bitfield,
a heartbeat on an interval the server chooses, a sequence number to track, and a
RESUME that replays what was missed after a drop.

Like Socket Mode this is an OUTBOUND connection, so it needs no public ingress
and no port — which is why it fits a sealed box at all.

FOUR THINGS THIS GETS RIGHT THAT ARE EASY TO GET WRONG:

  A MISSED HEARTBEAT ACK MEANS A ZOMBIE SOCKET. The connection looks fine and
  delivers nothing. Waiting on it is how a bot goes quiet for hours, so an
  un-acked heartbeat forces a reconnect rather than another heartbeat.

  RESUME CAN FAIL FOREVER. If a session is unresumable and we keep trying to
  resume it, the bot never comes back. After a few consecutive failures the
  session is thrown away and a fresh IDENTIFY is sent.

  SOME CLOSE CODES ARE FATAL. Bad token, disallowed intents and invalid shard
  are configuration problems; reconnecting forever on them produces a service
  that looks alive and does nothing.

  MESSAGE CONTENT IS A PRIVILEGED INTENT. Without it, messages arrive with an
  empty `content` and everything looks subtly broken rather than refused, so the
  intents actually requested are recorded and surfaced.

This module speaks the protocol and decides what is worth waking for. It does
not run agents and does not touch the queue: the daemon supplies that, the same
way it does for Slack.
"""

from __future__ import annotations

import dataclasses
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import clock
from .ws import WebSocket, WebSocketError

__all__ = [
    "Intents", "DiscordError", "DiscordFatal", "DiscordClient", "GatewayListener",
    "GatewaySession", "TEXT_LIMIT", "chunks", "message_subject",
]

API = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
TEXT_LIMIT = 1900          # below Discord's 2000, leaving room for decoration

# Opcodes, from the gateway documentation.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Close codes that no amount of reconnecting will fix.
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}
RESUME_FAILURE_THRESHOLD = 3


class Intents:
    """The intents bitfield, named rather than written as a magic number."""

    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1                 # privileged
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    DIRECT_MESSAGES = 1 << 12
    DIRECT_MESSAGE_REACTIONS = 1 << 13
    MESSAGE_CONTENT = 1 << 15              # privileged

    @classmethod
    def default(cls, *, message_content: bool = True, guild_members: bool = False) -> int:
        """What a harness actually needs, and nothing more.

        MESSAGE_CONTENT is privileged and must be enabled in the application's
        own settings. Without it messages arrive with an empty ``content`` and
        the bot appears to ignore everyone, which reads as a bug rather than a
        permission — so it is requested explicitly and reported.
        """
        bits = (cls.GUILDS | cls.GUILD_MESSAGES | cls.DIRECT_MESSAGES
                | cls.GUILD_MESSAGE_REACTIONS | cls.DIRECT_MESSAGE_REACTIONS)
        if message_content:
            bits |= cls.MESSAGE_CONTENT
        if guild_members:
            bits |= cls.GUILD_MEMBERS
        return bits

    @classmethod
    def describe(cls, bits: int) -> list[str]:
        names = []
        for name in ("GUILDS", "GUILD_MEMBERS", "GUILD_MESSAGES", "GUILD_MESSAGE_REACTIONS",
                     "DIRECT_MESSAGES", "DIRECT_MESSAGE_REACTIONS", "MESSAGE_CONTENT"):
            if bits & getattr(cls, name):
                names.append(name)
        return names


class DiscordError(RuntimeError):
    """Discord answered, and the answer was no."""


class DiscordFatal(DiscordError):
    """Not transient. Reconnecting will never fix this one."""


def chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split for Discord's message cap, on line boundaries where possible."""
    out: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            out.append(current)
            current = line
        else:
            current += line
    if current:
        out.append(current)
    return out or [""]


def message_subject(message: dict[str, Any], *, prefix: str = "discord-") -> str:
    """The lane a Discord message occupies.

    Keyed on the CONVERSATION — the thread if there is one, otherwise the
    channel — so a follow-up joins the run already working rather than starting
    a second agent on the same discussion.
    """
    channel = str(message.get("channel_id") or "unknown")
    return f"{prefix}{channel}"


@dataclasses.dataclass
class GatewaySession:
    """What has to survive a disconnection for a RESUME to be possible."""

    session_id: str | None = None
    resume_url: str | None = None
    sequence: int | None = None
    resume_failures: int = 0

    def can_resume(self) -> bool:
        return bool(self.session_id and self.sequence is not None)

    def forget(self) -> None:
        """Throw the session away so the next connection identifies fresh."""
        self.session_id = None
        self.resume_url = None
        self.sequence = None
        self.resume_failures = 0


def _call(method: str, path: str, token: str, payload: dict[str, Any] | None = None,
          *, timeout: float = 20.0) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        API + path, data=data, method=method,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/montytorr/bothy, 0.1)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise DiscordFatal(f"{method} {path}: HTTP {exc.code} — this will not fix itself") from exc
        raise DiscordError(f"{method} {path}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DiscordError(f"{method} {path}: {exc}") from exc


class DiscordClient:
    """Outbound Discord, over the REST API."""

    def __init__(self, bot_token: str, *, timeout: float = 20.0) -> None:
        self.bot_token = bot_token
        self.timeout = timeout

    def post(self, channel_id: str, text: str, *, reply_to: str | None = None) -> list[str]:
        sent: list[str] = []
        for part in chunks(text):
            payload: dict[str, Any] = {"content": part, "allowed_mentions": {"parse": []}}
            if reply_to and not sent:
                payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
            body = _call("POST", f"/channels/{channel_id}/messages", self.bot_token,
                         payload, timeout=self.timeout)
            sent.append(str(body.get("id", "")))
        return sent

    def react(self, channel_id: str, message_id: str, emoji: str = "\N{EYES}") -> bool:
        """Acknowledge a human. Discord has typing, but a reaction outlives it."""
        try:
            _call("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/"
                         f"{urllib.parse.quote(emoji)}/@me", self.bot_token, timeout=self.timeout)
            return True
        except DiscordError:
            return False

    def whoami(self) -> dict[str, Any]:
        return _call("GET", "/users/@me", self.bot_token, timeout=self.timeout)


class GatewayListener:
    """Holds the gateway connection and hands messages on.

    ``on_message`` receives a MESSAGE_CREATE payload and must return True once
    it is DURABLY STORED. Unlike Slack there is no per-message acknowledgement
    to withhold, so the sequence number is only advanced for a message that was
    stored — which means a RESUME after a crash replays it rather than skipping
    past it. That is the closest equivalent Discord offers to withholding an ack.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        on_message: Callable[[dict[str, Any]], bool],
        intents: int | None = None,
        on_error: Callable[[str, bool], None] | None = None,
        min_backoff: float = 2.0,
        max_backoff: float = 30.0,
    ) -> None:
        self.bot_token = bot_token
        self.on_message = on_message
        self.intents = Intents.default() if intents is None else intents
        self.on_error = on_error or (lambda message, fatal: None)
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff

        self.session = GatewaySession()
        self.connected = False
        self._stop = threading.Event()
        self._socket: WebSocket | None = None
        self._heartbeat_interval: float = 0.0
        self._awaiting_ack = False
        self._heartbeat: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()

    def backoff_for(self, attempt: int) -> float:
        """Exponential, capped, so a gateway outage does not become a flood."""
        return min(self.max_backoff, self.min_backoff * (2 ** max(0, attempt)))

    def run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                url = self.session.resume_url if self.session.can_resume() else GATEWAY_URL
                with WebSocket.connect(url or GATEWAY_URL, timeout=30.0) as socket_ws:
                    self._socket = socket_ws
                    self.connected = True
                    attempt = 0
                    self._serve(socket_ws)
                    if socket_ws.close_code in FATAL_CLOSE_CODES:
                        self.on_error(
                            f"gateway closed {socket_ws.close_code}: {socket_ws.close_reason} "
                            "— check the bot token and that the privileged intents are enabled",
                            True,
                        )
                        return
            except DiscordFatal as exc:
                self.on_error(str(exc), True)
                return
            except (WebSocketError, OSError, DiscordError) as exc:
                self.on_error(f"{exc}; reconnecting in {self.backoff_for(attempt):.0f}s", False)
            finally:
                self.connected = False
                self._stop_heartbeat()
                self._socket = None
            if self._stop.wait(timeout=self.backoff_for(attempt)):
                return
            attempt += 1

    # ---- the protocol --------------------------------------------------

    def _send(self, socket_ws: WebSocket, op: int, data: Any = None) -> None:
        socket_ws.send(json.dumps({"op": op, "d": data}))

    def identify_payload(self) -> dict[str, Any]:
        return {
            "token": self.bot_token,
            "intents": self.intents,
            "properties": {"os": "linux", "browser": "bothy", "device": "bothy"},
        }

    def resume_payload(self) -> dict[str, Any]:
        return {"token": self.bot_token, "session_id": self.session.session_id,
                "seq": self.session.sequence}

    def _start_heartbeat(self, socket_ws: WebSocket, interval_ms: float) -> None:
        self._heartbeat_interval = interval_ms / 1000.0
        self._awaiting_ack = False

        def beat() -> None:
            # The first beat is jittered, as the gateway asks, so a fleet
            # reconnecting together does not thunder.
            delay = self._heartbeat_interval * random.random()
            while not self._stop.is_set() and not socket_ws.closed:
                if self._stop.wait(timeout=delay):
                    return
                delay = self._heartbeat_interval
                if self._awaiting_ack:
                    # The socket is open and answering nothing. Waiting on it is
                    # how a bot goes quiet for hours.
                    self.on_error("heartbeat was not acknowledged; the socket is a zombie", False)
                    socket_ws.close()
                    return
                try:
                    self._awaiting_ack = True
                    self._send(socket_ws, OP_HEARTBEAT, self.session.sequence)
                except WebSocketError:
                    return

        self._heartbeat = threading.Thread(target=beat, name="discord-heartbeat", daemon=True)
        self._heartbeat.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat = None
        self._awaiting_ack = False

    def handle(self, socket_ws: WebSocket, message: dict[str, Any]) -> None:
        """One gateway frame. Separated out so the protocol is testable alone."""
        op = message.get("op")
        data = message.get("d")

        if op == OP_HELLO:
            self._start_heartbeat(socket_ws, float((data or {}).get("heartbeat_interval", 45000)))
            if self.session.can_resume():
                self._send(socket_ws, OP_RESUME, self.resume_payload())
            else:
                self._send(socket_ws, OP_IDENTIFY, self.identify_payload())
            return

        if op == OP_HEARTBEAT:
            # The gateway may ask for one out of band; answer immediately.
            self._send(socket_ws, OP_HEARTBEAT, self.session.sequence)
            return

        if op == OP_HEARTBEAT_ACK:
            self._awaiting_ack = False
            return

        if op == OP_RECONNECT:
            # Routine: Discord asks during deploys. Keep the session and resume.
            socket_ws.close()
            return

        if op == OP_INVALID_SESSION:
            resumable = bool(data)
            self.session.resume_failures += 1
            if not resumable or self.session.resume_failures >= RESUME_FAILURE_THRESHOLD:
                # An unresumable session that we keep trying to resume pins the
                # bot offline forever. Throw it away and identify fresh.
                self.on_error(
                    f"session invalid after {self.session.resume_failures} attempt(s); "
                    "starting a fresh session", False)
                self.session.forget()
            socket_ws.close()
            return

        if op == OP_DISPATCH:
            sequence = message.get("s")
            event = message.get("t")
            if event == "READY":
                self.session.session_id = str((data or {}).get("session_id") or "")
                self.session.resume_url = str((data or {}).get("resume_gateway_url") or "") or None
                self.session.resume_failures = 0
                if sequence is not None:
                    self.session.sequence = int(sequence)
                return
            if event == "RESUMED":
                self.session.resume_failures = 0
                if sequence is not None:
                    self.session.sequence = int(sequence)
                return
            if event == "MESSAGE_CREATE":
                stored = False
                try:
                    stored = bool(self.on_message(data or {}))
                except Exception as exc:  # noqa: BLE001 - a handler must not drop the connection
                    self.on_error(f"handler failed for a message: {exc}", False)
                if stored and sequence is not None:
                    # Advanced only for a message we kept. A crash before this
                    # means a RESUME replays it rather than skipping past it,
                    # which is the closest thing the gateway offers to
                    # withholding an acknowledgement.
                    self.session.sequence = int(sequence)
                return
            if sequence is not None:
                self.session.sequence = int(sequence)

    def _serve(self, socket_ws: WebSocket) -> None:
        for raw in socket_ws.messages():
            if self._stop.is_set():
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.handle(socket_ws, message)
