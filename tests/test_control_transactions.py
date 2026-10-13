"""Independent CLI and runner state writers cannot lose each other's writes."""

from __future__ import annotations

import concurrent.futures
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bothy import clock
from bothy.audit import AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.proc import ProcessRegistry, Worker


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_independent_ledgers_settle_the_same_run_only_once(self):
        policy = BudgetPolicy(mode="api")
        ledger_path = self.path / "budget.json"
        BudgetLedger(ledger_path, policy).reserve(run_id="run")

        def settle(_):
            return BudgetLedger(ledger_path, policy).settle_run("run")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            settled = list(pool.map(settle, range(8)))
        self.assertEqual(sum(len(rows) for rows in settled), 1)
        self.assertEqual(BudgetLedger(ledger_path, policy).snapshot()["used"], 2.0)

    def test_independent_registries_remove_only_their_own_workers(self):
        registry_path = self.path / "workers.json"
        registry = ProcessRegistry(registry_path)
        registry._save({str(n): Worker(str(n), n, n, [], clock.iso()).as_dict() for n in range(24)})

        def unregister(n):
            ProcessRegistry(registry_path).unregister(str(n))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(unregister, range(24)))
        self.assertEqual(registry.workers(), [])

    def test_independent_audit_instances_refresh_the_tail_before_writing(self):
        path = self.path / "audit.jsonl"
        runner, cli = AuditLog(path), AuditLog(path)
        runner.append(kind="run", action="started")
        cli.append(kind="run", action="cancel_requested")
        runner.append(kind="run", action="finished")
        self.assertEqual(runner.verify(), 3)

    def test_an_audit_instance_refreshes_after_another_instance_rotates(self):
        path = self.path / "audit.jsonl"
        runner, cli = AuditLog(path), AuditLog(path)
        runner.append(kind="run", action="started")
        cli.append(kind="run", action="cancel_requested")
        archived = cli.rotate()
        runner.append(kind="run", action="finished")
        self.assertEqual(AuditLog(archived).verify(), 2)
        self.assertEqual(runner.verify(), 2)
        self.assertEqual(next(runner.records())["seq"], 3)

    def test_audit_writes_from_separate_processes_stay_on_one_chain(self):
        path = self.path / "audit.jsonl"
        script = """import sys
from bothy.audit import AuditLog
log = AuditLog(sys.argv[1])
for n in range(20):
    log.append(kind='run', action='event', data={'n': n})
"""

        def write(_):
            subprocess.run([sys.executable, "-c", script, str(path)], check=True,
                           cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(write, range(3)))
        self.assertEqual(AuditLog(path).verify(), 60)

    def test_audit_appends_use_cache_or_seek_tail_without_a_full_walk(self):
        path = self.path / "audit.jsonl"
        runner, cli = AuditLog(path), AuditLog(path)
        runner.append(kind="run", action="started")
        with mock.patch.object(runner, "records", side_effect=AssertionError("full walk")), \
                mock.patch.object(runner, "_tail_tip", wraps=runner._tail_tip) as tail:
            runner.append(kind="run", action="event")
            tail.assert_not_called()  # unchanged inode/size uses the cache
            cli.append(kind="run", action="cancel_requested")
            runner.append(kind="run", action="finished")
            self.assertEqual(tail.call_count, 1)
            cli.rotate()
            runner.append(kind="run", action="event")
            self.assertEqual(tail.call_count, 2)
        self.assertEqual(runner.verify(), 2)

    def test_audit_seek_handles_a_record_larger_than_one_read_chunk(self):
        path = self.path / "audit.jsonl"
        AuditLog(path).append(kind="run", action="large", data={"text": "x" * 9000})
        other = AuditLog(path)
        with mock.patch.object(other, "records", side_effect=AssertionError("full walk")):
            other.append(kind="run", action="next")
        self.assertEqual(other.verify(), 2)

    def test_unparseable_audit_tail_falls_back_to_tolerant_walk(self):
        path = self.path / "audit.jsonl"
        log = AuditLog(path)
        expected = log.append(kind="run", action="started")
        with path.open("a") as handle:
            handle.write("unparseable\n")
        with mock.patch.object(log, "records", wraps=log.records) as records:
            self.assertEqual(log.tip(), (expected["seq"], expected["hash"]))
            records.assert_called_once()


if __name__ == "__main__":
    unittest.main()
