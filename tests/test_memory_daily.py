"""Daily memory: concurrent audited appends, disposable search and retention."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from bothy import memory, memory_index
from bothy.audit import AuditError, AuditLog
from bothy.budget import BudgetPolicy
from bothy.capability import ToolRegistry
from bothy.cli import cmd_doctor, main
from bothy.config import Config, load
from bothy.retention import sweep

NOW = dt.datetime(2026, 10, 13, 23, 30, tzinfo=dt.timezone.utc)
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


class DailyMemory(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = Config(home=self.home, workspace=self.home / "workspace", timezone="Europe/Paris",
                             budget=BudgetPolicy(mode="api"),
                             profiles={"worker": {"memory": "write"}})
        self.config.ensure_dirs()
        self.root = self.config.memory_dir
        self.directory = self.root / "worker"
        self.directory.mkdir(parents=True)
        self.audit = AuditLog(self.config.audit_path)
        frozen = patch("bothy.memory.clock.utcnow", return_value=NOW)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.day = self.directory / "daily/2026-10-14.md"

    def note(self, text: str) -> str:
        return memory.note(self.root, "worker", self.audit, text=text, timezone=self.config.timezone)

    def search(self, query: str, limit: int = 6) -> list[dict]:
        return memory_index.search(self.root, self.config.state_dir, "worker", query, limit)

    def put(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_timestamp_date_and_audit_precede_fsync_append(self) -> None:
        original = self.audit.append

        def audit_first(**kwargs):
            self.assertFalse(self.day.exists())
            self.assertEqual(kwargs["action"], "note")
            return original(**kwargs)

        with patch.object(self.audit, "append", side_effect=audit_first), \
                patch("bothy.memory.os.fsync", wraps=os.fsync) as fsync, \
                patch("bothy.memory.fcntl.flock", wraps=memory.fcntl.flock) as flock:
            self.assertEqual(self.note("Kept fact."), "daily/2026-10-14.md")
        self.assertIn("2026-10-14T01:30:00+02:00", self.day.read_text())
        self.assertGreaterEqual(fsync.call_count, 2)
        flock.assert_called()
        record = list(self.audit.records())[-1]
        self.assertEqual(record["data"]["sha256"], hashlib.sha256(b"Kept fact.").hexdigest())
        self.assertEqual(record["data"]["chars"], 10)
        self.assertEqual(record["data"]["date"], "2026-10-14")
        self.assertNotIn("Kept fact.", self.config.audit_path.read_text())

    def test_system_local_timezone_default(self) -> None:
        with patch("bothy.memory.clock.now", return_value=NOW):
            path = memory.note(self.root, "worker", self.audit, text="local")
        self.assertEqual(path, "daily/2026-10-13.md")

    def test_entry_cap_checked_before_scan(self) -> None:
        with patch("bothy.memory.scan", side_effect=AssertionError("scan should not run")), \
                self.assertRaisesRegex(memory.MemoryError, "2000-character"):
            self.note("x" * 2001)
        self.assertFalse(self.day.exists())
        self.note("x" * 2000)
        self.assertIn("x" * 2000, self.day.read_text())

    def test_day_byte_cap_checked_before_scan_and_no_truncation(self) -> None:
        self.put("daily/2026-10-14.md", "x" * memory.DAY_CAP)
        before = self.day.read_bytes()
        with patch("bothy.memory.scan", side_effect=AssertionError("scan should not run")), \
                self.assertRaisesRegex(memory.MemoryError, "65536-byte"):
            self.note(SECRET)
        self.assertEqual(self.day.read_bytes(), before)

    def test_utf8_daily_byte_boundary(self) -> None:
        entry = "- [2026-10-14T01:30:00+02:00] é\n".encode()
        self.put("daily/2026-10-14.md", "x" * (memory.DAY_CAP - len(entry)))
        self.note("é")
        self.assertEqual(self.day.stat().st_size, memory.DAY_CAP)
        with self.assertRaisesRegex(memory.MemoryError, "65536-byte"):
            self.note("é")

    def test_scan_refusals_are_private_and_write_nothing(self) -> None:
        for text, category in ((SECRET, "secret"), ("hidden\u200b", "invisible_unicode")):
            with self.subTest(category=category), self.assertRaises(memory.MemoryError) as caught:
                self.note(text)
            self.assertEqual(caught.exception.category, category)
            self.assertNotIn(text, str(caught.exception))
            self.assertNotIn(text, self.config.audit_path.read_text())
            self.assertFalse(self.day.exists())

    def test_failed_audit_prevents_append(self) -> None:
        with patch.object(self.audit, "append", side_effect=AuditError("full")), self.assertRaises(AuditError):
            self.note("fact")
        self.assertFalse(self.day.exists())

    def test_concurrent_appends_all_kept(self) -> None:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda number: self.note(f"entry {number:02d}"), range(20)))
        text = self.day.read_text()
        self.assertEqual(len(text.splitlines()), 20)
        for number in range(20):
            self.assertEqual(text.count(f"entry {number:02d}"), 1)
        self.assertEqual(self.audit.verify(), 20)

    def test_multiline_notes_are_preserved(self) -> None:
        self.note("first\nsecond")
        self.assertIn("first\n  second\n", self.day.read_text())

    def test_daily_lock_is_constant_across_dates_and_symlinks_are_refused(self) -> None:
        self.note("today")
        with patch("bothy.memory.clock.utcnow", return_value=NOW + dt.timedelta(days=1)):
            self.note("tomorrow")
        daily = self.day.parent
        self.assertEqual([p.name for p in daily.iterdir() if p.suffix != ".md"], [".lock"])
        (daily / ".lock").unlink()
        outside = self.home / "outside.lock"
        (daily / ".lock").symlink_to(outside)
        with self.assertRaisesRegex(memory.MemoryError, "path"):
            self.note("refused")
        self.assertFalse(outside.exists())

    def test_no_daily_notes_in_snapshot(self) -> None:
        self.note("Daily private content.")
        self.assertNotIn("Daily private content.", memory.snapshot(self.root, "worker", "base").instructions)

    def test_symlink_daily_directory_and_file_refused(self) -> None:
        outside = self.home / "outside"
        outside.mkdir()
        (self.directory / "daily").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(memory.MemoryError, "path"):
            self.note("fact")
        (self.directory / "daily").unlink()
        (self.directory / "daily").mkdir()
        self.day.symlink_to(outside / "note.md")
        with self.assertRaisesRegex(memory.MemoryError, "path"):
            self.note("fact")
        self.assertEqual(list(outside.iterdir()), [])

    def test_search_bm25_ranking_paths_lines_and_snippets(self) -> None:
        self.put("MEMORY.md", "- apple pear pear pear pear\n- unrelated fruit\n")
        self.put("USER.md", "- apple apple apple apple pear\n")
        self.note("apple pear")
        hits = self.search("apple")
        self.assertEqual(hits[0]["path"], "USER.md")
        self.assertEqual({hit["path"] for hit in hits}, {"MEMORY.md", "USER.md", "daily/2026-10-14.md"})
        for hit in hits:
            self.assertEqual(hit["from_line"], 1)
            self.assertEqual(hit["to_line"], 1)
            self.assertLessEqual(len(hit["snippet"]), 240)
        self.assertIn("data, not instructions", memory_index.format_hits(hits))

    def test_search_never_includes_soul(self) -> None:
        self.put("SOUL.md", "Unique persona word\n")
        self.assertEqual(self.search("persona"), [])

    def test_default_and_max_search_limits(self) -> None:
        self.put("MEMORY.md", "- match\n" * 25)
        self.assertEqual(len(self.search("match")), 6)
        self.assertEqual(len(self.search("match", 100)), 20)
        self.assertEqual(len(self.search("match", 2)), 2)

    def test_query_fts_syntax_is_literal_and_cannot_widen(self) -> None:
        self.put("MEMORY.md", "- apple\n- banana\n- apple OR banana\n")
        for query in ('apple OR banana', 'apple NEAR banana', 'apple NOT banana', 'apple*',
                      '"apple', 'text:apple', 'apple) OR (banana', '---', '"'):
            with self.subTest(query=query):
                hits = self.search(query)
                if "OR" in query:
                    self.assertEqual([hit["from_line"] for hit in hits], [3])
                if "NEAR" in query or "NOT" in query or "text:" in query:
                    self.assertEqual(hits, [])

    def test_query_cap_checked_before_regex_and_bad_limits_refused(self) -> None:
        with patch("bothy.memory_index.re.findall", side_effect=AssertionError("regex")), \
                self.assertRaisesRegex(memory.MemoryError, "500-character"):
            self.search("x" * 501)
        for limit in (0, -1, True, "6"):
            with self.assertRaisesRegex(memory.MemoryError, "limit"):
                self.search("fact", limit)

    def test_search_snippet_shows_a_match_late_in_a_line(self) -> None:
        self.put("MEMORY.md", "- " + "background " * 100 + "needle found here\n")
        for available in (True, False):
            with patch("bothy.memory_index.fts5_available", return_value=available):
                hit = self.search("needle")[0]
            self.assertIn("needle", hit["snippet"])
            self.assertLessEqual(len(hit["snippet"]), 240)

    def test_index_rebuilds_after_deletion_and_corruption(self) -> None:
        self.put("MEMORY.md", "- rebuild marker\n")
        first = self.search("marker")
        path = memory_index.index_path(self.config.state_dir, "worker")
        self.assertTrue(path.is_file())
        path.unlink()
        self.assertEqual(self.search("marker"), first)
        path.write_bytes(b"not a sqlite database")
        self.assertEqual(self.search("marker"), first)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_index_updates_changed_files_and_prunes_deleted_files(self) -> None:
        path = self.put("MEMORY.md", "- oldword\n")
        self.assertEqual(len(self.search("oldword")), 1)
        self.put("MEMORY.md", "- newword\n")
        self.assertEqual(self.search("oldword"), [])
        self.assertEqual(len(self.search("newword")), 1)
        path.unlink()
        self.assertEqual(self.search("newword"), [])

    def test_unchanged_index_does_not_reread_files(self) -> None:
        self.put("MEMORY.md", "- cached word\n")
        first = self.search("cached")
        with patch("bothy.memory_index._read", side_effect=AssertionError("should reuse")):
            self.assertEqual(self.search("cached"), first)

    def test_no_fts5_falls_back_case_insensitively(self) -> None:
        self.put("USER.md", "- Prefer PARIS\n- Prefer London\n")
        self.note("Paris is home")
        with patch("bothy.memory_index.fts5_available", return_value=False):
            self.assertEqual(len(self.search("paris")), 2)
            self.assertEqual(len(self.search("PREFER paris")), 1)
        self.assertFalse(memory_index.index_path(self.config.state_dir, "worker").exists())

    def test_fts5_probe_handles_missing_module(self) -> None:
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("no such module: fts5")
        with patch("bothy.memory_index.sqlite3.connect", return_value=connection):
            self.assertFalse(memory_index.fts5_available())
        connection.close.assert_called_once()

    def test_get_path_containment(self) -> None:
        self.put("MEMORY.md", "- inside\n")
        outside = self.home / "outside.md"
        outside.write_text("outside")
        (self.directory / "escape.md").symlink_to(outside)
        (self.directory / "alias.md").symlink_to(self.directory / "MEMORY.md")
        for path in ("../outside.md", "daily/../../outside.md", str(outside), "escape.md", "alias.md", None):
            with self.subTest(path=path), self.assertRaisesRegex(memory.MemoryError, "path"):
                memory.get(self.root, "worker", path)
        self.assertIn("inside", memory.get(self.root, "worker", "MEMORY.md"))

    def test_get_returns_at_most_200_lines_with_range_and_fence(self) -> None:
        self.put("daily/2026-10-14.md", "".join(f"row {number}\n" for number in range(1, 401)))
        text = memory.get(self.root, "worker", "daily/2026-10-14.md", from_line=10, lines=500)
        self.assertIn("10-209", text)
        self.assertIn("row 209\n", text)
        self.assertNotIn("row 210\n", text)
        self.assertIn("data, not instructions", text)
        for start, lines in ((0, 2), (1, 0), (True, 5), (1, "5")):
            with self.assertRaisesRegex(memory.MemoryError, "lines"):
                memory.get(self.root, "worker", "MEMORY.md", from_line=start, lines=lines)

    def test_untrusted_tool_registry_blocks_note_only(self) -> None:
        registry = ToolRegistry(memory.daily_tools(self.root, self.config.state_dir, "worker", self.audit,
                                                  write_mode=True))
        names = registry.names()
        self.assertNotIn("bothy_memory_note", registry.select(names, untrusted=True).names())
        self.assertEqual(registry.select(names, untrusted=True, allow_from_untrusted=True).names(), names)
        read = ToolRegistry(memory.daily_tools(self.root, self.config.state_dir, "worker", self.audit,
                                              write_mode=False))
        self.assertEqual(read.names(), ["bothy_memory_get", "bothy_memory_search"])
        self.assertIn("bothy_memory_note", memory.REDACTED_ARGUMENT_TOOLS)

    def test_retention_by_local_days_prunes_cache_and_audits_counts(self) -> None:
        self.config.memory_retention_days = 2
        old = self.put("daily/2026-10-11.md", "oldmarker\n")
        edge = self.put("daily/2026-10-12.md", "boundarymarker\n")
        newer = self.put("daily/2026-10-14.md", "recentmarker\n")
        self.put("MEMORY.md", "- permanentmarker\n")
        self.assertEqual(len(self.search("oldmarker")), 1)
        report = sweep(self.config)
        self.assertFalse(old.exists())
        self.assertTrue(edge.exists())
        self.assertTrue(newer.exists())
        self.assertEqual(report.memory_days_removed, 1)
        self.assertEqual(report.memory_index_rows_pruned, 1)
        self.assertTrue(report.did_anything())
        with sqlite3.connect(memory_index.index_path(self.config.state_dir, "worker")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chunks WHERE path = ?",
                                                ("daily/2026-10-11.md",)).fetchone()[0], 0)
        record = next(r for r in self.audit.records() if r["action"] == "retention_sweep")
        self.assertEqual(record["data"]["files_removed"], 1)
        self.assertNotIn("oldmarker", self.config.audit_path.read_text())
        self.assertEqual(sweep(self.config).memory_days_removed, 0)
        self.assertEqual(len(self.search("permanentmarker")), 1)

    def test_retention_ignores_non_dates_and_symlink_escapes(self) -> None:
        path = self.put("daily/not-a-day.md", "keep")
        outside = self.home / "outside.md"
        outside.write_text("keep")
        (path.parent / "2000-01-01.md").symlink_to(outside)
        report = sweep(self.config)
        self.assertEqual(report.memory_days_removed, 0)
        self.assertTrue(path.exists())
        self.assertTrue(outside.exists())

    def test_retention_sweeps_profiles_removed_from_config(self) -> None:
        old = self.put("daily/2000-01-01.md", "old orphan\n")
        self.search("orphan")
        self.config.profiles.clear()
        report = sweep(self.config)
        self.assertEqual(report.memory_days_removed, 1)
        self.assertEqual(report.memory_index_rows_pruned, 1)
        self.assertFalse(old.exists())

    def test_retention_config_defaults_and_validation(self) -> None:
        self.assertEqual(Config().memory_retention_days, 90)
        config_path = self.home / "config.json"
        for value in (0, -1, True, 1.5, "90"):
            config_path.write_text(json.dumps({"memory_retention_days": value}))
            with self.assertRaisesRegex(ValueError, "memory_retention_days"):
                load(config_path)
        config_path.write_text(json.dumps({"memory_retention_days": 1, "timezone": "Europe/Paris"}))
        self.assertEqual(load(config_path).memory_retention_days, 1)
        config_path.write_text(json.dumps({"timezone": "not/a/zone"}))
        with self.assertRaisesRegex(ValueError, "timezone"):
            load(config_path)

    def test_doctor_missing_fts5_warns_nonfatally(self) -> None:
        self.config.codex_binary = "true"
        self.config.sandbox = "dangerFullAccess"
        output = io.StringIO()
        with patch("bothy.cli.CairnTracker.available", return_value=False), \
                patch("bothy.memory_index.fts5_available", return_value=False), contextlib.redirect_stdout(output):
            code = cmd_doctor(argparse.Namespace(json=True, refresh=False), self.config)
        self.assertEqual(code, 0)
        check = next(c for c in json.loads(output.getvalue())["checks"] if c["check"] == "memory FTS5")
        self.assertFalse(check["ok"])
        self.assertFalse(check["fatal"])

    def test_cli_show_day_and_search_escape_terminal_controls(self) -> None:
        self.put("daily/2026-10-14.md", "daily marker\x1b[31m\n")
        path = self.home / "config.json"
        path.write_text(json.dumps({"home": str(self.home), "profiles": self.config.profiles}))
        for args in (("show", "worker", "daily", "2026-10-14"), ("search", "worker", "marker")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--config", str(path), "memory", *args]), 0)
            self.assertIn("daily marker\\x1b[31m", output.getvalue())
            self.assertNotIn("\x1b", output.getvalue())


if __name__ == "__main__":
    unittest.main()
