"""Who is allowed to start, and who has to wait.

Three independent questions have to agree before a worker starts, and every
"no" has to be legible afterwards:

  IS THE SUBJECT FREE?   Never two runs on the same thing at once. A subject is
                         whatever the work is about — a Cairn ref, a contract
                         id, a repo path. This is the rule the closest prior art
                         skips: its webhook session key includes the delivery
                         id, so two webhooks about one pull request start two
                         independent agents that then fight over the same files.

  IS THERE A SLOT?       A bounded pool, so load is shed at the door rather than
                         by the kernel's OOM killer several minutes later.

  IS THERE BUDGET?       Reserved, not measured. See budget.py for why that
                         distinction is the whole ballgame once N > 1.

REFUSALS ARE EVENTS, NOT SILENCE. "It did not start, and here is which gate
said no" is the question an operator actually asks, so every refusal carries a
machine-readable reason and enough numbers to act on.

THE POOL HEALS ITSELF. Its size is recomputed from the recent FAILURE RATIO —
not latency, not queue depth — on the model of a message broker that has run
this in production for years: comfortably under the threshold, grow by one;
around it, hold; over it, shrink; badly over it, drop to a single run; still
failing, stop admitting and let one probe through per interval. It never stops
dead and it climbs back without anyone being paged, which is the behaviour you
want at 3am on a machine you cannot reach.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Literal

from . import clock
from .budget import BudgetLedger, BudgetRefused, Reservation

__all__ = ["PoolPolicy", "PoolState", "Refused", "Admission", "Admitted"]

PoolState = Literal["healthy", "degraded", "probing"]


class Refused(RuntimeError):
    """Admission denied. Always names the gate that said no."""

    def __init__(self, reason: str, *, gate: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.gate = gate
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"reason": str(self), "gate": self.gate, **self.detail}


@dataclasses.dataclass(frozen=True)
class PoolPolicy:
    """Concurrency limits. Defaults have to be right on an unattended box.

    ``max_workers`` defaults to 2 rather than 1 deliberately: a pool that only
    ever runs one job is untested concurrency dressed up as a feature, and the
    bugs it hides all surface the first time a client turns it up.
    """

    max_workers: int = 2
    min_workers: int = 1
    # self-healing, recomputed on this cadence from the recent window
    recompute_seconds: float = 30.0
    window_seconds: float = 900.0
    recent_samples: int = 20
    grow_below_ratio: float = 0.01
    shrink_above_ratio: float = 0.05
    collapse_above_ratio: float = 0.50
    min_samples: int = 4


@dataclasses.dataclass
class Admitted:
    """A granted admission. Hand it back to ``release`` when the run ends."""

    run_id: str
    subject: str
    reservation: Reservation
    admitted_at: str


class _Outcomes:
    """Recent run outcomes, bounded by both age and count.

    The ratio is taken over the last ``recent_samples`` outcomes, not over the
    whole time window, and that choice was forced by watching the first version
    behave badly: with a time window alone, a burst of failures pinned the pool
    at one worker for the entire window even after everything since had
    succeeded. Recovery has to be paced by what happened LATELY, or "degraded"
    becomes indistinguishable from "hung" for fifteen minutes at a time.

    The time window still applies, so a pool that goes quiet after a bad patch
    forgets it rather than carrying the failures into next week.
    """

    def __init__(self, window_seconds: float, recent_samples: int) -> None:
        self.window_seconds = window_seconds
        self.recent_samples = max(1, recent_samples)
        self._samples: list[tuple[float, bool]] = []

    def record(self, ok: bool) -> None:
        self._samples.append((clock.monotonic(), ok))
        self._trim()

    def _trim(self) -> None:
        cutoff = clock.monotonic() - self.window_seconds
        fresh = [sample for sample in self._samples if sample[0] >= cutoff]
        # Keep a little history beyond the ratio window so the count an operator
        # sees reflects real traffic, but never grow without bound.
        self._samples = fresh[-(self.recent_samples * 5) :]

    def ratio(self) -> tuple[float, int]:
        """Failure ratio over the most recent outcomes, and how many there were."""
        self._trim()
        recent = self._samples[-self.recent_samples :]
        if not recent:
            return 0.0, 0
        failures = sum(1 for _, ok in recent if not ok)
        return failures / len(recent), len(recent)


class Admission:
    """The single door every run goes through."""

    def __init__(self, policy: PoolPolicy, ledger: BudgetLedger) -> None:
        self.policy = policy
        self.ledger = ledger
        self._lock = threading.RLock()
        self._lanes: dict[str, str] = {}          # subject -> run_id
        self._live: dict[str, Admitted] = {}      # run_id -> admission
        self._outcomes = _Outcomes(policy.window_seconds, policy.recent_samples)
        self._effective = policy.max_workers
        self._state: PoolState = "healthy"
        self._last_recompute = clock.monotonic()
        self._probe_open = True

    # ---- health --------------------------------------------------------

    def _recompute(self) -> None:
        """Adjust the effective pool size from the recent failure ratio."""
        if clock.monotonic() - self._last_recompute < self.policy.recompute_seconds:
            return
        self._last_recompute = clock.monotonic()
        ratio, samples = self._outcomes.ratio()
        if samples < self.policy.min_samples:
            return  # too little evidence to move on

        if ratio > self.policy.collapse_above_ratio:
            self._effective = 1
            self._state = "probing"
            self._probe_open = True
        elif ratio > self.policy.shrink_above_ratio:
            self._effective = max(self.policy.min_workers, self._effective - 1)
            self._state = "degraded"
        elif ratio < self.policy.grow_below_ratio:
            self._effective = min(self.policy.max_workers, self._effective + 1)
            self._state = "healthy" if self._effective >= self.policy.max_workers else "degraded"
        # between grow and shrink thresholds: hold, deliberately

    def state(self) -> PoolState:
        with self._lock:
            return self._state

    def effective_size(self) -> int:
        with self._lock:
            return self._effective

    # ---- the gate ------------------------------------------------------

    def admit(
        self,
        *,
        run_id: str,
        subject: str,
        amount: float | None = None,
        observed_used: float | None = None,
        resets_at: str | None = None,
    ) -> Admitted:
        """Grant a run its lane, its slot and its budget, or raise Refused.

        The order matters: the cheap local checks run before the budget
        reservation, so a run refused for a busy lane never takes a reservation
        it would immediately have to give back.
        """
        with self._lock:
            self._recompute()

            if self._state == "probing" and self._live:
                raise Refused(
                    "pool is probing after repeated failures and one run is already in flight",
                    gate="pool_probing",
                    detail={"state": self._state, "in_flight": len(self._live)},
                )

            if subject in self._lanes:
                raise Refused(
                    f"another run is already working {subject}",
                    gate="lane_busy",
                    detail={"subject": subject, "held_by": self._lanes[subject]},
                )

            if len(self._live) >= self._effective:
                raise Refused(
                    f"all {self._effective} worker slots are busy",
                    gate="pool_full",
                    detail={
                        "in_flight": len(self._live),
                        "effective_size": self._effective,
                        "max_workers": self.policy.max_workers,
                        "state": self._state,
                    },
                )

            try:
                reservation = self.ledger.reserve(
                    run_id=run_id, amount=amount, observed_used=observed_used, resets_at=resets_at
                )
            except BudgetRefused as exc:
                raise Refused(str(exc), gate="budget", detail=exc.as_dict()) from exc

            admitted = Admitted(
                run_id=run_id, subject=subject, reservation=reservation, admitted_at=clock.iso()
            )
            self._lanes[subject] = run_id
            self._live[run_id] = admitted
            return admitted

    def release(self, run_id: str, *, ok: bool, actual: float | None = None) -> float:
        """End a run: free its lane and slot, settle its budget, record the outcome.

        Safe to call twice — a supervisor that crashes between settling and
        releasing will call it again on recovery, and a release that finds
        nothing simply returns zero.
        """
        with self._lock:
            admitted = self._live.pop(run_id, None)
            if admitted is None:
                return 0.0
            self._lanes.pop(admitted.subject, None)
            self._outcomes.record(ok)
            if self._state == "probing" and ok:
                # A successful probe reopens the door; size climbs back on the
                # next recompute rather than all at once.
                self._state = "degraded"
            return self.ledger.settle(admitted.reservation.id, actual)

    def report(self, run_id: str, actual: float) -> None:
        """Push a live run's actual spend into the ledger."""
        with self._lock:
            admitted = self._live.get(run_id)
            if admitted is not None:
                self.ledger.report(admitted.reservation.id, actual)

    def snapshot(self, observed_used: float | None = None) -> dict[str, Any]:
        """Everything ``bothy status`` needs about concurrency."""
        with self._lock:
            ratio, samples = self._outcomes.ratio()
            return {
                "state": self._state,
                "in_flight": len(self._live),
                "effective_size": self._effective,
                "max_workers": self.policy.max_workers,
                "failure_ratio": round(ratio, 3),
                "samples": samples,
                "lanes": dict(self._lanes),
                "budget": self.ledger.snapshot(observed_used=observed_used),
            }
