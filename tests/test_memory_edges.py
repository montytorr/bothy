"""BOTHY-43 regressions over a fake app-server's actual stdio wire."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bothy.audit import AuditLog
from bothy.budget import BudgetPolicy
from bothy.cli import build, cmd_doctor, main
from bothy.config import Config


SERVER = r'''
import json
import sys

def send(**payload):
    print(json.dumps(payload), flush=True)

def event(method, **params):
    send(method=method, params=params)

mode = ""
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send(id=message["id"], result={})
    elif method == "thread/start":
        send(id=message["id"], result={"thread": {"id": "thread"}})
    elif method == "turn/start":
        mode = message["params"]["input"][0]["text"]
        send(id=message["id"], result={"turn": {"id": "turn"}})
        send(id="tool-request", method="item/tool/call", params={
            "threadId": "thread", "turnId": "turn", "callId": "tool",
            "tool": "bothy_memory_note", "arguments": {"text": "Private daily note."}})
    elif message.get("id") == "tool-request":
        assert message["result"]["success"]
        items = [
            {"id": "tool", "type": "dynamicToolCall", "tool": "bothy_memory_note",
             "arguments": {"text": "Private daily note."}, "status": "completed",
             "contentItems": [{"text": "Private daily note."}]},
            {"id": "answer", "type": "agentMessage", "text": "Remembered. — H."},
        ]
        if mode != "terminal":
            for item in items:
                event("item/started", item={"id": item["id"], "type": item["type"]})
                event("item/completed", item=item)
        if mode == "cap":
            event("thread/tokenUsage/updated", tokenUsage={"total": {"totalTokens": 60001}})
        else:
            event("turn/completed", turn={"id": "turn", "status": "completed", "items": items})
    elif method == "turn/interrupt":
        send(id=message["id"], result={})
        event("turn/completed", turn={"id": "turn", "status": "interrupted"})
'''


class MemoryEdges(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        binary = self.home / "fake-codex"
        binary.write_text(f"#!{sys.executable}\n" + SERVER)
        binary.chmod(0o700)
        self.config = Config(home=self.home, workspace=self.home / "workspace",
                             budget=BudgetPolicy(mode="api"), codex_binary=str(binary),
                             codex_credentials_dir=self.home / "credentials",
                             profiles={"worker": {"memory": "write", "tools": ["bothy_memory_note"]}})
        self.config.ensure_dirs()
        self.config_path = self.home / "config.json"
        self.config_path.write_text(json.dumps({
            "home": str(self.home), "workspace": str(self.config.workspace),
            "codex_binary": str(binary), "budget": {"mode": "api"},
            "codex_credentials_dir": str(self.config.codex_credentials_dir),
            "profiles": self.config.profiles}))

    def command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = main(["--config", str(self.config_path), *args])
        return result, output.getvalue()

    def test_terminal_items_capture_reply_and_private_metadata_summary(self):
        runner, _, audit, _ = build(self.config)
        result = runner.run(subject="terminal", prompt="terminal", profile="worker")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.messages, ["Remembered. — H."])
        finished = next(r for r in audit.records() if r["kind"] == "turn" and r["action"] == "finished")
        self.assertEqual([item["type"] for item in finished["data"]["items"]],
                         ["dynamicToolCall", "agentMessage"])
        self.assertNotIn("Private daily note.", self.config.audit_path.read_text())
        self.assertNotIn("Remembered.", self.config.audit_path.read_text())
        self.assertEqual(audit.verify(), len(list(audit.records())))

    def test_streamed_and_terminal_items_print_reply_once(self):
        code, output = self.command("run", "stream", "--subject", "stream", "--profile", "worker")
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("Remembered. — H."), 1)
        finished = next(r for r in AuditLog(self.config.audit_path).records()
                        if r["kind"] == "turn" and r["action"] == "finished")
        self.assertEqual(len(finished["data"]["items"]), 2)

    def test_terminal_only_reply_printed_by_plain_cli(self):
        code, output = self.command("run", "terminal", "--subject", "terminal", "--profile", "worker")
        self.assertEqual(code, 0, output)
        self.assertIn("Remembered. — H.", output)

    def test_token_cap_prints_reason_and_prior_answer(self):
        code, output = self.command("run", "cap", "--subject", "cap", "--profile", "worker",
                                    "--max-tokens", "60000")
        self.assertEqual(code, 1, output)
        self.assertIn("interrupted", output)
        self.assertIn("stopped: token_cap", output)
        self.assertIn("Remembered. — H.", output)

    def test_doctor_effective_memory_cap_warning_is_nonfatal(self):
        self.config.codex_binary = "true"
        self.config.sandbox = "dangerFullAccess"
        cases = [(60000, None, True), (None, 60000, True), (60000, 100000, False),
                 (100000, None, False), (None, None, False)]
        for global_cap, profile_cap, warning in cases:
            with self.subTest(global_cap=global_cap, profile_cap=profile_cap):
                self.config.max_tokens = global_cap
                self.config.profiles["worker"]["max_tokens"] = profile_cap
                output = io.StringIO()
                with patch("bothy.cli.CairnTracker.available", return_value=True), contextlib.redirect_stdout(output):
                    code = cmd_doctor(argparse.Namespace(json=True, refresh=False), self.config)
                self.assertEqual(code, 0, output.getvalue())
                checks = [c for c in json.loads(output.getvalue())["checks"] if c["check"].endswith("token cap")]
                self.assertEqual(bool(checks), warning)
                if warning:
                    self.assertFalse(checks[0]["fatal"])
                    self.assertFalse(checks[0]["ok"])
        self.config.profiles["worker"]["memory"] = "off"
        self.config.max_tokens = 60000
        output = io.StringIO()
        with patch("bothy.cli.CairnTracker.available", return_value=True), contextlib.redirect_stdout(output):
            self.assertEqual(cmd_doctor(argparse.Namespace(json=True, refresh=False), self.config), 0)
        self.assertNotIn("token cap", output.getvalue())

    def test_restore_help_names_argument_order(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main(["memory", "restore", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("PROFILE memory|user PREIMAGE", output.getvalue())

    def test_restore_infers_each_file_and_rejects_mismatch_or_ambiguity(self):
        directory = self.config.memory_dir / "worker"
        preimages = directory / "preimages"
        preimages.mkdir(parents=True)
        for file in ("MEMORY", "USER"):
            for prefix in (file, file + ".md"):
                with self.subTest(prefix=prefix):
                    name = f"{prefix}-20261014T000000.000000Z.md"
                    (preimages / name).write_text("- original\n")
                    target = directory / f"{file}.md"
                    target.write_text("- current\n")
                    code, output = self.command("memory", "restore", "worker", name)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(target.read_text(), "- original\n")
                    other = "user" if file == "MEMORY" else "memory"
                    code, output = self.command("memory", "restore", "worker", other, name)
                    self.assertEqual(code, 78, output)
                    self.assertIn("preimage must belong to the selected file", output)
        code, output = self.command("memory", "restore", "worker", "unknown.md")
        self.assertEqual(code, 78, output)
        self.assertIn("PROFILE memory|user PREIMAGE", output)


if __name__ == "__main__":
    unittest.main()
