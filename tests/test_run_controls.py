"""Operator cancellation and accounting, with a stub app-server."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bothy import clock, codex, ids
from bothy.audit import AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.cli import main
from bothy.config import Config, load
from bothy.daemon import Daemon
from bothy.pool import Admission
from bothy.proc import ProcessRegistry, Worker
from bothy.runner import Runner
from bothy.schedule import Job, Schedule
from bothy.wake import WakeStore


class ControlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(home=Path(self.temp.name), budget=BudgetPolicy(mode="api"),
                             codex_credentials_dir=Path(self.temp.name) / "credentials")
        self.config.ensure_dirs()
        self.ledger = BudgetLedger(self.config.budget_path, self.config.budget)
        self.admission = Admission(self.config.pool, self.ledger)
        self.audit = AuditLog(self.config.audit_path)
        self.registry = ProcessRegistry(self.config.registry_path)
        self.runner = Runner(self.config, admission=self.admission, audit=self.audit, registry=self.registry)
        self.run_id = ids.run_id()
        self.worker = Worker(self.run_id, 12345, 12345, ["codex", "app-server"], clock.iso())
        self.config_path = self.config.home / "config.json"
        self.config_path.write_text(json.dumps({"home": str(self.config.home), "budget": {"mode": "api"}}))

    def cancel(self, run_id=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.config_path), "cancel", run_id or self.run_id])
        return code, out.getvalue() + err.getvalue()

    def register(self, run_id, popen, argv):
        self.worker.run_id = run_id
        self.registry._save({run_id: self.worker.as_dict()})
        return self.worker

    def client(self, *, usage=None, complete=False, ready=None, never_finish_interrupt=False):
        outer = self

        class StubClient(codex.CodexClient):
            def start(self):
                self._popen = SimpleNamespace(pid=12345)
                outer.live_client = self
                return self._popen

            def handshake(self, **kwargs):
                return {}

            def start_thread(self, **kwargs):
                return "thread"

            def request(self, method, params=None, **kwargs):
                if method == "turn/start":
                    if usage is not None:
                        self._notifications.put(("thread/tokenUsage/updated", {"tokenUsage": {"total": usage}}))
                    if complete:
                        self._notifications.put(("turn/completed", {"turn": {"status": "completed"}}))
                    if ready is not None:
                        ready.set()
                    return {"turn": {"id": "turn"}}
                if method == "turn/interrupt" and not never_finish_interrupt:
                    self._notifications.put(("turn/completed", {"turn": {"status": "interrupted"}}))
                return {}

            def run_turn(self, **kwargs):
                outer.limits = kwargs
                return super().run_turn(**kwargs)

            def close(self, **kwargs):
                outer.closed = kwargs
                return "terminated"

        return StubClient

    def harness(self, client_type, **kwargs):
        with mock.patch("bothy.runner.codex.CodexClient", client_type), \
                mock.patch.object(self.registry, "register", self.register), \
                mock.patch("bothy.runner.ids.run_id", return_value=self.run_id):
            return self.runner.run(subject="lane", prompt="go", **kwargs)

    def assert_freed(self):
        self.assertEqual(self.admission.snapshot()["lanes"], {})
        self.assertEqual(self.admission.snapshot()["in_flight"], 0)
        self.assertEqual(self.ledger.snapshot()["in_flight"], 0)
        self.assertEqual(self.registry.workers(), [])
        self.assertFalse((self.config.control_dir / f"{self.run_id}.cancel").exists())

    def test_live_cli_cancel_stops_settles_and_frees_lane_within_two_seconds(self) -> None:
        ready = threading.Event()
        results, errors = [], []

        def run():
            try:
                results.append(self.harness(self.client(ready=ready)))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.assertTrue(ready.wait(2), "stub reached its turn")
        started = time.monotonic()
        with mock.patch.object(Worker, "still_ours", return_value=True), \
                mock.patch("bothy.cli.proc.kill_group") as killed:
            code, output = self.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(code, 0, output)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(results[0].stop_reason, "cancelled")
        self.assertEqual(results[0].status, "interrupted")
        self.assertIn("clean interrupt", output)
        killed.assert_not_called()
        self.assert_freed()
        terminal = [r for r in self.audit.records() if r["action"] == "finished"][-1]
        self.assertEqual(terminal["data"]["stop_reason"], "cancelled")
        self.assertEqual(self.audit.verify(), len(list(self.audit.records())))

    def test_quiet_worker_without_terminal_event_still_frees_within_two_seconds(self) -> None:
        ready = threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.harness(
            self.client(ready=ready, never_finish_interrupt=True))))
        thread.start()
        self.addCleanup(thread.join, 3)
        self.assertTrue(ready.wait(2))
        started = time.monotonic()
        with mock.patch.object(Worker, "still_ours", return_value=True):
            code, output = self.cancel()
        thread.join(2)
        self.assertEqual(code, 0, output)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(results[0].stop_reason, "cancelled")
        self.assert_freed()

    def test_api_spend_over_reservation_stops_with_budget_reason(self) -> None:
        result = self.harness(self.client(usage={"outputTokens": 300_000}, complete=True))
        self.assertEqual((result.status, result.stop_reason), ("interrupted", "budget"))
        self.assertEqual(result.cost, 3.0)
        self.assertEqual(self.ledger.snapshot()["used"], 3.0)
        self.assert_freed()
        self.assertEqual([r for r in self.audit.records() if r["action"] == "finished"][-1]
                         ["data"]["stop_reason"], "budget")

    def test_hung_worker_fallback_kills_group_settles_and_reports_it(self) -> None:
        self.registry._save({self.run_id: self.worker.as_dict()})
        self.ledger.reserve(run_id=self.run_id)
        with mock.patch.object(Worker, "still_ours", return_value=True) as identity, \
                mock.patch("bothy.cli.clock.monotonic", side_effect=[0, 3]), \
                mock.patch("bothy.cli.proc.kill_group", return_value="killed") as killed:
            code, output = self.cancel()
        self.assertEqual(code, 0, output)
        self.assertIn("fallback: killed", output)
        killed.assert_called_once_with(self.worker.pgid, grace_seconds=0.25)
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(self.ledger.snapshot()["in_flight"], 0)
        self.assertEqual(self.ledger.snapshot()["used"], 2.0)
        self.assertEqual(self.registry.workers(), [])
        record = list(self.audit.records())[-1]
        self.assertEqual(record["data"]["stop_reason"], "cancelled")
        self.assertTrue(record["data"]["fallback"])
        self.assertEqual(record["data"]["disposal"], "killed")

    def test_unverified_worker_identity_is_never_killed(self) -> None:
        self.registry._save({self.run_id: self.worker.as_dict()})
        with mock.patch.object(Worker, "still_ours", side_effect=[True, False]), \
                mock.patch("bothy.cli.clock.monotonic", side_effect=[0, 3]), \
                mock.patch("bothy.cli.proc.kill_group") as killed:
            code, output = self.cancel()
        self.assertEqual(code, 0, output)
        killed.assert_not_called()
        self.assertIn("already-gone", output)

    def test_unkillable_worker_preserves_reservation_and_control(self) -> None:
        self.registry._save({self.run_id: self.worker.as_dict()})
        self.ledger.reserve(run_id=self.run_id)
        with mock.patch.object(Worker, "still_ours", return_value=True), \
                mock.patch("bothy.cli.clock.monotonic", side_effect=[0, 3]), \
                mock.patch("bothy.cli.proc.kill_group", return_value="unkillable"):
            code, output = self.cancel()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.ledger.snapshot()["in_flight"], 1)
        self.assertEqual(len(self.registry.workers()), 1)
        self.assertTrue((self.config.control_dir / f"{self.run_id}.cancel").exists())

    def test_unknown_and_malformed_ids_signal_nothing(self) -> None:
        for candidate in [ids.run_id(), "bad", "../workers.json", "run_1234567890123456_../x...."]:
            with self.subTest(candidate=candidate), mock.patch("bothy.cli.proc.kill_group") as killed:
                code, output = self.cancel(candidate)
                self.assertNotEqual(code, 0, output)
                killed.assert_not_called()
        self.assertEqual(list(self.config.control_dir.iterdir()), [])
        self.assertEqual(list(self.audit.records()), [])

    def test_dead_record_is_not_a_live_run(self) -> None:
        self.registry._save({self.run_id: self.worker.as_dict()})
        with mock.patch.object(Worker, "still_ours", return_value=False), \
                mock.patch("bothy.cli.proc.kill_group") as killed:
            self.assertNotEqual(self.cancel()[0], 0)
            killed.assert_not_called()
        self.assertEqual(list(self.config.control_dir.iterdir()), [])

    def test_cancelled_startup_error_retains_cause_and_releases_lane(self) -> None:
        stub = self.client()

        def handshake(client, **kwargs):
            (self.config.control_dir / f"{self.run_id}.cancel").write_text("cancelled")
            raise codex.CodexError("worker closed during handshake")

        stub.handshake = handshake
        result = self.harness(stub)
        self.assertEqual((result.status, result.stop_reason), ("interrupted", "cancelled"))
        self.assert_freed()

    def test_startup_error_after_fallback_cleanup_retains_cancelled_cause(self) -> None:
        stub = self.client()

        def handshake(client, **kwargs):
            self.audit.append(kind="run", action="finished", status="interrupted", run_id=self.run_id,
                              data={"fallback": True, "stop_reason": "cancelled"})
            raise codex.CodexError("worker was reaped")

        stub.handshake = handshake
        result = self.harness(stub)
        self.assertEqual((result.status, result.stop_reason), ("interrupted", "cancelled"))
        self.assert_freed()

    def test_worker_exit_after_fallback_cleanup_retains_cancelled_cause(self) -> None:
        stub = self.client()

        def turn(client, **kwargs):
            self.audit.append(kind="run", action="finished", status="interrupted", run_id=self.run_id,
                              data={"fallback": True, "stop_reason": "cancelled"})
            return codex.TurnOutcome(status="failed", error="app-server exited")

        stub.run_turn = turn
        result = self.harness(stub)
        self.assertEqual((result.status, result.stop_reason), ("interrupted", "cancelled"))
        self.assert_freed()

    def test_cancellation_audit_lookup_stops_at_first_match(self) -> None:
        stub = self.client()
        stub.run_turn = lambda client, **kwargs: codex.TurnOutcome(status="failed")

        def records():
            yield {"run_id": self.run_id, "action": "finished",
                   "data": {"fallback": True, "stop_reason": "cancelled"}}
            raise AssertionError("lookup continued after finding cancellation")

        with mock.patch.object(self.audit, "records", records):
            result = self.harness(stub)
        self.assertEqual(result.stop_reason, "cancelled")
        self.assert_freed()

    def test_limits_fall_back_from_run_to_profile_to_config(self) -> None:
        self.config.max_tokens = 2000
        self.config.wall_clock_seconds = 800
        self.config.profiles = {"limited": {"max_tokens": 1500, "wall_clock_seconds": 400}}
        for kwargs, expected in [({}, (2000, 800)), ({"profile": "limited"}, (1500, 400)),
                                 ({"profile": "limited", "max_tokens": 1000, "wall_clock_seconds": 200},
                                  (1000, 200))]:
            with self.subTest(kwargs=kwargs):
                result = self.harness(self.client(complete=True), **kwargs)
                self.assertTrue(result.ok)
                self.assertEqual((self.limits["max_tokens"], self.limits["wall_clock_seconds"]), expected)
                self.assert_freed()

    def test_invalid_limits_refuse_and_release_the_reservation(self) -> None:
        for kwargs in [{"max_tokens": 0}, {"max_tokens": -1}, {"wall_clock_seconds": 0}]:
            with self.subTest(kwargs=kwargs):
                result = self.harness(self.client(complete=True), **kwargs)
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.refusal["gate"], "limits")
                self.assert_freed()

    def test_job_limits_survive_storage_and_the_wake_wire_format(self) -> None:
        schedule = Schedule(self.config.state_dir / "jobs.json")
        self.config.sops_dir.mkdir()
        (self.config.sops_dir / "limited.md").write_text("MUST: inspect the queued task")
        schedule.put(Job("limited", "every", "60", wall_clock_seconds=45, max_tokens=1000,
                         sop="limited"), sops_dir=self.config.sops_dir)
        job = schedule.get("limited")
        daemon = Daemon(self.config, runner=self.runner, admission=self.admission, audit=self.audit,
                        registry=self.registry, alerter=None, routes=[])
        daemon.fire(job)
        event = WakeStore(self.config.wake_dir).pending()[0]
        self.assertEqual((event["wall_clock_seconds"], event["max_tokens"]), (45, 1000))
        self.assertEqual(event["sop"], "limited")
        self.assertEqual(len(event["sop_sha256"]), 64)
        self.assertIn("MUST: inspect the queued task", event["prompt"])
        with mock.patch.object(self.runner, "run", return_value=SimpleNamespace(
                status="completed", refusal=None, messages=[], run_id=self.run_id)) as run:
            daemon.drain()
        self.assertEqual(run.call_args.kwargs["max_tokens"], 1000)
        self.assertEqual(run.call_args.kwargs["wall_clock_seconds"], 45)
        self.assertEqual(run.call_args.kwargs["prompt"], event["prompt"])
        self.assertFalse(run.call_args.kwargs["untrusted"])
        self.assertIsNone(Job.from_dict({"id": "legacy", "kind": "every", "spec": "60"}).max_tokens)

    def test_cli_schedule_limit_flags_are_persisted(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--config", str(self.config_path), "schedule", "add", "limited",
                         "--max-tokens", "1000", "--wall-clock", "45"])
        self.assertEqual(code, 0)
        job = Schedule(self.config.state_dir / "jobs.json").get("limited")
        self.assertEqual((job.max_tokens, job.wall_clock_seconds), (1000, 45))

    def test_config_max_tokens_is_loaded_and_visible(self) -> None:
        self.config_path.write_text(json.dumps({"max_tokens": 1000}))
        loaded = load(self.config_path)
        self.assertEqual(loaded.max_tokens, 1000)
        self.assertEqual(loaded.as_dict()["max_tokens"], 1000)

    def test_cli_schedule_refuses_invalid_limits_without_storing_them(self) -> None:
        for flag, value in [("--max-tokens", "-5"), ("--max-tokens", "0"),
                            ("--wall-clock", "0"), ("--wall-clock", "nan")]:
            with self.subTest(flag=flag, value=value), contextlib.redirect_stdout(io.StringIO()):
                code = main(["--config", str(self.config_path), "schedule", "add", "invalid", flag, value])
                self.assertNotEqual(code, 0)
                self.assertIsNone(Schedule(self.config.state_dir / "jobs.json").get("invalid"))

    def test_taint_and_token_cap_both_reach_the_runner_after_merge(self) -> None:
        with mock.patch.object(self.runner, "_registry", wraps=self.runner._registry) as registry:
            result = self.harness(self.client(usage={"totalTokens": 1200}, complete=True),
                                  max_tokens=1000, untrusted=True)
        registry.assert_called_once_with(None, untrusted=True)
        self.assertEqual(result.stop_reason, "token_cap")
        self.assert_freed()


if __name__ == "__main__":
    unittest.main()
