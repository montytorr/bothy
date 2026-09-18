"""The reservation ledger: the mechanism parallelism forces."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bothy.budget import PRICING, BudgetLedger, BudgetPolicy, BudgetRefused, Usage


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def ledger(self, **policy: object) -> BudgetLedger:
        settings = {"mode": "api", "per_run_usd": 2.0, "per_day_usd": 5.0}
        settings.update(policy)
        return BudgetLedger(self.dir / "budget.json", BudgetPolicy(**settings))  # type: ignore[arg-type]

    def test_parallel_runs_cannot_collectively_breach_the_cap(self) -> None:
        """The whole reason reservations exist.

        Measuring after the fact cannot bound a total several writers are moving
        at once: each run would observe the same headroom and all would start.
        """
        ledger = self.ledger()
        ledger.reserve(run_id="a")
        ledger.reserve(run_id="b")
        with self.assertRaises(BudgetRefused) as caught:
            ledger.reserve(run_id="c")
        self.assertEqual(caught.exception.cap, "per_day_usd")
        self.assertEqual(caught.exception.limit, 5.0)
        self.assertEqual(caught.exception.current, 4.0)

    def test_refusal_carries_what_an_operator_needs(self) -> None:
        ledger = self.ledger(mode="subscription", per_run_percent=2.0, ceiling_percent=85.0)
        # 82 + one 2% reservation fits under 85; a second would reach 86.
        ledger.reserve(run_id="a", observed_used=82.0, resets_at="2026-09-20T11:31:42+00:00")
        with self.assertRaises(BudgetRefused) as caught:
            ledger.reserve(run_id="b", observed_used=82.0, resets_at="2026-09-20T11:31:42+00:00")
        payload = caught.exception.as_dict()
        self.assertEqual(payload["unit"], "percent")
        self.assertEqual(payload["resets_at"], "2026-09-20T11:31:42+00:00")
        self.assertIn("ceiling", payload["reason"])

    def test_settling_under_the_reservation_returns_headroom(self) -> None:
        ledger = self.ledger()
        first = ledger.reserve(run_id="a")
        ledger.reserve(run_id="b")
        ledger.settle(first.id, 0.25)
        self.assertAlmostEqual(ledger.snapshot()["used"], 0.25)
        ledger.reserve(run_id="c")  # must not raise: headroom came back

    def test_an_overrun_squeezes_everyone_else_immediately(self) -> None:
        """held() is max(promised, actual), so an overrun is felt while it happens."""
        ledger = self.ledger()
        reservation = ledger.reserve(run_id="a")
        ledger.report(reservation.id, 4.5)
        self.assertGreater(ledger.snapshot()["total"], 4.0)
        with self.assertRaises(BudgetRefused):
            ledger.reserve(run_id="b")

    def test_a_crashed_runs_reservation_expires_and_accrues(self) -> None:
        """Money is an exclusive slot; every exclusive slot needs a path back to empty."""
        ledger = self.ledger(reservation_ttl_seconds=0.0)
        ledger.reserve(run_id="doomed")
        expired = ledger.expire_stale()
        self.assertEqual([r.run_id for r in expired], ["doomed"])
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["in_flight"], 0)
        self.assertAlmostEqual(snapshot["used"], 2.0, msg="spend is assumed, the safe direction")

    def test_settle_run_releases_by_run_id_for_the_reaper(self) -> None:
        ledger = self.ledger()
        ledger.reserve(run_id="orphan")
        self.assertEqual(len(ledger.settle_run("orphan")), 1)
        self.assertEqual(ledger.snapshot()["in_flight"], 0)

    def test_subscription_mode_does_not_accrue_its_own_total(self) -> None:
        """Codex owns the total there; adding our own would double-count."""
        ledger = self.ledger(mode="subscription", per_run_percent=2.0, ceiling_percent=85.0)
        reservation = ledger.reserve(run_id="a", observed_used=10.0)
        ledger.settle(reservation.id, 2.0)
        self.assertEqual(ledger.snapshot(observed_used=10.0)["used"], 10.0)

    def test_subscription_mode_refuses_to_guess_the_position(self) -> None:
        ledger = self.ledger(mode="subscription")
        with self.assertRaises(ValueError):
            ledger.reserve(run_id="a")

    def test_cache_writes_are_priced_as_their_own_line(self) -> None:
        """58% of a documented $19,302 run was cache writes. They are not input."""
        pricing = PRICING["default"]
        writes = Usage(cache_write_tokens=1_000_000)
        reads = Usage(cached_input_tokens=1_000_000)
        self.assertGreater(pricing.cost(writes), pricing.cost(reads) * 5)

    def test_usage_reads_both_field_spellings(self) -> None:
        camel = Usage.from_app_server({"inputTokens": 5, "cacheWriteInputTokens": 7})
        snake = Usage.from_app_server({"input_tokens": 5, "cache_write_input_tokens": 7})
        self.assertEqual(camel, snake)


if __name__ == "__main__":
    unittest.main()
