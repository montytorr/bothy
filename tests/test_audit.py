"""The audit log: one join key, and evidence that it has not been edited."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bothy.audit import AuditLog, ChainBreak


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.log = AuditLog(self.dir / "audit.jsonl", max_bytes=1_000)

    def test_a_run_is_one_grep(self) -> None:
        """The property that makes the log worth keeping."""
        self.log.append(kind="wake", action="received", subject="issue-1")
        self.log.append(kind="run", action="admitted", run_id="run_x", subject="issue-1")
        self.log.append(kind="run", action="finished", run_id="run_x", subject="issue-1")
        self.log.append(kind="run", action="admitted", run_id="run_y", subject="other")
        mine = [r for r in self.log.records() if r.get("run_id") == "run_x"]
        self.assertEqual([r["action"] for r in mine], ["admitted", "finished"])

    def test_chain_verifies(self) -> None:
        for index in range(5):
            self.log.append(kind="run", action="finished", run_id=f"r{index}")
        self.assertEqual(self.log.verify(), 5)

    def test_an_edited_record_is_caught_at_its_own_sequence(self) -> None:
        for index in range(4):
            self.log.append(kind="run", action="finished", run_id=f"r{index}")
        path = self.log.path
        lines = path.read_text().splitlines()
        record = json.loads(lines[1])
        record["subject"] = "forged"
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(ChainBreak) as caught:
            AuditLog(path).verify()
        self.assertEqual(caught.exception.sequence, 2)

    def test_a_deleted_record_is_caught(self) -> None:
        for index in range(4):
            self.log.append(kind="run", action="finished", run_id=f"r{index}")
        lines = self.log.path.read_text().splitlines()
        del lines[2]
        self.log.path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(ChainBreak):
            AuditLog(self.log.path).verify()

    def test_rotation_keeps_the_chain_joined(self) -> None:
        """Without this, every rotation is a gap an editor could hide in."""
        for index in range(6):
            self.log.append(kind="run", action="finished", run_id=f"r{index}")
        archived = self.log.rotate()
        self.assertIsNotNone(archived)
        self.log.append(kind="run", action="finished", run_id="after")

        self.assertEqual(AuditLog(archived).verify(), 6, "the archived segment verifies alone")
        self.assertEqual(self.log.verify(), 2, "the live segment verifies alone")

        tip = None
        for record in AuditLog(archived).records():
            tip = record["hash"]
        marker = next(self.log.records())
        self.assertEqual(marker["action"], "rotated")
        self.assertEqual(marker["data"]["previous_hash"], tip, "the segments link")
        self.assertEqual(marker["data"]["previous_file"], archived.name)

    def test_tampering_inside_an_archived_segment_is_still_caught(self) -> None:
        for index in range(6):
            self.log.append(kind="run", action="finished", run_id=f"r{index}")
        archived = self.log.rotate()
        lines = archived.read_text().splitlines()
        record = json.loads(lines[2])
        record["status"] = "ok-honest"
        lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        archived.write_text("\n".join(lines) + "\n")
        with self.assertRaises(ChainBreak):
            AuditLog(archived).verify()

    def test_refusals_are_first_class(self) -> None:
        """"It did not start, and why" is what an operator actually asks."""
        self.log.append(kind="run", action="refused", status="refused",
                        subject="s", data={"gate": "lane_busy"})
        record = next(self.log.records())
        self.assertEqual(record["status"], "refused")
        self.assertEqual(record["data"]["gate"], "lane_busy")

    def test_a_truncated_last_line_costs_one_record_not_the_file(self) -> None:
        for index in range(3):
            self.log.append(kind="run", action="finished", run_id=f"r{index}")
        with self.log.path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 4, "partial')
        self.assertEqual(len(list(self.log.records())), 3)


if __name__ == "__main__":
    unittest.main()
