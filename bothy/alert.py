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
import urllib.request
from typing import Any, Protocol

from . import clock

__all__ = [
    "AlertSink",
    "StderrSink",
    "DiscordWebhookSink",
    "SlackWebhookSink",
    "CompositeSink",
    "Deduplicator",
    "Severity",
]

Severity = str  # "info" | "warning" | "critical"

_DISCORD_LIMIT = 1900   # below the 2000 hard cap, leaving room for decoration
_SLACK_LIMIT = 7900     # below 8000


class AlertSink(Protocol):
    def alert(self, message: str) -> None: ...


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


def _post_json(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> tuple[bool, str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - url is operator-configured
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "bothy/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 300, str(response.status)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - alerting must never raise into the caller
        return False, str(exc)


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

    def alert(self, message: str) -> None:
        print(f"[bothy][alert] {message}", file=sys.stderr, flush=True)


class DiscordWebhookSink:
    """Post to a Discord webhook URL. No bot, no gateway, no inbound port."""

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.last_error: str | None = None

    def alert(self, message: str) -> None:
        for part in _chunks(message, _DISCORD_LIMIT):
            ok, detail = _post_json(self.url, {"content": part}, timeout=self.timeout)
            if not ok:
                self.last_error = detail
                print(f"[bothy][alert-failed][discord] {detail}", file=sys.stderr, flush=True)
                return


class SlackWebhookSink:
    """Post to a Slack incoming webhook. Inbound Slack is a later phase."""

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.last_error: str | None = None

    def alert(self, message: str) -> None:
        for part in _chunks(message, _SLACK_LIMIT):
            ok, detail = _post_json(self.url, {"text": part}, timeout=self.timeout)
            if not ok:
                self.last_error = detail
                print(f"[bothy][alert-failed][slack] {detail}", file=sys.stderr, flush=True)
                return


class CompositeSink:
    """Fan out to several sinks. One failing sink must not silence the others."""

    def __init__(self, *sinks: AlertSink) -> None:
        self.sinks = list(sinks)

    def alert(self, message: str) -> None:
        for sink in self.sinks:
            try:
                sink.alert(message)
            except Exception as exc:  # noqa: BLE001
                print(f"[bothy][alert-failed] {type(sink).__name__}: {exc}", file=sys.stderr, flush=True)


class Alerter:
    """What the rest of Bothy calls. Formats, suppresses, then delivers."""

    ICONS = {"info": "•", "warning": "!", "critical": "!!"}

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
    ) -> bool:
        """Report something going wrong. Returns whether it was actually sent.

        ``reaction`` is a free-text runbook line attached to the thing that
        broke, so the alert arrives carrying its own first response instead of
        sending the reader to look one up.
        """
        key = fingerprint(job, error)
        if not self.dedupe.should_send(key):
            return False
        lines = [
            f"{self.ICONS.get(severity, '•')} **{severity.upper()}** `{self.host}` — {job}",
            f"```{error.strip()[:1200]}```",
        ]
        if run_id:
            lines.append(f"run `{run_id}`")
        for name, value in (detail or {}).items():
            lines.append(f"· {name}: `{value}`")
        if reaction:
            lines.append(f"→ {reaction}")
        lines.append(f"_{clock.iso()}_")
        self.sink.alert("\n".join(lines))
        return True

    def resolved(self, *, job: str, error: str) -> None:
        """Forget an incident, so its return is news again."""
        self.dedupe.clear(fingerprint(job, error))

    def say(self, message: str) -> None:
        """Unconditional, unsuppressed. For things a human asked for."""
        self.sink.alert(message)
