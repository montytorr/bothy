"""Time, always with an offset attached.

Three separate incidents in the systems Bothy learns from were timezone bugs.
The worst used ``time.mktime`` on a UTC log timestamp, which shifted it by the
server's local offset, so a Discord reply twenty seconds old was read as idle
and a restart aborted it mid-turn.

So: one module, every timestamp aware, and no naive datetime ever created or
accepted. ``parse`` treats a missing offset as an error rather than guessing,
because guessing is how the incident happened.

Wall-clock time and elapsed time are different questions and use different
sources. ``now()`` answers "when did this happen" and can jump when the clock
is corrected. ``monotonic()`` answers "how long has this been running" and
cannot. A deadline computed from wall clock will fire early or late across an
NTP step, so every timeout in Bothy is measured with ``monotonic``.
"""

from __future__ import annotations

import datetime as _dt
import time as _time

__all__ = ["now", "utcnow", "monotonic", "iso", "parse", "from_epoch", "age_seconds"]


def now() -> _dt.datetime:
    """The current instant in the system's local zone, offset attached."""
    return _dt.datetime.now(_dt.timezone.utc).astimezone()


def utcnow() -> _dt.datetime:
    """The current instant in UTC. What gets written to disk."""
    return _dt.datetime.now(_dt.timezone.utc)


def monotonic() -> float:
    """Seconds from an arbitrary origin, immune to clock adjustment."""
    return _time.monotonic()


def iso(moment: _dt.datetime | None = None) -> str:
    """Render an aware datetime as ISO-8601 with an explicit offset."""
    moment = moment if moment is not None else utcnow()
    if moment.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime; attach a timezone")
    return moment.isoformat()


def parse(text: str) -> _dt.datetime:
    """Parse an ISO-8601 timestamp that carries an offset.

    A trailing ``Z`` is accepted and means UTC. A timestamp with no offset is
    rejected: there is no correct default, and the plausible ones are how the
    bug above happened.
    """
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    moment = _dt.datetime.fromisoformat(candidate)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp carries no timezone offset: {text!r}")
    return moment


def age_seconds(moment: _dt.datetime | str, *, reference: _dt.datetime | None = None) -> float:
    """How long ago an aware instant was, in seconds. Never negative-by-surprise.

    A future timestamp returns a negative number rather than zero, because a
    clock-skewed peer is worth seeing rather than silently flattening.
    """
    instant = parse(moment) if isinstance(moment, str) else moment
    if instant.tzinfo is None:
        raise ValueError("refusing to age a naive datetime; attach a timezone")
    reference = reference if reference is not None else utcnow()
    return (reference - instant).total_seconds()


def from_epoch(seconds: float | int | None) -> _dt.datetime | None:
    """Turn a Unix timestamp into an aware UTC datetime, or None.

    Codex reports rate-limit resets as an epoch integer. Passing that straight
    through into an operator-facing message produces "resets at 1789903902",
    which nobody can act on — and a budget refusal nobody can act on defeats the
    point of carrying the reset time at all. Convert at the boundary.
    """
    if seconds is None:
        return None
    return _dt.datetime.fromtimestamp(float(seconds), tz=_dt.timezone.utc)
