"""Typed approval policy: proceed, deny, guide, confirm.

The behaviour these pin down used to be "decline everything, silently". A tool
pinned to ask for approval was refused with no record and no alert, which looked
exactly like a broken tool.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bothy import clock, codex, policy
from bothy.audit import AuditError, AuditLog
from bothy.cli import main
from bothy.trace import build_trace
from bothy.budget import BudgetPolicy, BudgetLedger
from bothy.capability import DynamicTool, Profile, ToolRegistry, render_config_toml
from bothy.config import Config, load
from bothy.pool import Admission
from bothy.proc import ProcessRegistry
from bothy.runner import Runner

MAIL = {"mail": {"command": "mail-mcp"}}


def profile_raw(*rules: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"mcp_servers": MAIL, "mcp_tools": {"mail": ["read_message", "send_message"]},
            "approvals": list(rules), **extra}


def rule(verdict: str, **extra: Any) -> dict[str, Any]:
    return {"server": "mail", "tool": "send_message", "verdict": verdict, **extra}


def approval_request(tool: str = "send_message", server: str = "mail") -> dict[str, Any]:
    return {"server": server, "tool": tool}


class FakeAlerter:
    def __init__(self) -> None:
        self.incidents: list[dict[str, Any]] = []

    def incident(self, **kwargs: Any) -> None:
        self.incidents.append(kwargs)

    def resolved(self, **kwargs: Any) -> None:
        pass


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = Config(home=self.home, budget=BudgetPolicy(mode="api"),
                             codex_credentials_dir=self.home / "credentials")
        self.config.ensure_dirs()
        self.audit = AuditLog(self.config.audit_path)
        self.alerter = FakeAlerter()
        self.runner = Runner(
            self.config,
            admission=Admission(self.config.pool, BudgetLedger(self.config.budget_path, self.config.budget)),
            audit=self.audit, registry=ProcessRegistry(self.config.registry_path), alerter=self.alerter)

    def profile(self, *rules: dict[str, Any], **extra: Any) -> Profile:
        return Profile.from_dict("mail", profile_raw(*rules, **extra))

    def approval(self, active: Profile) -> Any:
        return self.runner._approval_handler("run_1", "ACME-1", active)

    def records(self, kind: str = "approval") -> list[dict[str, Any]]:
        return [r for r in self.audit.records() if r["kind"] == kind]

    def approve_in_log(self, tool: str = "send_message", decision: str = "approve") -> None:
        self.audit.append(kind="interrupt", action="answered",
                          data={"decision": decision, "server": "mail", "tool": tool})


class VerdictTests(RunnerCase):
    def test_a_deny_rule_declines_and_is_audited(self) -> None:
        answer = self.approval(self.profile(rule("deny", reason="no outbound mail")))(
            "item/mcpToolCall/requestApproval", approval_request())
        self.assertEqual(answer.decision, "decline")
        self.assertEqual(answer.reason, "no outbound mail")
        [record] = self.records()
        self.assertEqual((record["action"], record["status"]), ("deny", "refused"))
        self.assertEqual(record["run_id"], "run_1")
        self.assertEqual(record["data"], {
            "method": "item/mcpToolCall/requestApproval", "server": "mail", "tool": "send_message",
            "rule": "mail.send_message", "reason": "no outbound mail"})

    def test_a_proceed_that_cannot_be_audited_becomes_a_deny_and_alerts(self) -> None:
        def failing(**fields: Any) -> None:
            raise AuditError("disk full")

        with mock.patch.object(self.audit, "append", failing):
            answer = self.approval(self.profile(rule("proceed")))("x/requestApproval", approval_request())
        self.assertEqual(answer.decision, "decline")
        self.assertEqual(answer.reason, "the decision could not be audited")
        self.assertEqual([i["severity"] for i in self.alerter.incidents], ["critical"])
        self.assertEqual(self.records(), [])

    def test_a_decline_still_goes_through_when_the_audit_log_fails(self) -> None:
        def failing(**fields: Any) -> None:
            raise AuditError("disk full")

        with mock.patch.object(self.audit, "append", failing):
            answer = self.approval(self.profile(rule("deny", reason="no")))("x/requestApproval", approval_request())
        self.assertEqual((answer.decision, answer.reason), ("decline", "no"))
        self.assertEqual([i["severity"] for i in self.alerter.incidents], ["critical"])

    def test_a_proceed_rule_accepts_and_is_audited(self) -> None:
        answer = self.approval(self.profile(rule("proceed")))("x/requestApproval", approval_request())
        self.assertEqual(answer.decision, "accept")
        self.assertEqual(self.records()[0]["action"], "proceed")

    def test_a_guide_reason_reaches_the_wire_payload(self) -> None:
        reason = "Draft the message in the task note instead; a person sends it."
        c = codex.CodexClient(run_id="r", codex_home="/tmp/bothy-test-home",
                              approval=self.approval(self.profile(rule("guide", reason=reason))))
        sent: list[dict[str, Any]] = []
        c._send = sent.append  # type: ignore[assignment]
        c._handle_server_request({"id": 9, "method": "item/mcpToolCall/requestApproval",
                                  "params": approval_request()})
        self.assertEqual(sent, [{"id": 9, "result": {"decision": "decline", "reason": reason}}])
        self.assertEqual(self.records()[0]["action"], "guide")

    def test_a_tool_with_no_rule_is_declined_by_default_and_audited(self) -> None:
        answer = self.approval(self.profile())("item/commandExecution/requestApproval", {"command": "rm -rf /"})
        self.assertEqual(answer.decision, "decline")
        self.assertIn("no approval policy configured", answer.reason)
        self.assertEqual(self.records()[0]["status"], "refused")

    def test_a_declined_mcp_ask_tool_raises_an_alert(self) -> None:
        active = self.profile(mcp_ask={"mail": ["send_message"]})
        answer = self.approval(active)("x/requestApproval", approval_request())
        self.assertEqual(answer.decision, "decline")
        self.assertEqual(self.records()[0]["data"]["rule"], "mcp_ask")
        [incident] = self.alerter.incidents
        self.assertIn("mail.send_message", incident["error"])

    def test_an_ungated_decline_does_not_alert(self) -> None:
        self.approval(self.profile())("x/requestApproval", approval_request("read_message"))
        self.assertEqual(self.alerter.incidents, [])

    def test_an_unknown_server_request_is_still_audited(self) -> None:
        c = codex.CodexClient(run_id="r", codex_home="/tmp/bothy-test-home",
                              approval=self.approval(self.profile()))
        sent: list[dict[str, Any]] = []
        c._send = sent.append  # type: ignore[assignment]
        c._handle_server_request({"id": 3, "method": "mcpServer/elicitation/request", "params": approval_request()})
        self.assertEqual(sent[0]["error"]["code"], -32601)
        self.assertEqual(self.records()[0]["data"]["method"], "mcpServer/elicitation/request")

    def test_a_failing_policy_declines_on_the_wire(self) -> None:
        def broken(method: str, params: dict[str, Any]) -> codex.ApprovalDecision:
            raise RuntimeError("boom")

        c = codex.CodexClient(run_id="r", codex_home="/tmp/bothy-test-home", approval=broken)
        sent: list[dict[str, Any]] = []
        c._send = sent.append  # type: ignore[assignment]
        c._handle_server_request({"id": 4, "method": "x/requestApproval", "params": {}})
        self.assertEqual(sent[0]["result"]["decision"], "decline")


class ConfirmTests(RunnerCase):
    def setUp(self) -> None:
        super().setUp()
        self.active = self.profile(rule("confirm", unless_approved_within=900, reason="outbound mail"))

    def ask(self, minutes_later: float) -> codex.ApprovalDecision:
        # Age the record by moving "now" forward rather than sleeping.
        later = clock.utcnow() + dt.timedelta(minutes=minutes_later)
        real = policy.evaluate
        with mock.patch.object(policy, "evaluate", lambda m, p, f: real(m, p, dataclasses.replace(f, now=later))):
            return self.approval(self.active)("x/requestApproval", approval_request())

    def test_an_approval_ten_minutes_ago_inside_a_fifteen_minute_window_proceeds(self) -> None:
        self.approve_in_log()
        answer = self.ask(minutes_later=10)
        self.assertEqual(answer.decision, "accept")
        self.assertEqual(self.records()[0]["action"], "proceed")
        self.assertEqual(self.records()[0]["status"], "ok")

    def test_an_approval_sixteen_minutes_ago_is_declined_as_awaiting_human(self) -> None:
        self.approve_in_log()
        answer = self.ask(minutes_later=16)
        self.assertEqual(answer.decision, "decline")
        self.assertTrue(answer.reason.startswith(policy.AWAITING_CONFIRMATION))
        record = self.records()[0]
        self.assertEqual((record["action"], record["status"]), ("confirm", "awaiting_human"))

    def test_no_approval_at_all_is_awaiting_human(self) -> None:
        self.assertEqual(self.ask(minutes_later=0).decision, "decline")
        self.assertEqual(self.records()[0]["status"], "awaiting_human")

    def test_only_a_matching_approve_counts(self) -> None:
        self.approve_in_log(tool="read_message")
        self.approve_in_log(decision="deny")
        self.assertEqual(self.ask(minutes_later=1).decision, "decline")

    def test_the_newest_approval_decides(self) -> None:
        self.approve_in_log()
        self.assertEqual(self.ask(minutes_later=20).decision, "decline")
        self.approve_in_log()
        self.assertEqual(self.ask(minutes_later=1).decision, "accept")

    def test_a_confirm_rule_does_not_interrupt_the_turn(self) -> None:
        c = codex.CodexClient(run_id="r", codex_home="/tmp/bothy-test-home", approval=self.approval(self.active))
        c._send = lambda payload: None  # type: ignore[assignment]
        c._handle_server_request({"id": 1, "method": "x/requestApproval", "params": approval_request()})
        self.assertIsNone(c._stop_reason)

    def test_an_unreadable_audit_log_means_deny(self) -> None:
        self.approve_in_log()
        # A directory where the log should be: exists, cannot be opened as a file.
        broken = AuditLog(self.home / "unreadable")
        (self.home / "unreadable").mkdir()
        decision = policy.evaluate("x/requestApproval", approval_request(),
                                   policy.Facts(rules=self.active.rules(), audit=broken))
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.status, "refused")
        self.assertIn("could not be read", decision.reason)

    def test_no_audit_log_at_all_means_deny(self) -> None:
        decision = policy.evaluate("x/requestApproval", approval_request(),
                                   policy.Facts(rules=self.active.rules(), audit=None))
        self.assertEqual(decision.verdict, "deny")

    def test_a_future_dated_approval_is_not_evidence(self) -> None:
        self.approve_in_log()
        decision = policy.evaluate("x/requestApproval", approval_request(),
                                   policy.Facts(rules=self.active.rules(), audit=self.audit,
                                                now=clock.utcnow() - dt.timedelta(hours=1)))
        self.assertEqual(decision.status, "awaiting_human")


class HostedToolTests(RunnerCase):
    def registry(self, active: Profile) -> ToolRegistry:
        ran: list[str] = []
        self.ran = ran
        base = ToolRegistry([DynamicTool("bothy_note", "d", {}, lambda a: ran.append("x") or "ok")])
        return base.gated(self.runner._tool_gate("run_1", "ACME-1", active))

    def test_a_hosted_tool_rule_declines_and_the_handler_never_runs(self) -> None:
        active = Profile.from_dict("p", {"tools": ["bothy_note"], "approvals": [
            {"server": "bothy", "tool": "bothy_note", "verdict": "guide", "reason": "use the task comment"}]})
        ok, text = self.registry(active).call("bothy_note", {})
        self.assertEqual((ok, text, self.ran), (False, "use the task comment", []))
        self.assertEqual(self.records()[0]["data"]["server"], "bothy")

    def test_a_hosted_tool_with_no_rule_runs_and_writes_no_approval_record(self) -> None:
        active = Profile.from_dict("p", {"tools": ["bothy_note"]})
        self.assertEqual(self.registry(active).call("bothy_note", {}), (True, "ok"))
        self.assertEqual(self.records(), [])

    def test_a_gate_that_raises_declines(self) -> None:
        def broken(name: str, arguments: Any) -> str | None:
            raise RuntimeError("boom")

        base = ToolRegistry([DynamicTool("t", "d", {}, lambda a: "ok")]).gated(broken)
        ok, text = base.call("t", {})
        self.assertFalse(ok)
        self.assertIn("approval check failed", text)


class ProfileValidationTests(unittest.TestCase):
    def test_a_rule_for_a_tool_absent_from_mcp_tools_is_refused(self) -> None:
        raw = profile_raw(rule("deny"))
        raw["mcp_tools"] = {"mail": ["read_message"]}
        with self.assertRaisesRegex(ValueError, "mail.send_message.*not listed in mcp_tools"):
            Profile.from_dict("mail", raw)

    def test_a_hosted_rule_for_an_ungranted_tool_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not listed in tools"):
            Profile.from_dict("p", {"approvals": [{"server": "bothy", "tool": "bothy_note", "verdict": "deny"}]})

    def test_a_rule_for_an_ungranted_server_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not grant"):
            Profile.from_dict("p", {"approvals": [rule("deny")]})

    def test_bad_rules_are_refused(self) -> None:
        cases = {
            "unknown verdict": rule("allow"),
            "unknown key": rule("deny", colour="red"),
            "needs a reason": rule("guide"),
            "only applies to a confirm": rule("deny", unless_approved_within=60),
            "positive number": rule("confirm", unless_approved_within=0),
            "positive number ": rule("confirm", unless_approved_within=True),
        }
        for label, bad in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, label.strip()):
                Profile.from_dict("mail", profile_raw(bad))

    def test_two_rules_for_one_tool_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than one rule"):
            Profile.from_dict("mail", profile_raw(rule("deny"), rule("guide", reason="x")))

    def test_any_rule_but_proceed_renders_approval_mode_always(self) -> None:
        for verdict in ("deny", "guide", "confirm"):
            with self.subTest(verdict):
                rendered = render_config_toml(Profile.from_dict("mail", profile_raw(rule(verdict, reason="r"))))
                self.assertIn('[mcp_servers.mail.tools.send_message]\napproval_mode = "always"', rendered)
        proceed = render_config_toml(Profile.from_dict("mail", profile_raw(rule("proceed"))))
        self.assertNotIn("approval_mode", proceed)

    def test_a_rule_and_mcp_ask_do_not_render_the_table_twice(self) -> None:
        rendered = render_config_toml(Profile.from_dict(
            "mail", profile_raw(rule("deny"), mcp_ask={"mail": ["send_message"]})))
        self.assertEqual(rendered.count("[mcp_servers.mail.tools.send_message]"), 1)


class UnknownKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"

    def write(self, body: dict[str, Any]) -> Path:
        self.path.write_text(json.dumps({"home": self.temp.name, **body}))
        return self.path

    def test_an_unknown_profile_key_fails_the_load_and_is_named(self) -> None:
        with self.assertRaisesRegex(ValueError, "approvalz"):
            load(self.write({"profiles": {"mail": {"approvalz": []}}}))

    def test_an_unknown_profile_key_fails_from_dict_and_is_named(self) -> None:
        with self.assertRaisesRegex(ValueError, r"profile mail: unknown key\(s\) \['sandbxo'\]"):
            Profile.from_dict("mail", {"sandbxo": "read-only"})

    def test_an_unknown_top_level_key_fails_the_load_and_is_named(self) -> None:
        with self.assertRaisesRegex(ValueError, "wall_clock_secnds"):
            load(self.write({"wall_clock_secnds": 5}))

    def test_a_property_is_not_a_config_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "audit_path"):
            load(self.write({"audit_path": "/tmp/x"}))

    def test_a_profile_that_does_not_parse_fails_the_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not grant"):
            load(self.write({"profiles": {"mail": {"approvals": [rule("deny")]}}}))

    def test_every_config_field_is_accepted(self) -> None:
        names = {f.name for f in dataclasses.fields(Config)}
        self.assertIn("profiles", names)
        load(self.write({"site": "x", "port": 9000, "profiles": {}}))

    def test_per_profile_limits_stay_accepted_and_are_read_by_the_runner(self) -> None:
        loaded = load(self.write({"profiles": {"cheap": {"max_tokens": 5000, "wall_clock_seconds": 60}}}))
        active = Profile.from_dict("cheap", loaded.profiles["cheap"])
        self.assertEqual((active.max_tokens, active.wall_clock_seconds), (5000, 60))


class TraceTests(RunnerCase):
    """An approval decision is a span under the turn, so a run's trace shows what was refused."""

    def run_with_decisions(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        self.config.profiles = {"mail": profile_raw(rule("deny", reason="no outbound mail"))}
        run_id = "run_policy_trace"
        sent: list[dict[str, Any]] = []

        class Stub(codex.CodexClient):
            def start(self) -> Any:
                self._popen = SimpleNamespace(pid=12345)
                return self._popen

            def handshake(self) -> dict[str, Any]:
                return {}

            def start_thread(self, **kwargs: Any) -> str:
                return "thread-1"

            def request(self, method: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
                if method == "turn/start":
                    self._notifications.put(("turn/started", {"turn": {"id": "turn-1"}}))
                    for index, (name, body) in enumerate(requests):
                        self._handle_server_request({"id": index, "method": name, "params": body})
                    self._notifications.put(("turn/completed", {"turn": {"status": "completed"}}))
                    return {"turn": {"id": "turn-1"}}
                return {}

            def _send(self, payload: dict[str, Any]) -> None:
                sent.append(payload)

            def close(self, **kwargs: Any) -> str:
                return "terminated"

        self.sent = sent
        with mock.patch("bothy.runner.codex.CodexClient", Stub), \
                mock.patch.object(self.runner.registry, "register"), \
                mock.patch("bothy.runner.ids.run_id", return_value=run_id):
            result = self.runner.run(subject="policy-trace", prompt="go", profile="mail")
        self.assertTrue(result.ok, result.error)
        return run_id

    def test_a_deny_decision_traces_as_root_turn_approval(self) -> None:
        run_id = self.run_with_decisions([("item/mcpToolCall/requestApproval", approval_request())])
        self.assertEqual(self.sent[0]["result"]["decision"], "decline")
        made = build_trace(self.audit, run_id)["resourceSpans"][0]["scopeSpans"][0]["spans"]
        root = next(s for s in made if "parentSpanId" not in s)
        turn = next(s for s in made if s["name"].startswith("turn "))
        [approval] = [s for s in made if s["name"].startswith("approval ")]
        self.assertEqual(approval["name"], "approval mail.send_message")
        self.assertEqual(turn["parentSpanId"], root["spanId"])
        self.assertEqual(approval["parentSpanId"], turn["spanId"])
        attributes = {a["key"]: a["value"] for a in approval["attributes"]}
        self.assertEqual(attributes["bothy.verdict"], {"stringValue": "deny"})
        self.assertEqual(approval["status"]["code"], 2)

    def test_each_decision_in_a_run_gets_its_own_span(self) -> None:
        run_id = self.run_with_decisions([("item/mcpToolCall/requestApproval", approval_request()),
                                          ("item/mcpToolCall/requestApproval", approval_request("read_message"))])
        spans = [r["span"]["span_id"] for r in self.audit.records()
                 if r["kind"] == "approval" and r["run_id"] == run_id]
        self.assertEqual(len(spans), 2)
        self.assertEqual(len(set(spans)), 2)


class MainConfigErrorTests(unittest.TestCase):
    def test_a_stray_config_key_exits_78_and_names_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"home": temp, "wall_clock_secnds": 5}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--config", str(path), "doctor"])
        self.assertEqual(code, 78)
        self.assertIn("bothy:", err.getvalue())
        self.assertIn("wall_clock_secnds", err.getvalue())


if __name__ == "__main__":
    unittest.main()
