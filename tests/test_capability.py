"""Capability is a property of the job, and nothing by default."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from bothy.capability import DynamicTool, Profile, ToolRegistry, builtin_tools, render_config_toml
from bothy.audit import AuditLog


class ProfileTests(unittest.TestCase):
    def test_a_generated_config_carries_the_server_but_not_the_secret(self) -> None:
        profile = Profile.from_dict("mail", {
            "mcp_servers": {"gmail": {"command": "mcp-gmail", "env_vars": ["GMAIL_TOKEN"]}},
        })
        rendered = render_config_toml(profile)
        self.assertIn("[mcp_servers.gmail]", rendered)
        self.assertIn('env_vars = ["GMAIL_TOKEN"]', rendered)
        self.assertNotIn("xoxb", rendered)

    def test_no_profile_means_no_config_beyond_defaults(self) -> None:
        self.assertNotIn("mcp_servers", render_config_toml(None))

    def test_an_explicit_empty_mcp_allowlist_grants_no_tools(self) -> None:
        profile = Profile.from_dict("mail", {"mcp_servers": {"mail": {"command": "mail"}},
                                              "mcp_tools": {"mail": []}})
        self.assertIn("enabled_tools = []", render_config_toml(profile))
        self.assertIn("NO TOOLS", profile.summary())

    def test_untrusted_opt_in_requires_a_boolean(self) -> None:
        for value in ("false", "true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be true or false"):
                Profile.from_dict("memory", {"allow_from_untrusted": value})


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.registry = builtin_tools(checklist_path=self.dir / "checklist.md",
                                      audit=self.audit,
                                      note=lambda ref, text: True, status=lambda: {"in_flight": 0})

    def test_an_empty_registry_declares_nothing(self) -> None:
        """A job that names no profile must reach nothing extra."""
        self.assertEqual(ToolRegistry().specs(), [])
        self.assertFalse(ToolRegistry())

    def test_selection_narrows_and_ignores_unknown_names(self) -> None:
        """A renamed tool should degrade capability, not refuse to start."""
        chosen = self.registry.select(["bothy_note", "a_tool_that_was_renamed"])
        self.assertEqual(chosen.names(), ["bothy_note"])

    def test_the_agent_can_maintain_its_own_checklist(self) -> None:
        """The loop the heartbeat design always assumed."""
        ok, _ = self.registry.call("bothy_checklist_update", {"checklist": "- keep disk under 85%"})
        self.assertTrue(ok)
        self.assertIn("85%", (self.dir / "checklist.md").read_text())
        ok, text = self.registry.call("bothy_checklist_read", {})
        self.assertIn("85%", text)

    def test_an_empty_checklist_is_refused(self) -> None:
        """It would silently disable every future heartbeat."""
        _, text = self.registry.call("bothy_checklist_update", {"checklist": "   "})
        self.assertIn("refused", text)

    def test_every_checklist_replacement_records_before_and_after_hashes(self) -> None:
        path = self.dir / "checklist.md"
        for before, after in ((b"", b"- first\n"), (b"- first\n", b"- second\n")):
            ok, _ = self.registry.call("bothy_checklist_update", {"checklist": after.decode()})
            self.assertTrue(ok)
            record = list(self.audit.records())[-1]
            self.assertEqual((record["kind"], record["action"]), ("checklist", "replaced"))
            self.assertEqual(record["data"], {"before_sha256": hashlib.sha256(before).hexdigest(),
                                              "after_sha256": hashlib.sha256(after).hexdigest()})
            self.assertEqual(path.read_bytes(), after)
        self.audit.verify()

    def test_an_unwritable_audit_prevents_a_checklist_write(self) -> None:
        path = self.dir / "checklist.md"
        path.write_text("original", encoding="utf-8")
        registry = builtin_tools(checklist_path=path, audit=AuditLog(self.dir))
        ok, _ = registry.call("bothy_checklist_update", {"checklist": "replacement"})
        self.assertFalse(ok)
        self.assertEqual(path.read_text(), "original")

    def test_an_unknown_tool_answers_rather_than_raising(self) -> None:
        """An unanswered request hangs the turn until its wall clock."""
        ok, text = self.registry.call("not_a_tool", {})
        self.assertFalse(ok)
        self.assertIn("not a tool", text)

    def test_a_failing_tool_reports_instead_of_ending_the_turn(self) -> None:
        registry = ToolRegistry([DynamicTool(
            name="explodes", description="", input_schema={},
            handler=lambda _: (_ for _ in ()).throw(RuntimeError("boom")))])
        ok, text = registry.call("explodes", {})
        self.assertFalse(ok)
        self.assertIn("boom", text)

    def test_string_arguments_are_tolerated(self) -> None:
        ok, _ = self.registry.call("bothy_checklist_update", '{"checklist": "- from a string"}')
        self.assertTrue(ok)

    def test_a_spec_is_shaped_as_codex_expects(self) -> None:
        spec = self.registry.select(["bothy_note"]).specs()[0]
        self.assertEqual(spec["type"], "function")
        self.assertIn("inputSchema", spec)
        self.assertIn("description", spec)


if __name__ == "__main__":
    unittest.main()
