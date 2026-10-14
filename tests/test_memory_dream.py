"""Scheduled dream grants, private consolidation transactions and recovery."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bothy import clock, codex, memory
from bothy.audit import AuditError, AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.capability import ToolRegistry
from bothy.cli import main
from bothy.config import Config
from bothy.daemon import Daemon
from bothy.pool import Admission
from bothy.proc import ProcessRegistry
from bothy.retention import sweep
from bothy.runner import Runner
from bothy.schedule import Job, Schedule, ScheduleError

NOW = dt.datetime(2026, 10, 14, 1, tzinfo=dt.timezone.utc)
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
TOOL = "bothy_memory_consolidate"


class DreamCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = Config(home=self.home, workspace=self.home / "workspace",
                             budget=BudgetPolicy(mode="api"),
                             codex_credentials_dir=self.home / "credentials",
                             profiles={"worker": {"memory": "write", "tools": []}})
        self.config.ensure_dirs()
        self.root = self.config.memory_dir
        self.directory = self.root / "worker"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "MEMORY.md"
        self.audit = AuditLog(self.config.audit_path)
        self.schedule = Schedule(self.config.state_dir / "jobs.json", profiles=self.config.profiles)

    def put(self, relative, text):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def job(self, **kwargs):
        return Job(id="nightly", kind="cron", spec="0 3 * * *", profile="worker", dream=True, **kwargs)

    def command(self, *args):
        path = self.home / "config.json"
        path.write_text(json.dumps({"home": str(self.home), "workspace": str(self.config.workspace),
                                    "profiles": self.config.profiles}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(["--config", str(path), *args])
        return code, output.getvalue()

    def consolidate(self, text, file="memory"):
        return memory.consolidate(self.root, "worker", file, self.audit, text=text)


class Definitions(DreamCase):
    def test_default_false_and_roundtrip(self):
        self.assertFalse(Job("plain", "every", "3600").dream)
        self.schedule.put(self.job())
        self.assertTrue(self.schedule.get("nightly").dream)

    def test_invalid_memory_profiles_refused_at_add_and_load(self):
        for profile, definition in ((None, {}), ("missing", {}), ("worker", {"memory": "off"}),
                                     ("worker", {"memory": "read"})):
            self.config.profiles["worker"] = definition
            job = self.job()
            job.profile = profile
            with self.subTest(profile=profile, definition=definition):
                with self.assertRaisesRegex(ScheduleError, "job nightly"):
                    self.schedule.put(job)
                self.schedule.path.write_text(json.dumps({"nightly": job.as_dict()}))
                with self.assertRaisesRegex(ScheduleError, "job nightly"):
                    self.schedule.jobs()

    def test_conflicting_jobs_refused_at_add_and_load(self):
        for extra in ({"heartbeat": True}, {"sop": "triage"},
                      {"drove": {"action": "plan", "brief": "go"}}, {"dream": "true"}):
            job = self.job()
            for key, value in extra.items():
                setattr(job, key, value)
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ScheduleError, "job nightly"):
                    self.schedule.put(job)
                self.schedule.path.write_text(json.dumps({"nightly": job.as_dict()}))
                with self.assertRaisesRegex(ScheduleError, "job nightly"):
                    self.schedule.get("nightly")

    def test_cli_add_refusals_name_job(self):
        for flags in (["--heartbeat"], ["--sop", "triage"], ["--drove-from-cairn", "BOTHY-41"]):
            code, output = self.command("schedule", "add", "nightly", "--profile", "worker",
                                        "--dream", *flags)
            self.assertEqual(code, 78)
            self.assertIn("job nightly", output)
        self.config.profiles["worker"]["memory"] = "read"
        code, output = self.command("schedule", "add", "nightly", "--profile", "worker", "--dream")
        self.assertEqual(code, 78)
        self.assertIn("job nightly", output)

    def test_config_load_refuses_invalid_dream(self):
        job = self.job()
        self.schedule.path.write_text(json.dumps({"nightly": job.as_dict()}))
        self.config.profiles["worker"]["memory"] = "read"
        code, output = self.command("status")
        self.assertEqual(code, 78)
        self.assertIn("job nightly", output)

    def test_cli_add_local_cron_and_ignores_prompt(self):
        code, output = self.command("schedule", "add", "nightly", "--profile", "worker", "--dream",
                                    "--kind", "cron", "--spec", "0 3 * * *", "--tz", "Europe/Paris",
                                    "--prompt", "ignored")
        self.assertEqual(code, 0, output)
        job = self.schedule.get("nightly")
        self.assertTrue(job.dream)
        self.assertEqual(job.tz, "Europe/Paris")


class Prompts(DreamCase):
    def prompt(self):
        return memory.dream_prompt(self.root, self.config.state_dir, "worker")[0]

    def test_all_retained_notes_and_curated_files_fenced_with_fixed_instruction(self):
        self.put("MEMORY.md", "- Durable marker\n")
        self.put("USER.md", "- User marker\n")
        self.put("daily/2026-10-11.md", "- Older marker\n")
        self.put("daily/2026-10-14.md", "- Newer marker\n")
        text = self.prompt()
        self.assertIn(memory.DREAM_INSTRUCTION, text)
        for content in ("Durable marker", "User marker", "Older marker", "Newer marker"):
            marker = re.search(r"<<untrusted-([0-9a-f]{16})>>\n[^<]*" + content, text)
            self.assertIsNotNone(marker, content)
        self.assertLess(text.index("Older marker"), text.index("Newer marker"))

    def test_since_cutoff_includes_new_entries_in_same_day_and_wrapped_lines(self):
        memory.mark_dream_success(self.config.state_dir, "worker", clock.iso(NOW))
        self.put("daily/2026-10-14.md",
                 "- [2026-10-14T00:30:00+00:00] old\n  old continuation\n"
                 "- [2026-10-14T03:00:00+01:00] new\n  new continuation\n")
        text = self.prompt()
        self.assertNotIn("old continuation", text)
        self.assertIn("new continuation", text)

    def test_cap_counts_notes_only_and_keeps_newest_across_files(self):
        self.put("daily/2026-10-11.md", "omitted older marker")
        self.put("daily/2026-10-13.md", "A" * 20000)
        self.put("daily/2026-10-14.md", "B" * 10000)
        text = self.prompt()
        self.assertNotIn("omitted older marker", text)
        self.assertEqual(text.count("A"), 14000)
        self.assertEqual(text.count("B" * 10000), 1)
        self.assertIn("Daily notes truncated", text)
        self.assertIn("newest 24000 characters kept", text)
        markers = re.findall(r"(<<untrusted-[0-9a-f]{16}>>)\n(.*?)\n\1", text, re.S)
        self.assertEqual(sum(len(body) for _, body in markers[2:]), memory.DREAM_NOTES_CAP)

    def test_one_large_file_keeps_tail(self):
        self.put("daily/2026-10-14.md", "old prefix" + "x" * 30000 + "newest suffix")
        text = self.prompt()
        self.assertNotIn("old prefix", text)
        self.assertIn("newest suffix", text)
        self.assertIn("Daily notes truncated", text)

    def test_no_notes_or_no_new_notes_returns_none(self):
        self.assertIsNone(self.prompt())
        memory.mark_dream_success(self.config.state_dir, "worker", clock.iso(NOW))
        self.put("daily/2026-10-14.md", "- [2026-10-14T00:00:00Z] already considered\n")
        self.assertIsNone(self.prompt())

    def test_manual_notes_use_mtime(self):
        memory.mark_dream_success(self.config.state_dir, "worker", clock.iso(NOW))
        path = self.put("daily/2026-10-14.md", "manual note\n")
        os.utime(path, (NOW.timestamp() - 1, NOW.timestamp() - 1))
        self.assertIsNone(self.prompt())
        os.utime(path, (NOW.timestamp() + 1, NOW.timestamp() + 1))
        self.assertIn("manual note", self.prompt())


class Transactions(DreamCase):
    def refusal(self, category, text):
        before = self.path.read_bytes() if self.path.exists() else None
        with self.assertRaises(memory.MemoryError) as caught:
            self.consolidate(text)
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        self.assertEqual(memory.preimages(self.root, "worker"), [])
        self.assertNotIn(SECRET, str(caught.exception))

    def test_cap_precedes_scan_and_format(self):
        with patch("bothy.memory.scan", side_effect=AssertionError("must not scan")):
            self.refusal("cap", SECRET + "x" * 8001)

    def test_scan_precedes_format_and_drop_guard(self):
        self.put("MEMORY.md", "- old\n" * 8)
        self.refusal("secret", SECRET)
        self.refusal("invisible_unicode", "bad\u200b")

    def test_format_precedes_drop_guard(self):
        self.put("MEMORY.md", "- old\n" * 8)
        self.refusal("entry", "invalid paragraph")

    def test_drop_guard_threshold_and_small_file_exception(self):
        self.put("MEMORY.md", "- old\n" * 8)
        self.refusal("drop_guard", "- new\n" * 5)
        self.consolidate("- new\n" * 6)
        self.put("MEMORY.md", "- old\n" * 7)
        self.consolidate("- new\n")
        self.put("MEMORY.md", "- old\n" * 9)
        with self.assertRaisesRegex(memory.MemoryError, "drop_guard"):
            self.consolidate("- new\n" * 6)
        self.consolidate("- new\n" * 7)

    def test_file_caps_and_no_soul(self):
        with self.assertRaisesRegex(memory.MemoryError, "cap"):
            self.consolidate("- " + "x" * 2999, file="user")
        with self.assertRaisesRegex(memory.MemoryError, "file"):
            self.consolidate("- persona", file="soul")

    def test_audited_fsynced_preimage_precedes_replace(self):
        before = "- private old marker\n"
        after = "- private new marker\n"
        self.put("MEMORY.md", before)
        original = memory._replace

        def replace(path, body):
            records = list(self.audit.records())
            self.assertEqual(records[-1]["action"], "consolidate")
            if path == self.path:
                saved = memory.preimages(self.root, "worker", "memory")
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0].read_text(), before)
            original(path, body)

        with patch("bothy.memory._replace", side_effect=replace), \
                patch("bothy.memory.os.fsync", wraps=os.fsync) as fsync:
            self.consolidate(after)
        self.assertGreaterEqual(fsync.call_count, 4)
        data = list(self.audit.records())[-1]["data"]
        self.assertEqual(data["before_sha256"], hashlib.sha256(before.encode()).hexdigest())
        self.assertEqual(data["after_sha256"], hashlib.sha256(after.encode()).hexdigest())
        self.assertEqual((data["before_entries"], data["after_entries"]), (1, 1))
        self.assertNotIn("private old marker", self.config.audit_path.read_text())
        self.assertNotIn("private new marker", self.config.audit_path.read_text())

    def test_failed_audit_blocks_replace_and_preimage(self):
        self.put("MEMORY.md", "- original\n")
        with patch.object(self.audit, "append", side_effect=AuditError("disk full")), \
                self.assertRaises(AuditError):
            self.consolidate("- replacement\n")
        self.assertEqual(self.path.read_text(), "- original\n")
        self.assertEqual(memory.preimages(self.root, "worker"), [])

    def test_failed_preimage_blocks_replace(self):
        self.put("MEMORY.md", "- original\n")
        with patch("bothy.memory._preimage", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self.consolidate("- replacement\n")
        self.assertEqual(self.path.read_text(), "- original\n")

    def test_preimage_timestamp_collision_does_not_overwrite(self):
        self.put("MEMORY.md", "- original\n")
        with patch("bothy.memory.clock.utcnow", return_value=NOW):
            self.consolidate("- second\n")
            self.consolidate("- third\n")
        self.assertEqual([p.read_text() for p in memory.preimages(self.root, "worker")],
                         ["- original\n", "- second\n"])

    def test_restore_saves_current_audits_and_operator_can_undo_drop(self):
        self.put("MEMORY.md", "- original\n")
        self.consolidate("- new\n" * 9)
        preimage = memory.preimages(self.root, "worker")[0]
        memory.restore(self.root, "worker", "memory", str(preimage), self.audit)
        self.assertEqual(self.path.read_text(), "- original\n")
        self.assertIn("- new\n" * 9, [p.read_text() for p in memory.preimages(self.root, "worker")])
        self.assertEqual(list(self.audit.records())[-1]["action"], "restore")

    def test_restore_recovers_malformed_current_file_and_preserves_bytes(self):
        preimage = self.put("preimages/MEMORY.md-recover.md", "- recovered\n")
        for before in (b"hand edited paragraph", b"\xff"):
            self.path.write_bytes(before)
            memory.restore(self.root, "worker", "memory", preimage.name, self.audit)
            self.assertEqual(self.path.read_text(), "- recovered\n")
            self.assertIn(before, [p.read_bytes() for p in memory.preimages(self.root, "worker")])

    def test_restore_refuses_outside_traversal_and_symlinks(self):
        self.put("MEMORY.md", "- current\n")
        outside = self.home / "MEMORY.md-outside.md"
        outside.write_text("- outside\n")
        for source in (str(outside), "../" + outside.name, "preimages/" + outside.name):
            with self.assertRaisesRegex(memory.MemoryError, "path"):
                memory.restore(self.root, "worker", "memory", source, self.audit)
        link = self.put("preimages/MEMORY.md-link.md", "")
        link.unlink()
        link.symlink_to(outside)
        with self.assertRaisesRegex(memory.MemoryError, "path"):
            memory.restore(self.root, "worker", "memory", link.name, self.audit)
        self.assertEqual(self.path.read_text(), "- current\n")

    def test_restore_applies_cap_scan_and_format(self):
        self.put("MEMORY.md", "- current\n")
        for text, category in (("x" * 8001, "cap"), (SECRET, "secret"), ("paragraph", "entry")):
            path = self.put("preimages/MEMORY.md-test.md", text)
            with self.assertRaisesRegex(memory.MemoryError, category):
                memory.restore(self.root, "worker", "memory", path.name, self.audit)
            self.assertEqual(self.path.read_text(), "- current\n")

    def test_cli_preimages_and_restore(self):
        self.put("MEMORY.md", "- original\n")
        self.consolidate("- current\n")
        preimage = memory.preimages(self.root, "worker")[0]
        code, output = self.command("memory", "preimages", "worker")
        self.assertEqual(code, 0, output)
        self.assertIn(preimage.name, output)
        self.assertNotIn("original", output)
        code, output = self.command("memory", "restore", "worker", "memory", preimage.name)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.path.read_text(), "- original\n")

    def test_retention_keeps_newest_14_per_file_without_daily_directory(self):
        for file in ("memory", "user"):
            for index in range(17):
                self.put(f"preimages/{memory.FILES[file]}-202610{index:02d}T010000.000000Z.md", str(index))
        report = sweep(self.config)
        self.assertEqual(report.memory_preimages_removed, 6)
        self.assertTrue(report.did_anything())
        for file in ("memory", "user"):
            saved = memory.preimages(self.root, "worker", file)
            self.assertEqual(len(saved), 14)
            self.assertEqual([p.read_text() for p in saved], [str(n) for n in range(3, 17)])
        records = [r for r in self.audit.records() if r["action"] == "preimage_retention"]
        self.assertEqual([r["data"]["files_removed"] for r in records], [3, 3])
        self.assertEqual(sweep(self.config).memory_preimages_removed, 0)

    def test_retention_audit_failure_leaves_preimages(self):
        for index in range(16):
            self.put(f"preimages/MEMORY.md-{index:02d}.md", "- fact\n")
        with patch("bothy.retention.AuditLog.append", side_effect=AuditError("disk full")):
            report = sweep(self.config)
        self.assertTrue(report.errors)
        self.assertEqual(len(memory.preimages(self.root, "worker")), 16)

    def test_untrusted_block_and_argument_redaction(self):
        tool = memory.consolidate_tool(self.root, "worker", self.audit, run_id="r", successful=set())
        self.assertEqual(ToolRegistry([tool]).select([TOOL], untrusted=True).names(), [])
        self.assertIn(TOOL, memory.REDACTED_ARGUMENT_TOOLS)
        self.assertNotIn(SECRET, str(memory.redacted_arguments({"file": "memory", "text": SECRET})))


class Runs(DreamCase):
    def setUp(self):
        super().setUp()
        self.admission = Admission(self.config.pool, BudgetLedger(self.config.budget_path, self.config.budget))
        self.processes = ProcessRegistry(self.config.registry_path)
        self.alerter = Mock()
        self.runner = Runner(self.config, admission=self.admission, audit=self.audit,
                             registry=self.processes, alerter=self.alerter)
        self.started = []
        self.turns = []
        self.sent = []
        self.actions = []
        self.outcome = "completed"
        self.after_call = lambda: None
        outer = self

        class Stub(codex.CodexClient):
            def start(self):
                self._popen = SimpleNamespace(pid=12345)
                return self._popen

            def handshake(self):
                return {}

            def start_thread(self, **kwargs):
                outer.started.append(kwargs)
                return "thread"

            def run_turn(self, **kwargs):
                outer.turns.append(kwargs)
                for i, args in enumerate(outer.actions):
                    self._handle_server_request({"id": i, "method": "item/tool/call", "params": {
                        "threadId": "thread", "turnId": "turn", "tool": TOOL, "arguments": args}})
                    outer.after_call()
                return codex.TurnOutcome(status=outer.outcome, messages=["private result"])

            def _send(self, payload):
                outer.sent.append(payload)

            def close(self, **kwargs):
                return "terminated"

        for stub in (patch("bothy.runner.codex.CodexClient", Stub), patch.object(self.processes, "register")):
            stub.start()
            self.addCleanup(stub.stop)
        self.daemon = Daemon(self.config, runner=self.runner, admission=self.admission, audit=self.audit,
                             registry=self.processes, alerter=self.alerter, routes=[])

    def note(self, text="new note"):
        return self.put("daily/2026-10-14.md", f"- [2026-10-14T00:00:00Z] {text}\n")

    def execute(self, **kwargs):
        return self.runner.run(subject="dream", prompt="must ignore this", profile="worker", **kwargs)

    def names(self):
        return [s["name"] for s in self.started[-1]["dynamic_tools"] or []]

    def test_dream_tool_granted_without_profile_listing_and_prompt_ignored(self):
        self.note()
        self.assertTrue(self.execute(dream=True).ok)
        self.assertIn(TOOL, self.names())
        self.assertNotIn("must ignore this", self.turns[-1]["text"])
        self.assertIn(memory.DREAM_INSTRUCTION, self.turns[-1]["text"])

    def test_tool_never_offered_on_non_dream_even_if_profile_lists_it(self):
        self.config.profiles["worker"]["tools"] = [TOOL]
        for kwargs in ({}, {"heartbeat": True}, {"untrusted": True}):
            self.assertTrue(self.execute(**kwargs).ok)
            self.assertNotIn(TOOL, self.names())

    def test_webhook_dream_spoof_does_not_grant_tool(self):
        self.config.profiles["worker"]["tools"] = [TOOL]
        self.schedule.put(self.job())
        self.note()
        self.daemon.store.append({"id": "spoof", "route": "/hook", "profile": "worker",
                                  "subject": "hook", "dream": True,
                                  "payload": {"dream": True, "job": "nightly", "route": "schedule:nightly"},
                                  "processed": False})
        with patch.object(self.runner, "run", wraps=self.runner.run) as run:
            self.assertEqual(self.daemon.drain(), 1)
        self.assertFalse(run.call_args.kwargs["dream"])
        self.assertNotIn(TOOL, self.names())

    def test_schedule_grant_comes_from_stored_job_and_no_prompt_saved_in_queue(self):
        job = self.schedule.put(self.job(prompt="ignore me"))
        self.note()
        wake = self.daemon.fire(job)
        event = next(e for e in self.daemon.store.all_events() if e["id"] == wake)
        self.assertEqual(event["prompt"], "")
        self.assertEqual(self.daemon.drain(), 1)
        self.assertIn(TOOL, self.names())
        self.assertNotIn("ignore me", self.turns[-1]["text"])

    def test_invalid_dream_refused_advances_and_does_not_block_other_due_job(self):
        due = clock.iso(NOW - dt.timedelta(seconds=1))
        dream_job = self.job(next_due_at=due, prompt="private job prompt")
        self.schedule.put(dream_job)
        self.schedule.put(Job(id="other", kind="every", spec="3600",
                              prompt="ordinary job", next_due_at=due))
        self.config.profiles["worker"]["memory"] = "read"
        passes = []

        def after_pass(timeout):
            passes.append(timeout)
            if len(passes) == 2:
                self.daemon._stopping.set()

        with patch("bothy.schedule.clock.utcnow", return_value=NOW), \
                patch.object(self.daemon._stopping, "wait", side_effect=after_pass), \
                patch.object(self.daemon, "fire", wraps=self.daemon.fire) as fire:
            self.daemon._schedule_loop()
        self.assertEqual([call.args[0].id for call in fire.call_args_list], ["nightly", "other"])
        jobs = json.loads(self.schedule.path.read_text())
        for job in jobs.values():
            self.assertEqual(job["last_fired_at"], clock.iso(NOW))
            self.assertGreater(clock.parse(job["next_due_at"]), NOW)
        events = self.daemon.store.all_events()
        self.assertEqual([event["payload"]["job"] for event in events], ["other"])
        refused = [record for record in self.audit.records() if record["action"] == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["kind"], "schedule")
        self.assertEqual(refused[0]["data"]["gate"], "dream_invalid")
        self.assertEqual(refused[0]["data"]["job"], "nightly")
        self.assertNotIn("private job prompt", self.config.audit_path.read_text())
        self.alerter.incident.assert_called_once()
        self.assertEqual(self.alerter.incident.call_args.kwargs["job"], "dream:nightly")
        self.assertEqual(self.started, [])

    def test_skip_no_codex_no_budget_probe_and_schedule_advances(self):
        self.config.budget = BudgetPolicy(mode="subscription")
        job = self.schedule.put(self.job())
        before = job.next_due_at
        self.daemon.fire(job)
        self.schedule.mark_fired(job.id, moment=clock.parse(before) + dt.timedelta(seconds=300))
        with patch.object(self.runner, "rate_limit_position", side_effect=AssertionError("must not probe")):
            self.assertEqual(self.daemon.drain(), 1)
        self.assertEqual(self.started, [])
        self.assertNotEqual(self.schedule.get(job.id).next_due_at, before)
        self.assertIn("dream_skipped", [r["action"] for r in self.audit.records()])
        self.assertIsNone(memory.last_dream_at(self.config.state_dir, "worker"))

    def test_last_dream_only_after_completed_success_and_no_text_in_audit(self):
        self.note()
        self.actions = [{"file": "memory", "text": "- private written marker\n"}]
        with patch("bothy.memory.clock.utcnow", return_value=NOW):
            result = self.execute(dream=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.messages, [])
        self.assertEqual(memory.last_dream_at(self.config.state_dir, "worker"), clock.iso(NOW))
        self.assertNotIn("private written marker", self.config.audit_path.read_text())

    def test_last_dream_unchanged_without_success_or_after_failed_run(self):
        self.note()
        self.execute(dream=True)
        self.assertIsNone(memory.last_dream_at(self.config.state_dir, "worker"))
        self.actions = [{"file": "memory", "text": "bad paragraph"}]
        self.execute(dream=True)
        self.assertIsNone(memory.last_dream_at(self.config.state_dir, "worker"))
        self.actions = [{"file": "memory", "text": "- valid fact\n"}]
        self.outcome = "failed"
        self.execute(dream=True)
        self.assertIsNone(memory.last_dream_at(self.config.state_dir, "worker"))

    def test_failed_write_audit_does_not_mark_success(self):
        self.note()
        self.actions = [{"file": "memory", "text": "- valid fact\n"}]
        append = self.audit.append

        def fail(**kwargs):
            if kwargs.get("action") == "consolidate":
                raise AuditError("disk full")
            return append(**kwargs)

        with patch.object(self.audit, "append", side_effect=fail):
            self.execute(dream=True)
        self.assertFalse(self.path.exists())
        self.assertIsNone(memory.last_dream_at(self.config.state_dir, "worker"))

    def test_tool_once_per_file_and_user_success_counts(self):
        self.note()
        self.actions = [{"file": "user", "text": "- first\n"},
                        {"file": "user", "text": "- second\n"}]
        self.execute(dream=True)
        self.assertEqual((self.directory / "USER.md").read_text(), "- first\n")
        self.assertIsNotNone(memory.last_dream_at(self.config.state_dir, "worker"))
        self.assertFalse(self.sent[-1]["result"]["success"])

    def test_notes_appended_during_run_remain_for_next_dream(self):
        path = self.note()
        self.actions = [{"file": "memory", "text": "- durable\n"}]
        self.after_call = lambda: path.write_text(
            path.read_text() + "- [2026-10-14T01:01:00Z] arrived during run\n")
        with patch("bothy.memory.clock.utcnow", return_value=NOW):
            self.execute(dream=True)
        prompt, _ = memory.dream_prompt(self.root, self.config.state_dir, "worker")
        self.assertIn("arrived during run", prompt)
        self.assertNotIn("new note", prompt)

    def test_invalid_state_refuses_before_codex_without_quoting_it(self):
        path = memory.dream_state_path(self.config.state_dir, "worker")
        path.parent.mkdir()
        for value in (SECRET, 5, None):
            path.write_text(json.dumps({"last_dream_at": value}))
            result = self.execute(dream=True)
            self.assertEqual(result.status, "refused")
            self.assertEqual(self.started, [])
            self.assertNotIn(SECRET, str(result.as_dict()))
            self.assertNotIn(SECRET, self.config.audit_path.read_text())

    def test_state_atomic_fsync_and_monotonic(self):
        with patch("bothy.memory.os.fsync", wraps=os.fsync) as fsync:
            memory.mark_dream_success(self.config.state_dir, "worker", clock.iso(NOW))
        self.assertGreaterEqual(fsync.call_count, 2)
        memory.mark_dream_success(self.config.state_dir, "worker", clock.iso(NOW - dt.timedelta(days=1)))
        self.assertEqual(memory.last_dream_at(self.config.state_dir, "worker"), clock.iso(NOW))
