"""Waiting is bounded: a wake refused for capacity is retried, but not forever."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bothy import clock
from bothy.audit import AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.config import Config
from bothy.daemon import Daemon
from bothy.pool import Admission, PoolPolicy
from bothy.proc import ProcessRegistry
from bothy.runner import RunResult
from bothy.wake import Route, WakeStore


class FakeRunner:
    """A runner that answers however the test needs, without spawning anything."""

    def __init__(self, behaviour) -> None:  # noqa: ANN001
        self.behaviour = behaviour
        self.calls: list[str] = []
        self.alerter = None

    def run(self, *, subject: str, prompt: str, **kwargs: Any) -> RunResult:
        self.calls.append(subject)
        return self.behaviour(subject)


def refused(subject: str, gate: str = "lane_busy") -> RunResult:
    return RunResult(run_id="run_x", subject=subject, status="refused",
                     started_at=clock.iso(),
                     refusal={"gate": gate, "reason": f"another run is working {subject}"})


def completed(subject: str) -> RunResult:
    return RunResult(run_id="run_ok", subject=subject, status="completed",
                     started_at=clock.iso(), messages=["done"])


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(home=Path(tempfile.mkdtemp()))
        self.config.ensure_dirs()
        self.audit = AuditLog(self.config.audit_path)
        self.store = WakeStore(self.config.wake_dir)

    def daemon(self, behaviour, **kwargs: Any) -> Daemon:  # noqa: ANN001
        runner = FakeRunner(behaviour)
        admission = Admission(
            PoolPolicy(),
            BudgetLedger(self.config.budget_path, BudgetPolicy(mode="api")),
        )
        alerts: list[str] = []

        class Sink:
            def incident(self, **kw: Any) -> bool:
                alerts.append(kw.get("error", ""))
                return True

            def resolved(self, **kw: Any) -> None:
                pass

            def say(self, message: str) -> None:
                pass

        sink = Sink()
        runner.alerter = sink
        daemon = Daemon(self.config, runner=runner, admission=admission, audit=self.audit,
                        registry=ProcessRegistry(self.config.registry_path),
                        alerter=sink, routes=[Route(path="/x", secret="s")], **kwargs)
        daemon.alerts = alerts          # type: ignore[attr-defined]
        daemon.fake_runner = runner     # type: ignore[attr-defined]
        return daemon

    def enqueue(self, wake_id: str, subject: str, **extra: Any) -> None:
        self.store.append({"id": wake_id, "received_at": clock.iso(), "route": "/x",
                           "subject": subject, "payload": {"prompt": "go"},
                           "processed": False, **extra})

    def test_a_capacity_refusal_leaves_the_wake_pending(self) -> None:
        self.enqueue("w1", "alpha")
        self.daemon(lambda s: refused(s)).drain()
        pending = self.store.pending()
        self.assertEqual(len(pending), 1, "not discarded because we were briefly busy")
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertEqual(pending[0]["last_gate"], "lane_busy")

    def test_a_busy_lane_is_attempted_once_per_pass_not_once_per_wake(self) -> None:
        """Ten wakes for one busy subject must not pay for ten admissions."""
        for index in range(5):
            self.enqueue(f"w{index}", "alpha")
        daemon = self.daemon(lambda s: refused(s))
        daemon.drain()
        self.assertEqual(len(daemon.fake_runner.calls), 1)  # type: ignore[attr-defined]
        self.assertEqual(len(self.store.pending()), 5, "all five are still queued")

    def test_a_different_lane_is_not_blocked_by_a_busy_one(self) -> None:
        self.enqueue("w1", "alpha")
        self.enqueue("w2", "beta")
        seen: list[str] = []

        def behaviour(subject: str) -> RunResult:
            seen.append(subject)
            return refused(subject) if subject == "alpha" else completed(subject)

        self.daemon(behaviour).drain()
        self.assertEqual(seen, ["alpha", "beta"], "beta was not head-of-line blocked")
        self.assertEqual([e["id"] for e in self.store.pending()], ["w1"])

    def test_it_runs_once_the_lane_frees(self) -> None:
        self.enqueue("w1", "alpha")
        self.daemon(lambda s: refused(s)).drain()
        self.daemon(lambda s: completed(s)).drain()
        self.assertEqual(self.store.pending(), [])

    def test_a_permanently_stuck_lane_is_abandoned_not_retried_forever(self) -> None:
        """Every path that requeues work must advance a counter that gives up."""
        self.enqueue("w1", "alpha")
        daemon = self.daemon(lambda s: refused(s), max_wake_attempts=3)
        for _ in range(5):
            daemon.drain()
        self.assertEqual(self.store.pending(), [])
        abandoned = [e for e in self.store.all_events() if e.get("outcome") == "abandoned"]
        self.assertEqual(len(abandoned), 1)
        self.assertIn("refused", abandoned[0]["abandoned_reason"])
        self.assertTrue(daemon.alerts, "a human is told rather than it vanishing")  # type: ignore[attr-defined]

    def test_a_wake_that_waited_past_usefulness_is_dropped(self) -> None:
        """Acting on a six-hour-old webhook can be worse than not acting."""
        old = clock.iso(clock.utcnow() - dt.timedelta(hours=9))
        self.enqueue("w1", "alpha", waiting_since=old)
        daemon = self.daemon(lambda s: completed(s), max_wake_age_seconds=6 * 3600)
        daemon.drain()
        self.assertEqual(daemon.fake_runner.calls, [], "never run late")  # type: ignore[attr-defined]
        abandoned = [e for e in self.store.all_events() if e.get("outcome") == "abandoned"]
        self.assertIn("past the", abandoned[0]["abandoned_reason"])

    def test_abandonment_is_recorded_in_the_audit(self) -> None:
        self.enqueue("w1", "alpha")
        daemon = self.daemon(lambda s: refused(s), max_wake_attempts=1)
        daemon.drain()
        daemon.drain()
        records = [r for r in self.audit.records() if r["action"] == "abandoned"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")

    def test_waiting_since_is_recorded_so_the_wait_is_visible(self) -> None:
        self.enqueue("w1", "alpha")
        self.daemon(lambda s: refused(s)).drain()
        self.assertIn("waiting_since", self.store.pending()[0])

    def test_a_non_capacity_refusal_is_not_retried(self) -> None:
        """A bad profile will not fix itself by being tried again."""
        self.enqueue("w1", "alpha")
        daemon = self.daemon(lambda s: refused(s, gate="profile"))
        daemon.drain()
        self.assertEqual(self.store.pending(), [], "settled, not queued")


if __name__ == "__main__":
    unittest.main()
