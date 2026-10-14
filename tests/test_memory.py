"""Persona is authority; curated memory is bounded, audited data."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bothy import codex, memory
from bothy.audit import AuditError, AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.capability import Profile, ToolRegistry
from bothy.cli import cmd_doctor, cmd_memory, main
from bothy.config import Config, load
from bothy.daemon import Daemon
from bothy.pool import Admission
from bothy.proc import ProcessRegistry
from bothy.runner import Runner
from bothy.schedule import Job

MARKER = re.compile(r"<<untrusted-[0-9a-f]{16}>>")
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


class MemoryCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.config = Config(home=self.home, workspace=self.home / "workspace",
                             budget=BudgetPolicy(mode="api"),
                             codex_credentials_dir=self.home / "credentials",
                             profiles={"worker": {"memory": "write", "tools": ["bothy_memory"],
                                                  "developer_instructions": "Profile instructions."}})
        self.config.ensure_dirs()
        self.audit = AuditLog(self.config.audit_path)
        self.path = self.config.memory_dir / "worker" / "MEMORY.md"
        self.path.parent.mkdir(parents=True)

    def write(self, action: str = "add", file: str = "memory", **kwargs) -> int:
        return memory.write(self.config.memory_dir, "worker", file, self.audit, action=action, **kwargs)

    def records(self) -> list[dict]:
        return [r for r in self.audit.records() if r["kind"] == "memory"]

    def refused(self, category: str, **kwargs) -> str:
        before = self.path.read_bytes() if self.path.exists() else None
        with self.assertRaises(memory.MemoryError) as caught:
            self.write(**kwargs)
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        record = self.records()[-1]
        self.assertEqual((record["action"], record["status"]), ("refused", "refused"))
        self.assertEqual(record["data"]["refusal_category"], category)
        return str(caught.exception)


class Writes(MemoryCase):
    def test_add_replace_remove_and_exact_wrapped_entry(self) -> None:
        self.write(text="Prefer the Paris timezone.\n  Also record UTC.")
        self.write(text="Deployment is manual.")
        self.write(action="replace", old_text="Prefer the Paris timezone.\n  Also record UTC.",
                   text="Prefer UTC.")
        self.assertEqual(self.path.read_text(), "- Prefer UTC.\n- Deployment is manual.\n")
        self.write(action="remove", old_text="Deployment is manual.")
        self.assertEqual(self.path.read_text(), "- Prefer UTC.\n")
        self.write(action="remove", old_text="Prefer UTC.")
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual([r["action"] for r in self.records()], ["add", "add", "replace", "remove", "remove"])

    def test_zero_match_and_nonexact_match_are_refused(self) -> None:
        self.path.write_text("- Exact body.\n")
        for action in ("replace", "remove"):
            for old in (None, "Exact", "Exact body. "):
                with self.subTest(action=action, old=old):
                    self.assertIn("0 entries", self.refused("match", action=action, old_text=old, text="new"))

    def test_several_matches_are_refused(self) -> None:
        self.path.write_text("- duplicate\n- duplicate\n")
        for action in ("replace", "remove"):
            self.assertIn("2 entries", self.refused("match", action=action, old_text="duplicate", text="new"))

    def test_cap_boundary_and_overflow_never_truncate(self) -> None:
        for file, cap in (("memory", 8000), ("user", 3000)):
            with self.subTest(file=file):
                # Count includes the bullet prefix and final newline.
                self.write(file=file, text="é" * (cap - 3))
                path = self.path.parent / memory.FILES[file]
                original = path.read_bytes()
                self.assertEqual(len(path.read_text()), cap)
                reason = self.refused("cap", file=file, text="overflow")
                self.assertIn(f"over the {cap}-character cap; consolidate entries with replace or remove, then retry", reason)
                self.assertEqual(path.read_bytes(), original)

    def test_consolidating_an_oversized_file_is_possible(self) -> None:
        self.path.write_text("- " + "x" * 8000 + "\n")
        self.write(action="replace", old_text="x" * 8000, text="Consolidated.")
        self.assertEqual(self.path.read_text(), "- Consolidated.\n")

    def test_operator_soul_cap_is_bytes_and_verbatim(self) -> None:
        text = "é" * (memory.CAPS["soul"] // 2)
        self.write(file="soul", action="replace", operator=True, text=text)
        path = self.path.parent / "SOUL.md"
        self.assertEqual(path.read_bytes(), text.encode())
        self.refused("cap", file="soul", action="replace", operator=True, text=text + "é")
        self.assertEqual(path.read_bytes(), text.encode())

    def test_tool_cannot_write_soul(self) -> None:
        self.refused("file", file="soul", text="persona")
        self.assertFalse((self.path.parent / "SOUL.md").exists())

    def test_one_entry_only_and_invalid_actions(self) -> None:
        for text in ("", None, 5, "first\n- second", "first\nunindented", "first\n"):
            with self.subTest(text=text):
                self.refused("entry", text=text)
        self.refused("action", action="set", text="valid")
        self.refused("file", file="../../SOUL.md", text="valid")

    def test_secret_categories_never_quote_input_or_audit_text(self) -> None:
        for text in (SECRET, "xoxb-" + "12345678901-abcdefghijk", "xoxp-" + "12345678901-abcdefghijk",
                     "ghp_" + "a" * 36, "AKIA" + "A" * 16,
                     "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----",
                     "-----BEGIN PGP PRIVATE KEY BLOCK-----",
                     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
                     "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxMjM0NTY3ODkwIn0.",
                     "token: J9tpL4rVy2Wq8aBc6E0zHsXmN3dFgU5i", "password J9tpL4rVy2Wq8aBc6E0zHsXmN3dFgU5i",
                     "secret: 123456789098765432101357902468"):
            with self.subTest(text=text):
                reason = self.refused("secret", text=text)
                self.assertNotIn(text, reason)
                self.assertNotIn(text, self.config.audit_path.read_text())

    def test_invisible_and_bidi_controls_are_refused(self) -> None:
        for char in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u034f",
                     *map(chr, range(0x202A, 0x202F)), *map(chr, range(0x2066, 0x206A))):
            with self.subTest(codepoint=ord(char)):
                self.refused("invisible_unicode", text="before" + char + "after")

    def test_all_control_and_hidden_categories_are_refused(self) -> None:
        points = [p for p in range(0x20) if p not in (9, 10)] + list(range(0x7F, 0xA0)) + [
            0x2028, 0x2029, 0xE000, 0xD800, 0x0378, 0x3164, 0x2800, 0x115F, 0x1160, 0xFFA0]
        for point in points:
            with self.subTest(point=point):
                self.refused("invisible_unicode", text="before" + chr(point) + "after")
        self.write(text="Line.\n\tContinuation.")

    def test_emoji_zwj_and_vs16_require_preceding_symbol(self) -> None:
        text = "Heart \u2764\ufe0f and family \U0001f469\u200d\U0001f467."
        self.write(text=text)
        self.assertEqual(memory.entries(self.path.read_text()), [text])
        for value in ("a\u200d", "a\ufe0f", "\u200d", "\ufe0f", "\u2800\u200d"):
            self.refused("invisible_unicode", text=value)

    def test_additional_secret_shapes_and_short_labelled_passwords(self) -> None:
        for prefix in ("github_pat_", "gho_", "ghu_", "ghs_", "ghr_", "sk_live_", "rk_live_",
                       "AIza", "glpat-", "npm_", "xoxa-", "xapp-"):
            with self.subTest(prefix=prefix):
                self.refused("secret", text=prefix + "a1B2c3D4e5F6g7H8i9J0k1L2")
        for value in ("Bearer abc123token", "https://username:password@example.test/path",
                      "aws_secret=" + "a" * 40, "AWS_SECRET_ACCESS_KEY: " + "a" * 40,
                      "ASIA" + "A" * 16 + " " + "a" * 40,
                      "AKIA" + "A" * 16 + " " + "a" * 40):
            self.refused("secret", text=value)
        for label in ("password", "passwd", "secret", "token", "api_key"):
            for delimiter in (":", "="):
                self.refused("secret", text=label + delimiter + " hunter2")

    def test_cap_checks_precede_any_scan_for_text_and_old_text(self) -> None:
        for args in ({"text": "a-" * 50000}, {"action": "replace", "old_text": "a-" * 50000, "text": "new"}):
            with patch("bothy.memory.scan", side_effect=AssertionError("scan ran before cap")) as scan:
                self.refused("cap", **args)
                scan.assert_not_called()

    def test_adversarial_hyphenated_scan_finishes_within_bound(self) -> None:
        result = subprocess.run([sys.executable, "-c", "from bothy.memory import scan; scan('a-' * 500000)"],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_ordinary_unicode_labels_and_web_addresses_pass_scan(self) -> None:
        memory.scan("Cal préfère Paris. API key rotation is manual. password: use the vault. www.example.com")
        self.write(text="Cal préfère Paris.")

    def test_remove_scans_any_supplied_text_too(self) -> None:
        self.write(text="kept")
        self.refused("secret", action="remove", old_text="kept", text=SECRET)

    def test_audit_before_fsync_and_atomic_replace_with_hashes(self) -> None:
        self.path.write_bytes(b"- before\n")
        before = self.path.read_bytes()
        calls = []
        append = self.audit.append
        replace = memory.os.replace
        fsync = memory.os.fsync

        def recorded(**kw):
            self.assertEqual(self.path.read_bytes(), before)
            calls.append("audit")
            return append(**kw)

        def synced(fd):
            calls.append("directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync")
            return fsync(fd)

        def replaced(source, target):
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(source).read_bytes(), b"- after\n")
            calls.append("replace")
            return replace(source, target)

        with patch.object(self.audit, "append", side_effect=recorded), \
                patch("bothy.memory.os.fsync", side_effect=synced), \
                patch("bothy.memory.os.replace", side_effect=replaced):
            self.write(action="replace", old_text="before", text="after")
        self.assertEqual(calls[0], "audit")
        self.assertLess(calls.index("fsync"), calls.index("replace"))
        self.assertEqual(calls[-1], "directory_fsync")
        data = self.records()[-1]["data"]
        self.assertEqual(data["before_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(data["after_sha256"], hashlib.sha256(b"- after\n").hexdigest())
        self.assertEqual((data["before_chars"], data["after_chars"]), (9, 8))
        self.assertEqual(data["profile"], "worker")
        self.assertEqual(data["file"], "memory")
        self.audit.verify()

    def test_failed_audit_blocks_write(self) -> None:
        self.path.write_bytes(b"- original\n")
        with patch.object(self.audit, "append", side_effect=AuditError("disk full")), \
                patch("bothy.memory.os.replace") as replace:
            with self.assertRaises(AuditError):
                self.write(text="new")
            replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"- original\n")

    def test_operator_can_repair_invalid_utf8(self) -> None:
        self.path.write_bytes(b"\xff")
        self.write(action="replace", operator=True, text="- recovered\n")
        self.assertEqual(self.path.read_text(), "- recovered\n")

    def test_failed_atomic_replace_cleans_temporary_file(self) -> None:
        self.path.write_bytes(b"- original\n")
        with patch("bothy.memory.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.write(text="new")
        self.assertEqual(self.path.read_bytes(), b"- original\n")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(self.records()[-1]["status"], "pending")

    def test_concurrent_adds_do_not_lose_entries(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.write(text=f"entry {i}"), range(12)))
        self.assertEqual(set(memory.entries(self.path.read_text())), {f"entry {i}" for i in range(12)})
        self.assertEqual(self.audit.verify(), 12)

    def test_symlink_escape_is_refused(self) -> None:
        self.path.symlink_to(self.home / "outside")
        self.refused("path", text="new")
        self.assertFalse((self.home / "outside").exists())


class Profiles(MemoryCase):
    def test_modes_and_defaults(self) -> None:
        default = Profile.from_dict("legacy profile", {})
        self.assertEqual(default.memory, "off")
        self.assertFalse(default.memory_on_heartbeat)
        for value in ("off", "read", "write"):
            self.assertEqual(Profile.from_dict("a_1-2", {"memory": value}).memory, value)
        Profile.from_dict("a" * 64, {"memory": "read"})

    def test_invalid_name_mode_or_heartbeat_flag_exits_78_naming_profile(self) -> None:
        for name, raw in [(name, {"memory": "read"}) for name in
                          ("../x", "a/b", "A", "", "-x", "a" * 65, "a\n") ] + [
                              ("worker", {"memory": value}) for value in ("on", None, True, [])] + [
                              ("worker", {"memory_on_heartbeat": value}) for value in ("false", 1, None)]:
            with self.subTest(name=name, raw=raw):
                path = self.home / "config.json"
                path.write_text(json.dumps({"profiles": {name: raw}}))
                with self.assertRaisesRegex(ValueError, "profile"):
                    load(path)
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    self.assertEqual(main(["--config", str(path), "memory", "list"]), 78)
                self.assertIn("profile " + name, error.getvalue())

    def test_enabled_memory_cannot_live_in_workspace_even_through_symlink(self) -> None:
        self.config.workspace = self.home
        with self.assertRaisesRegex(ValueError, "profile worker: memory directory must be outside"):
            self.config.validate_profiles()
        self.config.workspace = self.home / "workspace"
        self.config.workspace.mkdir()
        self.path.unlink(missing_ok=True)
        self.path.parent.rmdir()
        self.config.memory_dir.rmdir()
        self.config.memory_dir.symlink_to(self.config.workspace, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "profile worker: memory directory must be outside"):
            self.config.validate_profiles()
        self.config.profiles["worker"]["memory"] = "off"
        self.config.validate_profiles()


class Runs(MemoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.alerter = Mock()
        self.admission = Admission(self.config.pool, BudgetLedger(self.config.budget_path, self.config.budget))
        self.processes = ProcessRegistry(self.config.registry_path)
        self.runner = Runner(self.config, admission=self.admission, audit=self.audit,
                             registry=self.processes, alerter=self.alerter)
        self.started = []
        self.turns = []
        self.calls = []
        self.sent = []
        self.during_handshake = lambda: None
        self.during_turn = lambda: None
        self.after_tool_call = lambda client: None
        outer = self

        class Stub(codex.CodexClient):
            def start(self):
                outer.calls.append("start")
                self._popen = SimpleNamespace(pid=12345)
                return self._popen

            def handshake(self):
                outer.during_handshake()
                return {}

            def start_thread(self, **kwargs):
                outer.started.append(kwargs)
                return "thread-1"

            def run_turn(self, **kwargs):
                outer.turns.append(kwargs)
                outer.during_turn()
                for i, arguments in enumerate(outer.tool_calls):
                    self._handle_server_request({"id": i, "method": "item/tool/call", "params": {
                        "threadId": "thread-1", "turnId": "turn-1", "tool": outer.tool_name, "arguments": arguments}})
                    outer.after_tool_call(self)
                return codex.TurnOutcome(status="completed", turn_id="turn-1", messages=["done"])

            def _send(self, payload):
                outer.sent.append(payload)

            def close(self, **kwargs):
                return "terminated"

        self.tool_calls = []
        self.tool_name = "bothy_memory"
        for stub in (patch("bothy.runner.codex.CodexClient", Stub), patch.object(self.processes, "register")):
            stub.start()
            self.addCleanup(stub.stop)

    def files(self) -> dict[str, bytes]:
        original = {"SOUL.md": b"  Be direct.\n\n", "MEMORY.md": b"- Prefer UTC.\n",
                    "USER.md": "- Cal préfère Paris.\n".encode()}
        for name, raw in original.items():
            (self.path.parent / name).write_bytes(raw)
        return original

    def execute(self, **kwargs):
        return self.runner.run(subject="memory-test", prompt="Original prompt.\n", profile="worker", **kwargs)

    def injected(self) -> str:
        return self.started[-1]["developer_instructions"]

    def test_injection_order_fencing_and_audit_hashes(self) -> None:
        original = self.files()
        self.assertTrue(self.execute().ok)
        text = self.injected()
        self.assertTrue(text.startswith("Profile instructions.\n\nPersona\n" + original["SOUL.md"].decode()))
        self.assertLess(text.index("Persona"), text.index(memory.NOTES_HEADING))
        markers = MARKER.findall(text)
        self.assertEqual(len(markers), 2)
        self.assertEqual(markers[0], markers[1])
        fenced = text.split(markers[0])[1]
        self.assertIn(original["MEMORY.md"].decode(), fenced)
        self.assertIn(original["USER.md"].decode(), fenced)
        record = next(r for r in self.audit.records() if r["action"] == "memory_loaded")
        self.assertEqual(record["data"]["files"], [{"file": name, "sha256": hashlib.sha256(raw).hexdigest(),
                                                   "bytes": len(raw)} for name, raw in original.items()])
        self.assertEqual(self.turns[-1]["text"], "Original prompt.\n")

    def test_read_profile_injects_but_does_not_grant_tool(self) -> None:
        self.files()
        self.config.profiles["worker"]["memory"] = "read"
        self.assertTrue(self.execute().ok)
        self.assertIn("Persona", self.injected())
        self.assertIsNone(self.started[-1]["dynamic_tools"])

    def test_full_notes_are_never_truncated_by_fence_default_limit(self) -> None:
        self.path.write_text("- " + "x" * 7997 + "\n")
        (self.path.parent / "USER.md").write_text("- " + "y" * 2997 + "\n")
        self.assertTrue(self.execute().ok)
        self.assertIn("x" * 7997, self.injected())
        self.assertIn("y" * 2997, self.injected())
        self.assertNotIn("Payload truncated", self.injected())

    def test_snapshot_is_frozen_before_spawn_and_not_reloaded_by_writes(self) -> None:
        self.files()
        self.during_handshake = lambda: self.path.write_text("- Changed during startup.\n")
        self.during_turn = lambda: self.write(text="Changed during turn.")
        with patch("bothy.memory.read_file", wraps=memory.read_file) as read:
            self.assertTrue(self.execute().ok)
        self.assertEqual(read.call_count, 3)
        self.assertIn("Prefer UTC.", self.injected())
        self.assertNotIn("Changed", self.injected())

    def test_heartbeat_skips_notes_and_opt_in_restores_them(self) -> None:
        self.files()
        self.assertTrue(self.execute(heartbeat=True).ok)
        self.assertIn("Persona", self.injected())
        self.assertNotIn(memory.NOTES_HEADING, self.injected())
        loaded = [r for r in self.audit.records() if r["action"] == "memory_loaded"][-1]
        self.assertEqual([f["file"] for f in loaded["data"]["files"]], ["SOUL.md"])
        self.config.profiles["worker"]["memory_on_heartbeat"] = True
        self.assertTrue(self.execute(heartbeat=True).ok)
        self.assertIn("MEMORY.md", self.injected())
        self.assertIn("USER.md", self.injected())

    def test_heartbeat_does_not_even_read_invalid_notes(self) -> None:
        self.path.write_bytes(b"\xff")
        self.assertTrue(self.execute(heartbeat=True).ok)
        self.assertEqual(self.injected(), "Profile instructions.")

    def test_soul_invalid_is_refused_audited_alerted_without_spawn_or_truncation(self) -> None:
        path = self.path.parent / "SOUL.md"
        raw = b"x" * (memory.CAPS["soul"] + 1)
        path.write_bytes(raw)
        result = self.execute()
        self.assertEqual((result.status, result.refusal["gate"]), ("refused", "soul_invalid"))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.calls, [])
        self.alerter.incident.assert_called_once()
        record = next(r for r in self.audit.records() if r["action"] == "refused")
        self.assertEqual(record["data"]["gate"], "soul_invalid")
        self.assertEqual(self.admission.snapshot()["in_flight"], 0)

    def test_unreadable_and_invalid_utf8_soul_are_refused(self) -> None:
        path = self.path.parent / "SOUL.md"
        path.mkdir()
        self.assertEqual(self.execute().refusal["gate"], "soul_invalid")
        path.rmdir()
        path.write_bytes(b"\xff")
        self.assertEqual(self.execute().refusal["gate"], "soul_invalid")
        path.write_text("valid")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertEqual(self.execute().refusal["gate"], "soul_invalid")
        self.assertEqual(self.calls, [])

    def test_missing_or_empty_soul_is_optional(self) -> None:
        for raw in (None, b"", b"  \n"):
            with self.subTest(raw=raw):
                if raw is not None:
                    (self.path.parent / "SOUL.md").write_bytes(raw)
                self.assertTrue(self.execute().ok)
                self.assertEqual(self.injected(), "Profile instructions.")

    def test_off_and_no_profile_leave_instructions_and_prompt_unchanged(self) -> None:
        self.files()
        self.config.profiles["worker"]["memory"] = "off"
        with patch("bothy.memory.snapshot", side_effect=AssertionError("should not load")):
            self.assertTrue(self.execute().ok)
            self.assertEqual(self.injected(), "Profile instructions.")
            self.assertIsNone(self.started[-1]["dynamic_tools"])
            self.assertTrue(self.runner.run(subject="no-profile", prompt="Original prompt.\n").ok)
            self.assertIsNone(self.injected())
        self.assertEqual([turn["text"] for turn in self.turns], ["Original prompt.\n"] * 2)
        self.assertFalse(any(r["action"] == "memory_loaded" for r in self.audit.records()))

    def test_profile_must_list_tool_and_untrusted_runs_must_opt_in(self) -> None:
        self.assertTrue(self.execute(untrusted=True).ok)
        self.assertIsNone(self.started[-1]["dynamic_tools"])
        self.config.profiles["worker"]["allow_from_untrusted"] = True
        self.assertTrue(self.execute(untrusted=True).ok)
        self.assertEqual([s["name"] for s in self.started[-1]["dynamic_tools"]], ["bothy_memory"])
        self.config.profiles["worker"]["tools"] = []
        self.assertTrue(self.execute().ok)
        self.assertIsNone(self.started[-1]["dynamic_tools"])

    def test_registry_untrusted_block_contains_memory(self) -> None:
        registry = ToolRegistry([memory.dynamic_tool(self.config.memory_dir, "worker", self.audit)])
        self.assertFalse(registry.select(["bothy_memory"], untrusted=True))
        self.assertTrue(registry.select(["bothy_memory"], untrusted=True, allow_from_untrusted=True))

    def test_refused_secret_never_reaches_generic_tool_audit(self) -> None:
        self.tool_calls = [{"action": "add", "file": "memory", "text": SECRET},
                           json.dumps({"action": "add", "file": "memory", "text": SECRET})]
        self.assertTrue(self.execute().ok)
        self.assertFalse(self.path.exists())
        self.assertTrue(all(not response["result"]["success"] for response in self.sent))
        self.assertNotIn(SECRET, self.config.audit_path.read_text())
        calls = [r for r in self.audit.records() if r["kind"] == "tool"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r["data"]["arguments"] == {"redacted": True, "action": "add", "file": "memory"}
                            for r in calls))
        self.assertTrue(all(r["data"]["refusal_category"] == "secret" for r in self.records()))

    def test_successful_tool_arguments_are_redacted_too(self) -> None:
        self.tool_calls = [{"action": "add", "file": "user", "text": "Operator fact."}]
        self.assertTrue(self.execute().ok)
        self.assertTrue(self.sent[0]["result"]["success"])
        self.assertEqual((self.path.parent / "USER.md").read_text(), "- Operator fact.\n")
        self.assertNotIn("Operator fact.", self.config.audit_path.read_text())
        call = next(r for r in self.audit.records() if r["kind"] == "tool")
        self.assertEqual(call["data"]["arguments"], {"redacted": True, "action": "add", "file": "user"})

    def test_truncated_generic_arguments_keep_safe_routing_metadata(self) -> None:
        self.tool_calls = [{"action": "add", "file": "memory", "text": SECRET + "x" * 600}]
        self.assertTrue(self.execute().ok)
        call = next(r for r in self.audit.records() if r["kind"] == "tool")
        self.assertEqual(call["data"]["arguments"], {"redacted": True, "action": "add", "file": "memory"})
        self.assertNotIn(SECRET, self.config.audit_path.read_text())

    def test_policy_declined_tool_arguments_are_redacted(self) -> None:
        self.config.profiles["worker"]["approvals"] = [
            {"server": "bothy", "tool": "bothy_memory", "verdict": "deny", "reason": "Held by operator"}]
        self.tool_calls = [{"action": "add", "file": "memory", "text": SECRET}]
        self.assertTrue(self.execute().ok)
        self.assertFalse(self.sent[0]["result"]["success"])
        self.assertFalse(self.path.exists())
        self.assertNotIn(SECRET, self.config.audit_path.read_text())
        call = next(r for r in self.audit.records() if r["kind"] == "tool")
        self.assertEqual(call["data"]["arguments"], {"redacted": True, "action": "add", "file": "memory"})

    def test_confirm_hold_redacts_arguments_in_chat_and_terminal(self) -> None:
        from tests.test_interrupts import ImmediateThread

        self.config.question_hold_seconds = 30
        self.config.profiles["worker"]["approvals"] = [
            {"server": "bothy", "tool": "bothy_memory", "verdict": "confirm", "reason": "Ask operator"}]
        self.runner.post_question = Mock(return_value=["posted"])
        self.tool_calls = [{"action": "add", "file": "memory", "text": SECRET}]

        def confirm(client):
            client._tick_interrupts()
            pending = next(iter(client.interrupts.values()))
            self.assertEqual(pending.kind, "approval")
            self.assertTrue(client.answer(pending.id, "yes", "operator"))
            client._tick_interrupts()

        self.after_tool_call = confirm
        stderr = io.StringIO()
        with patch("bothy.codex.threading.Thread", ImmediateThread), contextlib.redirect_stderr(stderr):
            result = self.execute(foreground=True, reply_to={"slack": {"channel": "test"}})
        self.assertTrue(result.ok, result.error)
        self.runner.post_question.assert_called_once()
        for text in (stderr.getvalue(), self.runner.post_question.call_args.args[1]):
            self.assertNotIn(SECRET, text)
            self.assertIn('"redacted": true', text)
            self.assertIn('"action": "add"', text)
            self.assertIn('"file": "memory"', text)
        self.assertFalse(self.sent[0]["result"]["success"])
        self.assertNotIn(SECRET, self.config.audit_path.read_text())

    def test_daily_note_arguments_redacted_on_success_and_refusal(self) -> None:
        self.tool_name = "bothy_memory_note"
        self.config.profiles["worker"]["tools"] = [self.tool_name]
        self.tool_calls = [{"text": "Private daily fact."}, {"text": SECRET}]
        self.assertTrue(self.execute().ok)
        self.assertTrue(self.sent[0]["result"]["success"])
        self.assertFalse(self.sent[1]["result"]["success"])
        calls = [r for r in self.audit.records() if r["kind"] == "tool"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r["data"]["arguments"]["redacted"] for r in calls))
        for text in ("Private daily fact.", SECRET):
            self.assertNotIn(text, self.config.audit_path.read_text())
        self.assertNotIn("Private daily fact.", self.injected() or "")

    def test_daily_note_confirm_hold_redacts_text(self) -> None:
        from tests.test_interrupts import ImmediateThread

        self.tool_name = "bothy_memory_note"
        self.config.profiles["worker"]["tools"] = [self.tool_name]
        self.config.profiles["worker"]["approvals"] = [
            {"server": "bothy", "tool": self.tool_name, "verdict": "confirm", "reason": "Ask operator"}]
        self.config.question_hold_seconds = 30
        self.runner.post_question = Mock(return_value=["posted"])
        self.tool_calls = [{"text": SECRET}]

        def confirm(client):
            client._tick_interrupts()
            pending = next(iter(client.interrupts.values()))
            self.assertTrue(client.answer(pending.id, "yes", "operator"))
            client._tick_interrupts()

        self.after_tool_call = confirm
        stderr = io.StringIO()
        with patch("bothy.codex.threading.Thread", ImmediateThread), contextlib.redirect_stderr(stderr):
            result = self.execute(foreground=True, reply_to={"slack": {"channel": "test"}})
        self.assertTrue(result.ok, result.error)
        self.runner.post_question.assert_called_once()
        for text in (stderr.getvalue(), self.runner.post_question.call_args.args[1], self.config.audit_path.read_text()):
            self.assertNotIn(SECRET, text)
            self.assertIn('"redacted":true', text.replace(" ", ""))
        self.assertFalse(self.sent[0]["result"]["success"])

    def test_daily_tool_grants_for_read_write_off_and_untrusted(self) -> None:
        tools = ["bothy_memory_note", "bothy_memory_search", "bothy_memory_get"]
        self.config.profiles["worker"]["tools"] = tools
        for mode, untrusted, expected in (("read", False, tools[1:]), ("write", False, tools),
                                          ("write", True, tools[1:]), ("off", False, [])):
            with self.subTest(mode=mode, untrusted=untrusted):
                self.config.profiles["worker"]["memory"] = mode
                self.assertTrue(self.execute(untrusted=untrusted).ok)
                names = [s["name"] for s in self.started[-1]["dynamic_tools"] or []]
                self.assertEqual(set(names), set(expected))
        self.config.profiles["worker"]["memory"] = "write"
        self.config.profiles["worker"]["allow_from_untrusted"] = True
        self.assertTrue(self.execute(untrusted=True).ok)
        self.assertEqual({s["name"] for s in self.started[-1]["dynamic_tools"]}, set(tools))

    def test_memory_refusal_releases_cairn_budget_and_exports_trace(self) -> None:
        tracker = Mock()
        tracker.brief.return_value = (None, "No brief in fixture")
        self.runner.tracker = tracker
        for file, raw, gate in (("SOUL.md", b"x" * 16385, "soul_invalid"),
                                ("MEMORY.md", b"\xff", "memory_invalid")):
            with self.subTest(file=file):
                (self.path.parent / file).write_bytes(raw)
                tracker.reset_mock()
                with patch.object(self.runner, "_export_trace") as export, \
                        patch.object(self.admission, "release", wraps=self.admission.release) as release:
                    result = self.execute(cairn_ref="TASK-1")
                self.assertEqual(result.refusal["gate"], gate)
                tracker.claim.assert_called_once_with("TASK-1")
                tracker.record_run.assert_called_once()
                self.assertEqual(tracker.record_run.call_args.kwargs["outcome"], "refused")
                tracker.release.assert_called_once_with("TASK-1")
                release.assert_called_once_with(result.run_id, ok=False, actual=0.0)
                export.assert_called_once_with(result.run_id, "memory-test")
                self.assertEqual(self.admission.snapshot()["in_flight"], 0)
                self.assertEqual(self.calls, [])
                (self.path.parent / file).unlink()

    def test_failed_snapshot_audit_prevents_worker_spawn(self) -> None:
        original = self.audit.append

        def append(**kw):
            if kw["action"] == "memory_loaded":
                raise AuditError("disk full")
            return original(**kw)

        with patch.object(self.audit, "append", side_effect=append):
            result = self.execute()
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.calls, [])

    def test_daemon_passes_heartbeat_only_from_scheduled_jobs(self) -> None:
        daemon = Daemon(self.config, runner=self.runner, admission=self.admission, audit=self.audit,
                        registry=self.processes, alerter=self.alerter, routes=[])
        self.files()
        wake = daemon.fire(Job(id="beat", kind="every", spec="3600", prompt="Check.",
                               heartbeat=True, profile="worker"))
        self.assertEqual(daemon.drain(), 1)
        self.assertNotIn("MEMORY.md", self.injected())
        self.assertTrue(next(e for e in daemon.store.all_events() if e["id"] == wake)["processed"])
        daemon.store.append({"id": "fakebeat", "route": "/hook", "subject": "hook",
                             "profile": "worker", "payload": {"heartbeat": True}, "processed": False})
        self.assertEqual(daemon.drain(), 1)
        self.assertIn("MEMORY.md", self.injected())
        self.assertIsNone(self.started[-1]["dynamic_tools"])
        daemon.store.append({"id": "missing-route", "route": None, "subject": "missing-route",
                             "profile": "worker", "payload": {}, "processed": False})
        self.assertEqual(daemon.drain(), 1)

    def test_slack_and_discord_runs_can_write_granted_memory(self) -> None:
        daemon = Daemon(self.config, runner=self.runner, admission=self.admission, audit=self.audit,
                        registry=self.processes, alerter=self.alerter, routes=[])
        self.tool_calls = [{"action": "add", "file": "memory", "text": "Trusted chat fact."}]
        for route in ("slack", "discord"):
            with self.subTest(route=route):
                daemon.store.append({"id": route, "route": route, "subject": route,
                                     "profile": "worker", "payload": {}, "processed": False})
                self.assertEqual(daemon.drain(), 1)
                self.assertEqual([s["name"] for s in self.started[-1]["dynamic_tools"]], ["bothy_memory"])
                self.assertTrue(self.sent[-1]["result"]["success"])
        self.assertEqual(memory.entries(self.path.read_text()), ["Trusted chat fact."] * 2)


class Operator(MemoryCase):
    def command(self, *args, stdin="") -> tuple[int, str]:
        config = self.home / "config.json"
        config.write_text(json.dumps({"home": str(self.home), "profiles": self.config.profiles}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                patch("sys.stdin", io.StringIO(stdin)):
            code = main(["--config", str(config), "memory", *args])
        return code, output.getvalue()

    def test_cli_list_show_and_stdin_set(self) -> None:
        self.assertEqual(self.command("set", "worker", "soul", stdin="  Persona.\n")[0], 0)
        self.assertEqual(self.command("set", "worker", "memory", stdin="- Kept fact.\n")[0], 0)
        self.assertEqual(self.command("set", "worker", "user", "-", stdin="- Cal.\n")[0], 0)
        code, listing = self.command("list")
        self.assertEqual(code, 0)
        self.assertIn("worker", listing)
        for filename in memory.FILES.values():
            self.assertIn(filename, listing)
        for cap in ("16384 bytes", "8000 characters", "3000 characters"):
            self.assertIn(cap, listing)
        self.assertEqual(self.command("show", "worker", "soul"), (0, "  Persona.\n"))
        self.assertEqual(self.command("show", "worker", "memory"), (0, "- Kept fact.\n"))
        code, show = self.command("show", "worker")
        self.assertEqual(code, 0)
        self.assertIn("SOUL.md:", show)
        self.assertIn("MEMORY.md:", show)
        self.assertIn("USER.md:", show)
        self.assertEqual(len(self.records()), 3)

    def test_cli_source_file_and_caps_scan_and_audit(self) -> None:
        source = self.home / "source.md"
        source.write_text("- File fact.\n")
        self.assertEqual(self.command("set", "worker", "memory", str(source))[0], 0)
        self.assertEqual(self.path.read_text(), source.read_text())
        for text, category in (("- " + SECRET + "\n", "secret"), ("- Invisible\u200b\n", "invisible_unicode"),
                               ("- " + "x" * 8000, "cap")):
            code, output = self.command("set", "worker", "memory", stdin=text)
            self.assertEqual(code, 78)
            self.assertIn(category, output)
            self.assertEqual(self.path.read_text(), source.read_text())
            self.assertEqual(self.records()[-1]["data"]["refusal_category"], category)
        self.assertNotIn(SECRET, self.config.audit_path.read_text())
        self.path.write_text("x" * 8001)
        code, listing = self.command("list")
        self.assertEqual(code, 0)
        self.assertIn("8001 bytes, cap 8000 characters", listing)

    def test_cli_soul_scan_and_empty_reset(self) -> None:
        self.assertEqual(self.command("set", "worker", "soul", stdin=SECRET)[0], 78)
        self.assertEqual(self.command("set", "worker", "soul", stdin="")[0], 0)
        self.assertEqual((self.path.parent / "SOUL.md").read_bytes(), b"")

    def test_cli_show_escapes_hand_written_terminal_controls(self) -> None:
        path = self.path.parent / "SOUL.md"
        path.write_text("Persona\x1b[31m\x00\x08\x7f\u202e\u2028\n")
        code, text = self.command("show", "worker", "soul")
        self.assertEqual(code, 0)
        self.assertEqual(text, "Persona\\x1b[31m\\x00\\x08\\x7f\\u202e\\u2028\n")

    def test_malformed_notes_block_tool_writes_until_operator_repairs(self) -> None:
        self.path.write_text("A paragraph instead of bullets.\n")
        self.refused("entry", text="new")
        self.assertEqual(self.command("set", "worker", "memory", stdin="- Repaired.\n")[0], 0)
        self.write(text="new")
        self.assertEqual(self.path.read_text(), "- Repaired.\n- new\n")

    def test_cli_failed_audit_does_not_write(self) -> None:
        with patch("bothy.memory.AuditLog.append", side_effect=AuditError("full")):
            self.assertEqual(self.command("set", "worker", "memory", stdin="- Fact.\n")[0], 78)
        self.assertFalse(self.path.exists())

    def test_cli_missing_or_off_profiles_and_no_profiles_list(self) -> None:
        self.assertEqual(self.command("show", "unknown")[0], 78)
        self.assertEqual(self.command("set", "worker")[0], 78)
        self.config.profiles["worker"]["memory"] = "off"
        self.assertEqual(self.command("show", "worker")[0], 78)
        self.assertEqual(self.command("list"), (0, "no memory-enabled profiles\n"))

    def test_doctor_warns_nonfatally_about_invalid_memory(self) -> None:
        self.path.write_text("x" * 8001)
        (self.path.parent / "SOUL.md").mkdir()
        self.config.codex_binary = "true"
        self.config.sandbox = "dangerFullAccess"
        output = io.StringIO()
        with patch("bothy.cli.CairnTracker.available", return_value=False), contextlib.redirect_stdout(output):
            code = cmd_doctor(argparse.Namespace(json=True, refresh=False), self.config)
        self.assertEqual(code, 0)
        checks = json.loads(output.getvalue())["checks"]
        files = [c for c in checks if c["check"].startswith("memory worker/")]
        self.assertEqual(len(files), 3)
        self.assertTrue(all(not c["fatal"] for c in files))
        self.assertFalse(next(c for c in files if "SOUL" in c["check"])["ok"])
        self.assertFalse(next(c for c in files if "MEMORY" in c["check"])["ok"])
        self.assertTrue(next(c for c in files if "USER" in c["check"])["ok"])


if __name__ == "__main__":
    unittest.main()
