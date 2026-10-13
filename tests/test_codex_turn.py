"""Supervise turns with protocol stubs; never spawn a real Codex."""

from __future__ import annotations

import tempfile
import queue
import time
import unittest
from pathlib import Path
from unittest import mock

from bothy import codex


class TurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = codex.CodexClient(run_id="test", codex_home=Path(self.temp.name))
        self.calls: list[str] = []

        def request(method, params=None, **kwargs):
            self.calls.append(method)
            if method == "turn/start":
                return {"turn": {"id": "turn"}}
            if method == "turn/interrupt":
                self.client._notifications.put(("turn/completed", {"turn": {"status": "interrupted"}}))
            return {}

        self.client.request = request

    def turn(self, **kwargs):
        return self.client.run_turn(thread_id="thread", text="go", **kwargs)

    def usage(self, total):
        self.client._notifications.put(("thread/tokenUsage/updated", {"tokenUsage": {"total": total}}))

    def test_token_cap_interrupts_at_1200_with_a_1000_limit(self) -> None:
        self.usage({"totalTokens": 1200, "inputTokens": 1000, "outputTokens": 200})
        outcome = self.turn(max_tokens=1000)
        self.assertEqual((outcome.status, outcome.stop_reason), ("interrupted", "token_cap"))
        self.assertEqual(self.calls, ["turn/start", "turn/interrupt"])
        self.assertEqual(outcome.usage["totalTokens"], 1200)

    def test_token_count_without_total_uses_input_plus_output(self) -> None:
        self.usage({"inputTokens": 800, "outputTokens": 200, "reasoningOutputTokens": 100})
        self.assertEqual(self.turn(max_tokens=1000).stop_reason, "token_cap")

    def test_wall_clock_overrun_has_a_typed_cause(self) -> None:
        outcome = self.turn(wall_clock_seconds=0)
        self.assertEqual((outcome.status, outcome.stop_reason), ("interrupted", "wall_clock"))
        self.assertIn("wall clock", outcome.error)

    def test_tick_observes_cancellation_while_the_worker_is_quiet(self) -> None:
        started = time.monotonic()
        outcome = self.turn(should_stop=lambda: "cancelled")
        self.assertEqual(outcome.stop_reason, "cancelled")
        self.assertLess(time.monotonic() - started, 2)

    def test_budget_callback_runs_after_the_usage_observer(self) -> None:
        spent = 0

        def meter(total):
            nonlocal spent
            spent = total["outputTokens"]

        self.usage({"outputTokens": 1200})
        outcome = self.turn(on_usage=meter, should_stop=lambda: "budget" if spent > 1000 else None)
        self.assertEqual(outcome.stop_reason, "budget")

    def test_awaiting_human_can_be_requested_by_a_reader_callback(self) -> None:
        self.client._interrupt("awaiting_human")
        self.client._interrupt("wall_clock")
        self.assertEqual(self.turn().stop_reason, "awaiting_human")
        self.assertEqual(self.calls.count("turn/interrupt"), 1)

    def test_interrupt_has_a_deadline_even_without_a_terminal_event(self) -> None:
        self.client.request = lambda *a, **k: {"turn": {"id": "turn"}}
        started = time.monotonic()
        outcome = self.turn(should_stop=lambda: "cancelled", interrupt_grace_seconds=0.02)
        self.assertEqual(outcome.stop_reason, "cancelled")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_failed_interrupt_preserves_the_cause(self) -> None:
        original = self.client.request

        def request(method, *args, **kwargs):
            if method == "turn/interrupt":
                raise codex.CodexTimeout("no answer")
            return original(method, *args, **kwargs)

        self.client.request = request
        self.assertEqual(self.turn(wall_clock_seconds=0).stop_reason, "wall_clock")

    def test_completed_turn_below_limit_has_no_stop_reason(self) -> None:
        self.usage({"totalTokens": 900})
        self.client._notifications.put(("item/completed", {"item": {"type": "agentMessage", "text": "done"}}))
        self.client._notifications.put(("turn/completed", {"turn": {"status": "completed"}}))
        outcome = self.turn(max_tokens=1000)
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(outcome.stop_reason)
        self.assertEqual(outcome.messages, ["done"])
        self.assertNotIn("turn/interrupt", self.calls)

    def test_terminal_error_does_not_replace_the_interrupt_cause(self) -> None:
        self.client._notifications.put(("turn/completed", {"turn": {"status": "failed", "error": "remote"}}))
        outcome = self.turn(should_stop=lambda: "cancelled")
        self.assertEqual(outcome.error, "turn stopped: cancelled")

    def test_cancelled_rpc_uses_short_grace(self) -> None:
        original = self.client.request
        timeouts = []

        def request(method, *args, **kwargs):
            if method == "turn/interrupt":
                timeouts.append(kwargs["timeout"])
            return original(method, *args, **kwargs)

        self.client.request = request
        self.turn(should_stop=lambda: "cancelled")
        self.assertEqual(timeouts, [1.0])

    def test_other_interrupts_preserve_thirty_second_rpc_and_event_grace(self) -> None:
        for reason in ["wall_clock", "token_cap", "budget", "awaiting_human"]:
            for request_timeout, grace in [(60, 30), (10, 10)]:
                with self.subTest(reason=reason, request_timeout=request_timeout):
                    self.client._stop_reason = None
                    timeouts = []

                    def request(method, *args, **kwargs):
                        if method == "turn/interrupt":
                            timeouts.append(kwargs["timeout"])
                        return {"turn": {"id": "turn"}}

                    self.client.request = request
                    # The RPC succeeds without a terminal notification. At
                    # grace - 1 the event loop must still be waiting; at grace
                    # it must exit, without sleeping through a real 30s wait.
                    times = [0, 0, 0, 0, grace - 1, grace]
                    with mock.patch("bothy.codex.clock.monotonic", side_effect=times), \
                            mock.patch.object(self.client._notifications, "get", side_effect=queue.Empty) as get:
                        outcome = self.turn(should_stop=lambda: reason, request_timeout=request_timeout)
                    self.assertEqual(outcome.stop_reason, reason)
                    self.assertEqual(timeouts, [grace])
                    get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
