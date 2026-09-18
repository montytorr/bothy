"""Identifiers, minted in one place so every layer can be joined later.

The systems Bothy learns from can each tell you what happened, but not in one
place: the pane binding, the agent transcript, the trace log and the task
ledger use four different keys, and no join exists. Reconstructing one run
means four queries and a guess.

So Bothy mints ONE id per run at admission and stamps it into every layer it
touches — the audit log, the Cairn note, the Codex thread metadata, the alert.
"Show me everything about run X" has to be one grep, or nobody will ask it.

Ids are sortable by time and readable aloud. The time part is UTC and compact;
the random part is short because these are scoped to one host's lifetime, not
to the internet.
"""

from __future__ import annotations

import secrets

from . import clock

__all__ = ["run_id", "wake_id", "reservation_id", "is_run_id"]

_RUN = "run"
_WAKE = "wak"
_RESERVATION = "res"


def _mint(prefix: str) -> str:
    stamp = clock.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{secrets.token_hex(4)}"


def run_id() -> str:
    """Identify one supervised worker run, start to finish."""
    return _mint(_RUN)


def wake_id() -> str:
    """Identify one inbound wake, from receipt through to whatever it causes."""
    return _mint(_WAKE)


def reservation_id() -> str:
    """Identify one budget reservation held against an in-flight run."""
    return _mint(_RESERVATION)


def is_run_id(candidate: str) -> bool:
    """Cheap shape check, for validating something handed to us by an operator."""
    parts = candidate.split("_")
    return len(parts) == 3 and parts[0] == _RUN and len(parts[1]) == 16 and len(parts[2]) == 8
