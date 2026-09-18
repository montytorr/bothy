"""What a run is allowed to spend, reserved before it starts.

THE PROBLEM PARALLELISM CREATES. With one run at a time, "are we under the cap"
is a question you can answer immediately before starting and re-answer as you
go. With several runs in flight that is simply wrong: five runs each observe
"$40 of $50 used", all five pass, and the day ends at $90. Measuring after the
fact cannot bound a total that several writers are moving at once.

So admission takes a LEASE on budget. A run reserves its worst case before it
starts, reports actuals while it runs, and settles at the end — returning
whatever it did not use. Outstanding reservations count against the cap, so the
sixth run is refused while the first five are still deciding what they cost.

A crashed run does not get to hold its reservation forever. Reservations expire
on a TTL like any other lease, which is the same rule Bothy applies to claims
and to worker slots: every exclusive slot needs exactly one automatic path back
to empty. Money is just another exclusive slot.

TWO MODES, ONE LEDGER, because Codex authenticates two ways.

  api           the unit is dollars, and WE accumulate the total. Bothy prices
                each turn from the token counts the app-server reports.
  subscription  the unit is percent of the rate-limit window, and CODEX
                accumulates the total — we read usedPercent and never add it up
                ourselves. The ledger tracks only what is in flight and not yet
                reflected in that number.

That distinction is the whole reason ``observed_used`` exists separately from
``accrued``. Getting it backwards double-counts in one mode and under-counts in
the other.

PRICING NOTE, paid for by somebody else. A forensic post-mortem of a single
19-hour agent run found 58% of $19,302 was CACHE WRITES, not output — child
runs inherited a long cache TTL meant for a long-lived parent and paid roughly
2x write price for retention they never used. Cache writes are therefore priced
as their own line here, never folded into input.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from . import clock, ids

__all__ = [
    "Mode",
    "Pricing",
    "BudgetPolicy",
    "Reservation",
    "BudgetRefused",
    "BudgetLedger",
    "Usage",
    "PRICING",
]

Mode = Literal["api", "subscription"]


# --------------------------------------------------------------------------
# pricing


@dataclasses.dataclass(frozen=True)
class Pricing:
    """Dollars per million tokens, by kind. Cache writes are their own line."""

    input: float
    cached_input: float
    cache_write: float
    output: float

    def cost(self, usage: "Usage") -> float:
        million = 1_000_000.0
        return (
            usage.input_tokens * self.input
            + usage.cached_input_tokens * self.cached_input
            + usage.cache_write_tokens * self.cache_write
            + usage.output_tokens * self.output
        ) / million


# Indicative rates, deliberately overridable per deployment: a wrong price that
# is too HIGH refuses work early, which is the safe direction to be wrong in.
PRICING: dict[str, Pricing] = {
    "default": Pricing(input=1.25, cached_input=0.125, cache_write=1.5625, output=10.0),
}


@dataclasses.dataclass(frozen=True)
class Usage:
    """Token counts as the Codex app-server reports them.

    ``reasoning_output_tokens`` is a subset of ``output_tokens`` upstream, so it
    is recorded for visibility and deliberately not priced again.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @classmethod
    def from_app_server(cls, payload: dict[str, Any]) -> "Usage":
        """Read a thread/tokenUsage/updated total, tolerating absent fields."""
        block = payload or {}
        return cls(
            input_tokens=int(block.get("inputTokens") or block.get("input_tokens") or 0),
            cached_input_tokens=int(block.get("cachedInputTokens") or block.get("cached_input_tokens") or 0),
            cache_write_tokens=int(block.get("cacheWriteInputTokens") or block.get("cache_write_input_tokens") or 0),
            output_tokens=int(block.get("outputTokens") or block.get("output_tokens") or 0),
            reasoning_output_tokens=int(
                block.get("reasoningOutputTokens") or block.get("reasoning_output_tokens") or 0
            ),
        )

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------
# policy


@dataclasses.dataclass(frozen=True)
class BudgetPolicy:
    """The caps. Every default here has to be right on an unattended box.

    ``per_run`` is what a run reserves at admission when it does not ask for a
    specific amount: its worst case, not its expected case. Reserving the
    expected case is how you end up over the cap with everything "within
    budget".
    """

    mode: Mode = "subscription"
    # api mode, dollars
    per_run_usd: float = 2.0
    per_day_usd: float = 25.0
    model_pricing: str = "default"
    # subscription mode, percent of the rate-limit window
    per_run_percent: float = 2.0
    ceiling_percent: float = 85.0
    # shared
    reservation_ttl_seconds: float = 3600.0

    def unit(self) -> str:
        return "usd" if self.mode == "api" else "percent"

    def per_run(self) -> float:
        return self.per_run_usd if self.mode == "api" else self.per_run_percent

    def ceiling(self) -> float:
        return self.per_day_usd if self.mode == "api" else self.ceiling_percent

    def cap_name(self) -> str:
        return "per_day_usd" if self.mode == "api" else "ceiling_percent"


class BudgetRefused(RuntimeError):
    """Admission denied on budget. Carries what an operator needs to act.

    Never raised with a bare message: the refusal names the cap, the numbers
    that tripped it and, where it is knowable, when it stops being true.
    """

    def __init__(
        self,
        message: str,
        *,
        cap: str,
        limit: float,
        current: float,
        requested: float,
        unit: str,
        resets_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.cap = cap
        self.limit = limit
        self.current = current
        self.requested = requested
        self.unit = unit
        self.resets_at = resets_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": str(self),
            "cap": self.cap,
            "limit": self.limit,
            "current": self.current,
            "requested": self.requested,
            "unit": self.unit,
            "resets_at": self.resets_at,
        }


@dataclasses.dataclass
class Reservation:
    """Budget held for one in-flight run."""

    id: str
    run_id: str
    amount: float
    unit: str
    reserved_at: str
    expires_at: str
    actual: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reservation":
        return cls(**raw)

    def held(self) -> float:
        """What this reservation still occupies: the larger of promise and spend.

        A run that has already exceeded its reservation occupies what it has
        actually spent, not the smaller number it promised.
        """
        return max(self.amount, self.actual)


# --------------------------------------------------------------------------
# the ledger


class BudgetLedger:
    """Reservations and spend, durable across a crash.

    State is one small JSON file rewritten atomically, guarded by an advisory
    lock so the daemon and a ``bothy status`` running beside it cannot tear each
    other's writes. Not SQLite: this is a handful of rows that an operator
    should be able to read and, in an emergency, correct with an editor.
    """

    def __init__(self, path: str | os.PathLike[str], policy: BudgetPolicy) -> None:
        self.path = Path(path)
        self.policy = policy
        self._lock = threading.Lock()

    # ---- persistence ---------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"day": clock.utcnow().strftime("%Y-%m-%d"), "accrued": 0.0, "reservations": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt ledger must not mean unlimited spending. Start from zero
            # accrued but keep refusing nothing we cannot account for: the
            # caller sees the reset in the audit log and can investigate.
            return {"day": clock.utcnow().strftime("%Y-%m-%d"), "accrued": 0.0, "reservations": {}, "recovered": True}
        if not isinstance(state, dict):
            return {"day": clock.utcnow().strftime("%Y-%m-%d"), "accrued": 0.0, "reservations": {}, "recovered": True}
        state.setdefault("reservations", {})
        state.setdefault("accrued", 0.0)
        state.setdefault("day", clock.utcnow().strftime("%Y-%m-%d"))
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name, suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(state, handle, sort_keys=True, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise

    def _roll_day(self, state: dict[str, Any]) -> dict[str, Any]:
        """A new UTC day resets accrued spend. Reservations survive the boundary."""
        today = clock.utcnow().strftime("%Y-%m-%d")
        if state.get("day") != today:
            state["day"] = today
            state["accrued"] = 0.0
        return state

    # ---- the gate ------------------------------------------------------

    def outstanding(self, state: dict[str, Any] | None = None) -> float:
        """What live reservations currently occupy."""
        state = state if state is not None else self._load()
        return sum(Reservation.from_dict(raw).held() for raw in state["reservations"].values())

    def reserve(
        self,
        *,
        run_id: str,
        amount: float | None = None,
        observed_used: float | None = None,
        resets_at: str | None = None,
    ) -> Reservation:
        """Hold budget for a run, or refuse and say why.

        ``observed_used`` is required in subscription mode and ignored in api
        mode: in subscription mode the authority on what has been consumed is
        Codex's own rate-limit report, and accumulating our own total beside it
        would double-count. In api mode we are the only one counting.
        """
        want = self.policy.per_run() if amount is None else float(amount)
        with self._lock:
            state = self._roll_day(self._load())
            self._drop_expired(state)

            if self.policy.mode == "subscription":
                if observed_used is None:
                    raise ValueError("subscription mode needs observed_used from account/rateLimits/read")
                base = float(observed_used)
            else:
                base = float(state["accrued"])

            held = self.outstanding(state)
            projected = base + held + want
            limit = self.policy.ceiling()
            if projected > limit:
                raise BudgetRefused(
                    f"would reach {projected:.2f} {self.policy.unit()} against a {limit:.2f} ceiling "
                    f"({base:.2f} used, {held:.2f} already reserved, {want:.2f} requested)",
                    cap=self.policy.cap_name(),
                    limit=limit,
                    current=base + held,
                    requested=want,
                    unit=self.policy.unit(),
                    resets_at=resets_at,
                )

            now = clock.utcnow()
            reservation = Reservation(
                id=ids.reservation_id(),
                run_id=run_id,
                amount=want,
                unit=self.policy.unit(),
                reserved_at=clock.iso(now),
                expires_at=clock.iso(now + _dt.timedelta(seconds=self.policy.reservation_ttl_seconds)),
            )
            state["reservations"][reservation.id] = reservation.as_dict()
            self._save(state)
            return reservation

    def report(self, reservation_id: str, actual: float) -> Reservation | None:
        """Update what a live run has actually spent so far.

        Raising the actual above the reserved amount is allowed and is exactly
        what ``held()`` exists for — the run now occupies the larger number, so
        an overrun immediately squeezes admission for everyone else rather than
        being discovered at settle time.
        """
        with self._lock:
            state = self._load()
            raw = state["reservations"].get(reservation_id)
            if raw is None:
                return None
            reservation = Reservation.from_dict(raw)
            reservation.actual = float(actual)
            state["reservations"][reservation_id] = reservation.as_dict()
            self._save(state)
            return reservation

    def settle(self, reservation_id: str, actual: float | None = None) -> float:
        """Release a reservation and accrue what it really cost.

        Returns the settled amount. In subscription mode nothing is accrued —
        Codex's own counter already moved — but the reservation is still
        released so the slot frees immediately rather than at TTL.
        """
        with self._lock:
            state = self._roll_day(self._load())
            raw = state["reservations"].pop(reservation_id, None)
            if raw is None:
                self._save(state)
                return 0.0
            reservation = Reservation.from_dict(raw)
            spent = float(reservation.actual if actual is None else actual)
            if self.policy.mode == "api":
                state["accrued"] = float(state["accrued"]) + spent
            self._save(state)
            return spent

    def settle_run(self, run_id: str) -> list[Reservation]:
        """Release every reservation belonging to a run, by run id.

        Used when a worker is reaped after a crash: the process is gone, so its
        budget must go with it immediately rather than sitting out its TTL. An
        hour of phantom reservation on a small cap is an hour of refusing real
        work for money nobody is spending.

        The spend is accrued as held() — the larger of promised and last
        reported — because the run died mid-flight and the money it had already
        spent is not coming back.
        """
        with self._lock:
            state = self._roll_day(self._load())
            settled: list[Reservation] = []
            for key, raw in list(state["reservations"].items()):
                reservation = Reservation.from_dict(raw)
                if reservation.run_id != run_id:
                    continue
                state["reservations"].pop(key, None)
                if self.policy.mode == "api":
                    state["accrued"] = float(state["accrued"]) + reservation.held()
                settled.append(reservation)
            if settled:
                self._save(state)
            return settled

    def _drop_expired(self, state: dict[str, Any]) -> list[Reservation]:
        """Reap reservations whose run died without settling.

        Their spend is accrued anyway in api mode: the money was probably spent,
        and the safe direction to be wrong in is "assume it was".
        """
        expired: list[Reservation] = []
        for key, raw in list(state["reservations"].items()):
            reservation = Reservation.from_dict(raw)
            if clock.age_seconds(reservation.expires_at) > 0:
                state["reservations"].pop(key, None)
                if self.policy.mode == "api":
                    state["accrued"] = float(state["accrued"]) + reservation.held()
                expired.append(reservation)
        return expired

    def expire_stale(self) -> list[Reservation]:
        """Public sweep, for the daemon's janitor to call on a timer."""
        with self._lock:
            state = self._roll_day(self._load())
            expired = self._drop_expired(state)
            if expired:
                self._save(state)
            return expired

    def snapshot(self, observed_used: float | None = None) -> dict[str, Any]:
        """What ``bothy status`` prints. Read-only, no side effects."""
        state = self._roll_day(self._load())
        held = self.outstanding(state)
        base = float(state["accrued"]) if self.policy.mode == "api" else float(observed_used or 0.0)
        limit = self.policy.ceiling()
        return {
            "mode": self.policy.mode,
            "unit": self.policy.unit(),
            "used": round(base, 4),
            "reserved": round(held, 4),
            "total": round(base + held, 4),
            "limit": limit,
            "headroom": round(limit - base - held, 4),
            "in_flight": len(state["reservations"]),
            "day": state["day"],
        }
