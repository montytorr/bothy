"""One run, from the door to the record.

This is where the separate pieces become a harness. The order is the design,
and each step exists because skipping it broke something somewhere:

    mint a run id            one key, so every layer can be joined afterwards
    ask the gate             lane, slot and budget, atomically, before any cost
    claim the task           so the ledger shows who is on it while it happens
    spawn into a group       so the whole tree can be killed, always
    supervise the turn       usage metered live, wall clock enforced
    settle and release       budget, lane and slot returned even on the bad path
    record the outcome       audit, Cairn, and a human if they are needed

Two invariants hold no matter how the run ends, and both live in the ``finally``
block rather than in the happy path, because the happy path is not where
harnesses lose things:

  THE GROUP IS ALWAYS KILLED. Not the process — the group. Orphaned workers are
  how a reference host accumulated thirteen live app-servers, and how one
  runaway agent left 196 children and $1,193 of already-spent work behind.

  THE RESERVATION IS ALWAYS SETTLED. Budget is an exclusive slot like any other,
  and every exclusive slot needs exactly one automatic path back to empty.

A run that fails is still a run that completed its bookkeeping.
"""

from __future__ import annotations

import dataclasses
import shutil
import traceback
from pathlib import Path
from typing import Any

from . import clock, codex, ids
from .alert import Alerter
from .audit import AuditError, AuditLog
from .budget import PRICING, Usage
from .config import Config
from .pool import Admission, Refused
from .proc import ProcessRegistry
from .tracker import CairnTracker, CairnUnavailable

__all__ = ["Runner", "RunResult"]


@dataclasses.dataclass
class RunResult:
    """What happened, in the terms Bothy reports and bills."""

    run_id: str
    subject: str
    status: str                       # completed | failed | interrupted | refused
    started_at: str
    finished_at: str | None = None
    messages: list[str] = dataclasses.field(default_factory=list)
    cost: float = 0.0
    unit: str = "usd"
    usage: dict[str, Any] = dataclasses.field(default_factory=dict)
    refusal: dict[str, Any] | None = None
    error: str | None = None
    cairn_ref: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Runner:
    """Executes admitted work, and is the only thing that starts a worker."""

    def __init__(
        self,
        config: Config,
        *,
        admission: Admission,
        audit: AuditLog,
        registry: ProcessRegistry,
        tracker: CairnTracker | None = None,
        alerter: Alerter | None = None,
    ) -> None:
        self.config = config
        self.admission = admission
        self.audit = audit
        self.registry = registry
        self.tracker = tracker
        self.alerter = alerter
        self._last_used_percent: float | None = None
        self._last_resets_at: str | None = None

    # ---- budget position -----------------------------------------------

    def rate_limit_position(self, *, refresh: bool = False) -> tuple[float | None, str | None]:
        """Where the subscription window stands, cached between runs.

        Refreshed from every run's own notifications for free; probed only when
        nothing is known yet, because a probe costs a process spawn. Unknown
        stays None and is never rounded down to zero — treating "I could not
        find out" as "nothing used" would admit everything at exactly the moment
        the gate matters.
        """
        if refresh or self._last_used_percent is None:
            self.config.ensure_dirs()
            probe_home = self.config.homes_dir / "probe"
            self._seed_home(probe_home)
            limits = codex.probe_rate_limits(
                codex_home=probe_home, binary=self.config.codex_binary
            )
            seen = codex.used_percent(limits)
            if seen is not None:
                self._last_used_percent = seen
                self._last_resets_at = codex.resets_at(limits)
        return self._last_used_percent, self._last_resets_at

    def _observe(self, outcome: codex.TurnOutcome) -> None:
        seen = codex.used_percent(outcome.rate_limits or {})
        if seen is not None:
            self._last_used_percent = seen
            self._last_resets_at = codex.resets_at(outcome.rate_limits or {})

    # ---- audit helper ---------------------------------------------------

    def _record(self, **fields: Any) -> None:
        """Append to the audit log, and degrade loudly if we cannot.

        An unauditable harness on a client site should get quieter, not carry on
        — but a failed audit write must not take down a run that is already in
        flight, so this reports and returns rather than raising.
        """
        try:
            self.audit.append(**fields)
        except AuditError as exc:
            if self.alerter is not None:
                self.alerter.incident(
                    job="audit", error=str(exc), severity="critical",
                    reaction="Bothy cannot record what it is doing; investigate the disk before trusting the log.",
                )

    def _seed_home(self, home: Path) -> None:
        """Give an isolated worker home the credentials it needs.

        Per-run homes exist so concurrent workers do not share Codex's SQLite.
        The cost of that isolation is that a fresh home has no auth.json, and a
        worker without one fails with a bare 401 from the API — which is exactly
        how the first end-to-end run of this harness failed, and it looks like a
        model problem rather than a missing file.

        config.toml is deliberately NOT copied by default. A worker should be
        shaped by Bothy's own settings, not by whatever model, MCP servers or
        hooks the host account happens to have configured; inheriting them makes
        a client deployment behave differently from the machine it was tested on.
        """
        home.mkdir(parents=True, exist_ok=True)
        source = self.config.codex_credentials_dir
        names = ["auth.json"] + (["config.toml"] if self.config.codex_inherit_config else [])
        for name in names:
            candidate = source / name
            if candidate.exists():
                shutil.copy2(candidate, home / name)

    # ---- the run --------------------------------------------------------

    def run(
        self,
        *,
        subject: str,
        prompt: str,
        cairn_ref: str | None = None,
        wall_clock_seconds: float | None = None,
        wake_id: str | None = None,
    ) -> RunResult:
        run_id = ids.run_id()
        started = clock.iso()
        result = RunResult(run_id=run_id, subject=subject, status="failed", started_at=started, cairn_ref=cairn_ref)
        result.unit = self.config.budget.unit()

        observed, resets = (None, None)
        if self.config.budget.mode == "subscription":
            observed, resets = self.rate_limit_position()
            if observed is None:
                result.status = "refused"
                result.refusal = {
                    "gate": "budget_unknown",
                    "reason": "could not read the subscription window position, so admission is not safe",
                }
                self._record(kind="run", action="refused", status="refused", run_id=run_id,
                             subject=subject, data=result.refusal)
                return result

        # --- the gate ---
        try:
            admitted = self.admission.admit(
                run_id=run_id, subject=subject, observed_used=observed, resets_at=resets
            )
        except Refused as refusal:
            result.status = "refused"
            result.refusal = refusal.as_dict()
            self._record(kind="run", action="refused", status="refused", run_id=run_id,
                         subject=subject, data=result.refusal)
            if refusal.gate == "budget" and self.alerter is not None:
                self.alerter.incident(
                    job="budget", error=str(refusal), severity="warning", run_id=run_id,
                    detail={"subject": subject, **{k: v for k, v in refusal.detail.items() if k != "reason"}},
                    reaction="Raise the cap, wait for the window to reset, or switch auth mode.",
                )
            return result

        self._record(kind="run", action="admitted", run_id=run_id, subject=subject,
                     data={"reservation": admitted.reservation.id, "amount": admitted.reservation.amount,
                           "unit": admitted.reservation.unit, "wake_id": wake_id})

        # --- claim the task, so the ledger shows the work while it happens ---
        if cairn_ref and self.tracker is not None:
            try:
                if not self.tracker.claim(cairn_ref):
                    self._record(kind="task", action="claim_declined", status="refused",
                                 run_id=run_id, subject=cairn_ref,
                                 data={"note": "held by another agent"})
            except CairnUnavailable as exc:
                # Memory being unreachable is worth knowing about, but it is not
                # a reason to refuse to do the work.
                self._record(kind="task", action="claim_failed", status="failed",
                             run_id=run_id, subject=cairn_ref, data={"error": str(exc)})

        home = self.config.homes_dir / run_id
        self._seed_home(home)
        client = codex.CodexClient(
            run_id=run_id,
            codex_home=home,
            binary=self.config.codex_binary,
            on_event=lambda method, params: None,
        )
        spent = 0.0
        pricing = PRICING.get(self.config.budget.model_pricing, PRICING["default"])

        def meter(total: dict[str, Any]) -> None:
            """Push live cost into the ledger while the turn is still running."""
            nonlocal spent
            if self.config.budget.mode == "api":
                spent = pricing.cost(Usage.from_app_server(total))
                self.admission.report(run_id, spent)

        try:
            client.start()
            self.registry.register(run_id, client.popen, ["codex", "app-server"])
            client.handshake()
            thread_id = client.start_thread(
                cwd=self.config.workspace,
                sandbox=self.config.sandbox,
                approval_policy=self.config.approval_policy,
            )
            self._record(kind="run", action="started", run_id=run_id, subject=subject,
                         data={"thread": thread_id, "pgid": client.pgid, "sandbox": self.config.sandbox})

            outcome = client.run_turn(
                thread_id=thread_id,
                text=prompt,
                wall_clock_seconds=wall_clock_seconds or self.config.wall_clock_seconds,
                on_usage=meter,
            )
            self._observe(outcome)
            result.status = outcome.status
            result.messages = outcome.messages
            result.usage = outcome.usage
            result.error = outcome.error
            if self.config.budget.mode == "api":
                spent = pricing.cost(Usage.from_app_server(outcome.usage))
            result.cost = spent

        except Exception as exc:  # noqa: BLE001 - every failure still gets bookkeeping
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self._record(kind="run", action="errored", status="failed", run_id=run_id, subject=subject,
                         data={"error": result.error, "trace": traceback.format_exc()[-2000:],
                               "stderr": client.stderr_tail[-5:]})

        finally:
            # Invariant one: the group always dies.
            disposal = client.close(grace_seconds=10)
            self.registry.unregister(run_id)
            # Invariant two: the reservation is always settled.
            settled = self.admission.release(run_id, ok=(result.status == "completed"), actual=spent)
            result.finished_at = clock.iso()
            self._record(
                kind="run", action="finished",
                status="ok" if result.status == "completed" else result.status,
                run_id=run_id, subject=subject,
                data={"outcome": result.status, "cost": round(settled, 5), "unit": result.unit,
                      "disposal": disposal, "usage": result.usage, "error": result.error},
            )
            # Per-run homes are disposable; keeping them would grow without bound,
            # and Codex rollouts are the single biggest disk risk on a small box.
            shutil.rmtree(home, ignore_errors=True)

        # --- tell memory, and tell a human if they are needed ---
        if cairn_ref and self.tracker is not None:
            detail = "\n".join(result.messages)[:2000] or (result.error or "no output")
            try:
                self.tracker.record_run(cairn_ref, run_id=run_id, outcome=result.status, detail=detail)
                self.tracker.release(cairn_ref)
            except CairnUnavailable as exc:
                self._record(kind="task", action="record_failed", status="failed",
                             run_id=run_id, subject=cairn_ref, data={"error": str(exc)})

        if self.alerter is not None:
            job = f"run:{subject}"
            if result.status == "completed":
                self.alerter.resolved(job=job, error=result.error or "")
            else:
                self.alerter.incident(
                    job=job, error=result.error or f"run ended {result.status}",
                    severity="critical" if result.status == "failed" else "warning",
                    run_id=run_id,
                    detail={"subject": subject, "cost": f"{result.cost:.4f} {result.unit}"},
                    reaction=f"bothy audit --run {run_id}",
                )
        return result
