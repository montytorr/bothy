"""Slack over Socket Mode, which is the right shape for a closed machine.

Socket Mode is an OUTBOUND WebSocket to Slack. No public URL, no inbound port,
no TLS certificate to own, no request-signature verification — which means it
works unchanged on a client's machine that accepts no connections at all. That
is a much better fit for Bothy than the Events API, which would need a
publicly-reachable HTTPS endpoint and undo the one thing the deployment story
is built on.

THE ORDER OF OPERATIONS IS THE SAME RULE AS THE WEBHOOK RECEIVER, for the same
reason: durably store the envelope, THEN acknowledge it. Slack redelivers what
it has not seen acknowledged, and that is a feature — it is only a feature if
the thing we acknowledged actually survived.

Slack expects an acknowledgement within about three seconds, which is why the
ack carries nothing but the envelope id and why the work happens after it.

FAIL FAST ON THE ERRORS THAT RETRYING CANNOT FIX. ``invalid_auth``,
``token_revoked``, ``account_inactive`` and ``missing_scope`` are not transient:
retrying them forever produces a service that looks alive and does nothing,
which is worse than one that stops and says why. Everything else backs off and
tries again.

Slack has no bot typing indicator, so acknowledgement to a human is a reaction
on their message rather than a "typing…" that does not exist.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import clock
from .ws import WebSocket, WebSocketError

__all__ = ["SlackError", "SlackAuthError", "SlackClient", "SocketModeListener", "TEXT_LIMIT"]

API = "https://slack.com/api/"
TEXT_LIMIT = 7900          # below Slack's 8000, leaving room for decoration
FATAL = {"invalid_auth", "token_revoked", "account_inactive", "missing_scope",
         "not_authed", "invalid_client_id", "app_missing"}


class SlackError(RuntimeError):
    """Slack answered, and the answer was no."""


class SlackAuthError(SlackError):
    """Not transient. Retrying will never fix this one."""


def _call(method: str, token: str, payload: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    """One Slack Web API call. Raises SlackAuthError on anything unfixable."""
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        API + method, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "bothy/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SlackError(f"{method}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SlackError(f"{method}: {exc}") from exc
    if not body.get("ok"):
        error = str(body.get("error", "unknown"))
        if error in FATAL:
            raise SlackAuthError(f"{method}: {error} — this will not fix itself")
        raise SlackError(f"{method}: {error}")
    return body


def chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on line boundaries where possible; hard-split only when forced."""
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


@dataclasses.dataclass
class SlackClient:
    """Outbound Slack, over the Web API."""

    bot_token: str
    timeout: float = 20.0

    def post(self, channel: str, text: str, *, thread_ts: str | None = None) -> list[str]:
        """Send a message, chunked. Returns the timestamps of what was sent."""
        sent: list[str] = []
        for part in chunks(text):
            payload: dict[str, Any] = {"channel": channel, "text": part, "unfurl_links": False}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            body = _call("chat.postMessage", self.bot_token, payload, timeout=self.timeout)
            sent.append(str(body.get("ts", "")))
        return sent

    def react(self, channel: str, timestamp: str, emoji: str = "eyes") -> bool:
        """Acknowledge a human. Slack has no bot typing indicator, so this is it."""
        try:
            _call("reactions.add", self.bot_token,
                  {"channel": channel, "timestamp": timestamp, "name": emoji}, timeout=self.timeout)
            return True
        except SlackError:
            # Already-reacted and similar are not worth a failure; the point was
            # to show a human we heard them, and one way or another we tried.
            return False

    def whoami(self) -> dict[str, Any]:
        return _call("auth.test", self.bot_token, timeout=self.timeout)


class SocketModeListener:
    """Holds an outbound WebSocket to Slack and hands envelopes on.

    ``on_envelope`` must return True once the envelope is DURABLY STORED. It is
    only acknowledged if it does — an ack for something we then lost is exactly
    the bug the durable-before-ack rule exists to prevent, and Slack's
    redelivery is the safety net that makes refusing to ack the right move.
    """

    def __init__(
        self,
        *,
        app_token: str,
        on_envelope: Callable[[dict[str, Any]], bool],
        on_error: Callable[[str, bool], None] | None = None,
        min_backoff: float = 2.0,
        max_backoff: float = 30.0,
    ) -> None:
        self.app_token = app_token
        self.on_envelope = on_envelope
        self.on_error = on_error or (lambda message, fatal: None)
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._stop = threading.Event()
        self._socket: WebSocket | None = None
        self.connected = False

    def open_url(self) -> str:
        body = _call("apps.connections.open", self.app_token)
        url = body.get("url")
        if not url:
            raise SlackError("apps.connections.open returned no url")
        return str(url)

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()

    def run_forever(self) -> None:
        """Connect, serve, reconnect. Returns only when stopped or auth is dead."""
        backoff = self.min_backoff
        while not self._stop.is_set():
            try:
                url = self.open_url()
                with WebSocket.connect(url, timeout=30.0) as socket_ws:
                    self._socket = socket_ws
                    self.connected = True
                    backoff = self.min_backoff      # a good connection resets the ladder
                    self._serve(socket_ws)
            except SlackAuthError as exc:
                # Nothing about waiting makes a revoked token valid.
                self.connected = False
                self.on_error(str(exc), True)
                return
            except (SlackError, WebSocketError, OSError) as exc:
                self.connected = False
                self.on_error(f"{exc}; reconnecting in {backoff:.0f}s", False)
            finally:
                self.connected = False
                self._socket = None
            if self._stop.wait(timeout=backoff):
                return
            backoff = min(self.max_backoff, backoff * 2)

    def _serve(self, socket_ws: WebSocket) -> None:
        for raw in socket_ws.messages():
            if self._stop.is_set():
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = message.get("type")
            if kind == "hello":
                continue
            if kind == "disconnect":
                # Slack asks us to reconnect during deploys and refreshes. Not
                # an error, and not something to back off over.
                return
            envelope_id = message.get("envelope_id")
            if not envelope_id:
                continue
            stored = False
            try:
                stored = bool(self.on_envelope(message))
            except Exception as exc:  # noqa: BLE001
                self.on_error(f"handler failed for envelope {envelope_id}: {exc}", False)
            if stored:
                # Acknowledged only now, after it is safely on disk. If we did
                # not store it, staying silent makes Slack redeliver, which is
                # precisely what we want.
                socket_ws.send(json.dumps({"envelope_id": envelope_id}))


def envelope_subject(message: dict[str, Any], *, prefix: str = "slack-") -> str:
    """The lane a Slack message occupies.

    Keyed on the CONVERSATION — channel plus thread where there is one — so two
    messages in the same thread serialise instead of racing, while two different
    conversations proceed in parallel. Keying on the message id instead would
    let a follow-up start a second agent on the same discussion, which is the
    mistake the closest prior art makes with its webhooks.
    """
    payload = message.get("payload") or {}
    event = payload.get("event") or {}
    channel = event.get("channel") or payload.get("channel_id") or "unknown"
    thread = event.get("thread_ts") or event.get("ts") or ""
    return f"{prefix}{channel}" + (f"-{thread}" if thread else "")


def envelope_text(message: dict[str, Any]) -> str:
    """The human-written part of an envelope, or an empty string."""
    payload = message.get("payload") or {}
    event = payload.get("event") or {}
    return str(event.get("text") or payload.get("text") or "")
