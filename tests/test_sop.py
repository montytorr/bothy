"""A job can carry a standard operating procedure; a missing one refuses, loudly."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bothy.audit import AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.config import Config
from bothy.daemon import Daemon
from bothy.pool import Admission, PoolPolicy
from bothy.proc import ProcessRegistry
from bothy.schedule import SOP_MAX_BYTES, Job, Schedule, ScheduleError, read_sop
from bothy.wake import Route, WakeStore


class Sink:
    def __init__(self) -> None:
        self.alerts: list[str] = []

    def incident(self, **kw: Any) -> bool:
        self.alerts.append(kw.get("error", ""))
        return True

    def resolved(self, **kw: Any) -> None:
        pass

    def say(self, message: str) -> None:
        pass


class SopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(home=Path(tempfile.mkdtemp()))
        self.config.ensure_dirs()
        self.config.sops_dir.mkdir()
        self.schedule = Schedule(self.config.state_dir / "jobs.json")
        self.sink = Sink()
        self.daemon = Daemon(
            self.config, runner=SimpleNamespace(),
            admission=Admission(PoolPolicy(), BudgetLedger(self.config.budget_path, BudgetPolicy(mode="api"))),
            audit=AuditLog(self.config.audit_path), registry=ProcessRegistry(self.config.registry_path),
            alerter=self.sink, routes=[Route(path="/x", secret="s")],
        )
        self.store = WakeStore(self.config.wake_dir)

    def sop(self, name: str, text: str) -> Path:
        path = self.config.sops_dir / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def job(self, sop: str | None = "triage") -> Job:
        return Job(id="j1", kind="every", spec="3600", prompt="go", sop=sop)

    def add(self, sop: str | None) -> Job:
        return self.schedule.put(self.job(sop), sops_dir=self.config.sops_dir)

    def test_the_text_is_injected_and_its_hash_recorded(self) -> None:
        path = self.sop("triage", "MUST check the queue.\n")
        job = self.add("triage")
        wake_id = self.daemon.fire(job)
        event = next(e for e in self.store.all_events() if e["id"] == wake_id)
        self.assertIn("Standard operating procedure `triage`", event["payload"]["prompt"])
        self.assertIn("MUST check the queue.", event["payload"]["prompt"])
        self.assertEqual(event["sop_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_a_traversal_name_is_refused(self) -> None:
        for name in ("../x", "a/b", "A", "", "-x"):
            with self.assertRaises(ScheduleError, msg=name):
                self.add(name)

    def test_a_symlink_out_of_the_directory_is_refused(self) -> None:
        outside = Path(tempfile.mkdtemp()) / "x.md"
        outside.write_text("secret", encoding="utf-8")
        (self.config.sops_dir / "evil.md").symlink_to(outside)
        with self.assertRaises(ScheduleError):
            self.add("evil")

    def test_a_missing_file_is_refused_at_add(self) -> None:
        with self.assertRaises(ScheduleError):
            self.add("nope")

    def refused_wake(self, job: Job) -> dict[str, Any]:
        wake_id = self.daemon.fire(job)
        return next(e for e in self.store.all_events() if e["id"] == wake_id)

    def assert_refused_and_moved_on(self, job: Job) -> None:
        before = self.schedule.get("j1").next_due_at
        event = self.refused_wake(job)
        self.assertTrue(event["processed"])
        self.assertEqual((event["outcome"], event["gate"]), ("refused", "sop_missing"))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(len(self.sink.alerts), 1)
        self.assertTrue(any(r.get("action") == "refused" and r["data"]["gate"] == "sop_missing"
                            for r in self.daemon.audit.records()))
        self.schedule.mark_fired("j1")
        self.assertNotEqual(self.schedule.get("j1").next_due_at, before)

    def test_a_sop_deleted_after_add_refuses_the_wake(self) -> None:
        path = self.sop("triage", "MUST x")
        job = self.add("triage")
        path.unlink()
        self.assert_refused_and_moved_on(job)

    def test_an_empty_sop_refuses_the_wake(self) -> None:
        self.sop("triage", "MUST x")
        job = self.add("triage")
        self.sop("triage", "  \n")
        self.assert_refused_and_moved_on(job)

    def test_an_oversized_sop_is_not_truncated(self) -> None:
        self.sop("big", "x" * (SOP_MAX_BYTES + 1))
        with self.assertRaises(ScheduleError):
            read_sop(self.config.sops_dir, "big")


if __name__ == "__main__":
    unittest.main()
