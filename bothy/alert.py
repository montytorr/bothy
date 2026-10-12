"""Telling a human something needs them, without becoming noise.

Outbound only, over plain HTTPS webhooks. That is a deliberate limit for P0:
a webhook URL needs no bot, no gateway connection, no WebSocket and no inbound
port, which keeps Bothy standard-library-only and keeps the client's box closed.
Listening in chat is a later phase and a different shape.

The hard part of alerting is not delivery, it is SILENCE ON SUCCESS AND ON
REPETITION. Two disciplines, both learned from systems that got it wrong first:

  A scheduled report that speaks every day teaches everyone to ignore it. So
  nothing is sent when there is nothing to say.

  A failure that recurs every minute is one incident, not a thousand pings. So
  alerts are keyed on what actually went wrong — the job plus a normalised
  fingerprint of the error — and a key that has already been reported stays
  quiet until either the error text CHANGES or a cooldown expires. Changing
  text means something new happened; identical text means the same thing is
  still true, which the first alert already said.

Delivery failures are logged, never raised. An alerting path that can take down
the thing it watches is worse than no alerting path.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from . import clock
from .notice import Notice, discord_payload, slack_payload, text_of

__all__ = [
    "AlertSink",
    "StderrSink",
    "DiscordWebhookSink",
    "SlackWebhookSink",
    "CompositeSink",
    "Deduplicator",
    "Severity",
    "Notice",
]

Severity = str  # "info" | "warning" | "critical"

_DISCORD_LIMIT = 1900   # below the 2000 hard cap, leaving room for decoration
_SLACK_LIMIT = 7900     # below 8000


class AlertSink(Protocol):
    def alert(self, message: str) -> bool | None:
        """Deliver a message. False means it did not arrive; None means no opinion."""
        ...


# --------------------------------------------------------------------------
# suppression


_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b(run|wak|res)_\d{8}T\d{6}Z_[0-9a-f]{8}\b"), "<id>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})\b"), "<ts>"),
    (re.compile(r"\bpid \d+\b", re.I), "pid <n>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]


def fingerprint(job: str, error: str) -> str:
    """A stable key for "the same thing going wrong again".

    Volatile parts — ids, timestamps, pids, any bare number — are replaced
    before hashing, so a failure that differs only by run id is recognised as
    the same incident. Only the first line is considered: stack tails vary in
    ways that would defeat the whole purpose.
    """
    head = error.strip().splitlines()[0] if error.strip() else ""
    for pattern, replacement in _VOLATILE:
        head = pattern.sub(replacement, head)
    digest = hashlib.sha256(f"{job}\x00{head}".encode("utf-8")).hexdigest()[:16]
    return f"{job}:{digest}"


class Deduplicator:
    """Decides whether an incident is worth saying out loud again."""

    def __init__(self, *, cooldown_seconds: float = 3600.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_send(self, key: str) -> bool:
        with self._lock:
            last = self._seen.get(key)
            now = clock.monotonic()
            if last is not None and (now - last) < self.cooldown_seconds:
                return False
            self._seen[key] = now
            return True

    def clear(self, key: str) -> None:
        """Forget a key, so the next occurrence speaks again.

        Called when a run for that subject finally succeeds: the condition is
        over, and if it comes back it is news.
        """
        with self._lock:
            self._seen.pop(key, None)


# --------------------------------------------------------------------------
# sinks


def _send_json(method: str, url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> tuple[bool, str, Any]:
    """One JSON request. Returns (ok, detail, decoded body or None)."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - url is operator-configured
        url, data=body, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "bothy/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            try:
                decoded = json.loads(raw) if raw else None
            except ValueError:
                decoded = None
            return 200 <= response.status < 300, str(response.status), decoded
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", None
    except Exception as exc:  # noqa: BLE001 - alerting must never raise into the caller
        return False, str(exc), None


def _post_json(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> tuple[bool, str]:
    ok, detail, _ = _send_json("POST", url, payload, timeout=timeout)
    return ok, detail


def _with_query(url: str, **params: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key not in params] + list(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _message_url(url: str, message_id: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=f"{parts.path.rstrip('/')}/messages/{message_id}"))


def _chunks(text: str, limit: int) -> list[str]:
    """Split on line boundaries where possible, hard-split only when forced."""
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


class StderrSink:
    """Always available, never the only sink in production."""

    name = "stderr"

    def notify(self, notice: Notice, refs: dict[str, str]) -> tuple[bool | None, dict[str, str]]:
        return self.alert(text_of(notice)), {}

    def alert(self, message: str) -> bool | None:
        print(f"[bothy][alert] {message}", file=sys.stderr, flush=True)
        return None     # a terminal nobody may be watching is not a delivery


class DiscordWebhookSink:
    """Post to a Discord webhook URL. No bot, no gateway, no inbound port."""

    name = "discord"

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.last_error: str | None = None

    def alert(self, message: str) -> bool:
        for part in _chunks(message, _DISCORD_LIMIT):
            ok, detail = _post_json(self.url, {"content": part}, timeout=self.timeout)
            if not ok:
                self.last_error = detail
                print(f"[bothy][alert-failed][discord] {detail}", file=sys.stderr, flush=True)
                return False
        return True


    def notify(self, notice: Notice, refs: dict[str, str]) -> tuple[bool, dict[str, str]]:
        """Post an embed, remembering its message id; a resolution edits that message.

        A webhook may edit its own messages, and ``?wait=true`` is what makes the
        post answer with the message to edit. If the original is gone (404) or
        was never remembered, the resolution is posted fresh.
        """
        payload = discord_payload(notice)
        message_id = refs.get(self.name)
        if notice.resolved and message_id:
            ok, detail, _ = _send_json("PATCH", _message_url(self.url, message_id), payload, timeout=self.timeout)
            if ok:
                return True, {self.name: message_id}
            if detail != "HTTP 404":
                return self._failed(detail), {}
        ok, detail, body = _send_json("POST", _with_query(self.url, wait="true"), payload, timeout=self.timeout)
        if not ok:
            return self._failed(detail), {}
        posted = str(body["id"]) if isinstance(body, dict) and body.get("id") else None
        return True, {self.name: posted} if posted else {}

    def _failed(self, detail: str) -> bool:
        self.last_error = detail
        print(f"[bothy][alert-failed][discord] {detail}", file=sys.stderr, flush=True)
        return False


class SlackWebhookSink:
    """Post to a Slack incoming webhook. Inbound Slack is a later phase."""

    name = "slack"

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.last_error: str | None = None

    def alert(self, message: str) -> bool:
        for part in _chunks(message, _SLACK_LIMIT):
            ok, detail = _post_json(self.url, {"text": part}, timeout=self.timeout)
            if not ok:
                self.last_error = detail
                print(f"[bothy][alert-failed][slack] {detail}", file=sys.stderr, flush=True)
                return False
        return True


    def notify(self, notice: Notice, refs: dict[str, str]) -> tuple[bool, dict[str, str]]:
        """Post blocks. An incoming webhook cannot edit what it posted (that needs a
        bot token), so a resolution is a short follow-up message in the muted style."""
        ok, detail = _post_json(self.url, slack_payload(notice), timeout=self.timeout)
        if not ok:
            self.last_error = detail
            print(f"[bothy][alert-failed][slack] {detail}", file=sys.stderr, flush=True)
        return ok, {}


class CompositeSink:
    """Fan out to several sinks. One failing sink must not silence the others."""

    def __init__(self, *sinks: AlertSink) -> None:
        self.sinks = list(sinks)

    def alert(self, message: str) -> bool | None:
        """True if any sink delivered, False if one tried and none did, else None."""
        results: list[bool | None] = []
        for sink in self.sinks:
            try:
                results.append(sink.alert(message))
            except Exception as exc:  # noqa: BLE001
                print(f"[bothy][alert-failed] {type(sink).__name__}: {exc}", file=sys.stderr, flush=True)
                results.append(False)
        if any(result is True for result in results):
            return True
        return False if any(result is False for result in results) else None


    def notify(self, notice: Notice, refs: dict[str, str]) -> tuple[bool | None, dict[str, str]]:
        """Like ``alert`` for a notice. Returns the result and each sink's message ref."""
        results: list[bool | None] = []
        kept: dict[str, str] = {}
        for sink in self.sinks:
            try:
                if hasattr(sink, "notify"):
                    result, made = sink.notify(notice, refs)
                    kept.update(made)
                else:
                    result = sink.alert(text_of(notice))
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                print(f"[bothy][alert-failed] {type(sink).__name__}: {exc}", file=sys.stderr, flush=True)
                results.append(False)
        if any(result is True for result in results):
            return True, kept
        return (False if any(result is False for result in results) else None), kept


class Alerter:
    """What the rest of Bothy calls. Formats, suppresses, then delivers."""

    def __init__(self, sink: AlertSink, *, host: str = "bothy", dedupe: Deduplicator | None = None) -> None:
        self.sink = sink
        self.host = host
        self.dedupe = dedupe or Deduplicator()

    def incident(
        self,
        *,
        job: str,
        error: str,
        severity: Severity = "warning",
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
        reaction: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        command: str | None = None,
        project: str | None = None,
    ) -> bool:
        """Report something going wrong. Returns whether it was actually sent.

        ``reaction`` is a free-text runbook line attached to the thing that
        broke, so the alert arrives carrying its own first response instead of
        sending the reader to look one up; ``command`` is the thing to run, drawn
        in a code block. ``title`` and ``summary`` are the plain words for a
        person (the title leads with ``project``, or "Bothy"); without them the
        job name and the first line of the error stand in.
        """
        key = fingerprint(job, error)
        if not self.dedupe.should_send(key):
            return False
        raw = error.strip()
        first = raw.splitlines()[0] if raw else job
        notice = Notice(
            title=f"{project or 'Bothy'} · {title or job}",
            description=summary or first,
            severity=severity, command=command, advice=reaction, run=run_id, site=self.host,
            at=clock.iso(), details=raw[:1200] if raw and raw != summary else None,
            facts=tuple((str(name), str(value)) for name, value in (detail or {}).items()),
        )
        self.notify(notice)
        return True

    def resolved(self, *, job: str, error: str) -> None:
        """Forget an incident, so its return is news again."""
        self.dedupe.clear(fingerprint(job, error))

    def say(self, message: str) -> None:
        """Unconditional, unsuppressed. For things a human asked for."""
        self.sink.alert(message)

    def tell(self, notice: Notice) -> None:
        """``say`` for a notice."""
        self.notify(notice)

    def deliver(self, message: str) -> bool:
        """Unconditional, and says whether it arrived.

        For a caller that keeps the thing until somebody has heard it. ``say``
        cannot serve that caller: it swallows a failed delivery, so the caller
        would record "told" for a message that went nowhere.
        """
        return self.sink.alert(message) is not False

    def notify(self, notice: Notice, refs: dict[str, str] | None = None) -> tuple[bool, dict[str, str]]:
        """Deliver a notice and say whether it arrived, with the refs to edit it later.

        Unconditional, like ``deliver``: the caller keeps the thing until somebody
        has heard it, and keeps ``refs`` so a resolution can edit the original.
        """
        if hasattr(self.sink, "notify"):
            result, kept = self.sink.notify(notice, refs or {})
            return result is not False, kept
        return self.sink.alert(text_of(notice)) is not False, {}
