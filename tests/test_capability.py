"""Capability is a property of the job, and nothing by default."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bothy.capability import DynamicTool, Profile, ToolRegistry, builtin_tools, render_config_toml


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


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.registry = builtin_tools(checklist_path=self.dir / "checklist.md",
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
