"""Lanes and the pool: who may start, and why a refusal happened."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.pool import Admission, PoolPolicy, Refused


def gate(tmp: Path, **policy: object) -> Admission:
    settings = {"max_workers": 2}
    settings.update(policy)
    ledger = BudgetLedger(tmp / "b.json", BudgetPolicy(mode="api", per_run_usd=0.1, per_day_usd=1000.0))
    return Admission(PoolPolicy(**settings), ledger)  # type: ignore[arg-type]


class LaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def test_two_subjects_run_together(self) -> None:
        door = gate(self.dir)
        door.admit(run_id="a", subject="alpha")
        door.admit(run_id="b", subject="beta")
        self.assertEqual(door.snapshot()["in_flight"], 2)

    def test_one_subject_serialises(self) -> None:
        """The rule the closest prior art skips by keying on delivery id."""
        door = gate(self.dir)
        door.admit(run_id="a", subject="alpha")
        with self.assertRaises(Refused) as caught:
            door.admit(run_id="b", subject="alpha")
        self.assertEqual(caught.exception.gate, "lane_busy")
        self.assertEqual(caught.exception.detail["held_by"], "a")

    def test_the_lane_frees_on_release(self) -> None:
        door = gate(self.dir)
        door.admit(run_id="a", subject="alpha")
        door.release("a", ok=True, actual=0.01)
        door.admit(run_id="b", subject="alpha")

    def test_the_pool_is_bounded_and_says_so(self) -> None:
        door = gate(self.dir, max_workers=1)
        door.admit(run_id="a", subject="alpha")
        with self.assertRaises(Refused) as caught:
            door.admit(run_id="b", subject="beta")
        self.assertEqual(caught.exception.gate, "pool_full")
        self.assertEqual(caught.exception.detail["effective_size"], 1)

    def test_releasing_twice_is_safe(self) -> None:
        """A supervisor that crashes between settling and releasing calls again."""
        door = gate(self.dir)
        door.admit(run_id="a", subject="alpha")
        door.release("a", ok=True, actual=0.01)
        self.assertEqual(door.release("a", ok=True, actual=0.01), 0.0)

    def test_budget_refusal_is_reported_through_the_same_door(self) -> None:
        ledger = BudgetLedger(self.dir / "b2.json",
                              BudgetPolicy(mode="api", per_run_usd=2.0, per_day_usd=3.0))
        door = Admission(PoolPolicy(max_workers=8), ledger)
        door.admit(run_id="a", subject="alpha")
        with self.assertRaises(Refused) as caught:
            door.admit(run_id="b", subject="beta")
        self.assertEqual(caught.exception.gate, "budget")
        self.assertIn("ceiling", str(caught.exception))


class SelfHealingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def _cycle(self, door: Admission, count: int, ok: bool, tag: str) -> None:
        for index in range(count):
            try:
                door.admit(run_id=f"{tag}{index}", subject=f"{tag}-{index}")
                door.release(f"{tag}{index}", ok=ok, actual=0.0)
            except Refused:
                pass
            time.sleep(0.02)

    def test_it_collapses_under_failure_and_climbs_back_unattended(self) -> None:
        door = gate(self.dir, max_workers=4, recompute_seconds=0.01,
                    window_seconds=60, min_samples=4, recent_samples=10)
        self._cycle(door, 12, False, "bad")
        self.assertEqual(door.effective_size(), 1, "collapsed to a single run")
        self.assertEqual(door.state(), "probing")

        for round_index in range(8):
            self._cycle(door, 6, True, f"good{round_index}")
        self.assertEqual(door.effective_size(), 4, "recovered to full")
        self.assertEqual(door.state(), "healthy")

    def test_recovery_is_paced_by_recent_outcomes_not_the_whole_window(self) -> None:
        """A burst of failures must not pin the pool for the entire window.

        The first version used a time window alone, and "degraded" became
        indistinguishable from "hung" for fifteen minutes at a time.
        """
        door = gate(self.dir, max_workers=4, recompute_seconds=0.01,
                    window_seconds=3600, min_samples=4, recent_samples=10)
        self._cycle(door, 12, False, "bad")
        for round_index in range(6):
            self._cycle(door, 6, True, f"good{round_index}")
        self.assertGreater(door.effective_size(), 1,
                           "old failures still inside the time window must not pin it")


if __name__ == "__main__":
    unittest.main()
