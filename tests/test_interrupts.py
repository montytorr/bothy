"""Bounded human replies through real runner/bridge logic and protocol stubs.

Virtual monotonic time advances hundreds of seconds without sleeping. Nothing
here starts a daemon, Codex process, or a Slack/Discord connection.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bothy import codex, policy
from bothy.audit import AuditError, AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.capability import DynamicTool, ToolRegistry
from bothy.cli import cmd_answer
from bothy.config import Config, load
from bothy.daemon import Daemon, DiscordBridge, SlackBridge
from bothy.drove import AttentionForwarder, DroveCli, Outcome
from bothy.interrupts import Entry, InterruptBoard, control_transaction, pending_owner, write_control
from bothy.pool import Admission
from bothy.proc import ProcessRegistry
from bothy.runner import Runner, RunResult
from bothy.schedule import Job, Schedule
from bothy.wake import WakeStore

# Fixtures transcribed from generated Codex 0.162.0 request schemas.
REAL_THREAD = threading.Thread

QUESTION = {"threadId": "thread", "turnId": "turn", "itemId": "question", "isBlocking": True,
            "questions": [{"id": "destination", "question": "Which bucket?", "header": "Bucket"}]}


def mcp_approval(**overrides):
    params = {"serverName": "mail", "threadId": "thread", "turnId": "turn", "mode": "form",
              "message": 'Allow mail to run tool "send_message"',
              "requestedSchema": {"type": "object", "properties": {}},
              "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_name": "send_message"}}
    params.update(overrides)
    return params


class ImmediateThread:
    """Make only the outbound notifier deterministic in virtual-clock tests."""
    def __init__(self, *, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class VirtualInbox:
    def __init__(self, actions):
        self.values, self.actions = [], list(actions)

    def put(self, value):
        self.values.append(value)

    def get(self, **kwargs):
        if self.values:
            return self.values.pop(0)
        if not self.actions:
            raise AssertionError("stub ran out of protocol events")
        self.actions.pop(0)()
        raise queue.Empty


class HoldIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        self.config = Config(home=home, workspace=home, budget=BudgetPolicy(mode="api"),
                             question_hold_seconds=300, codex_credentials_dir=home / "credentials")
        self.config.ensure_dirs()
        self.audit = AuditLog(self.config.audit_path)
        self.admission = Admission(self.config.pool, BudgetLedger(self.config.budget_path, self.config.budget))
        self.registry = ProcessRegistry(self.config.registry_path)
        self.runner = Runner(self.config, admission=self.admission, audit=self.audit, registry=self.registry)
        self.daemon = Daemon(self.config, runner=self.runner, admission=self.admission, audit=self.audit,
                             registry=self.registry, alerter=mock.Mock(), routes=[])
        self.slack = SlackBridge(app_token="unused", bot_token="unused", store=self.daemon.store,
                                 audit=self.audit, allow_from=["human"], interrupt_board=self.daemon.interrupt_board)
        self.slack.client.post = mock.Mock(return_value=["posted"])
        self.slack.client.react = mock.Mock()
        self.daemon.slack = self.slack
        self.now = 0.0
        self.sent = []
        self.method, self.params = "item/tool/requestUserInput", QUESTION
        self.response_hook = None
        self.immediate = False

    def reply(self, text="archive-bucket", actor="human", delivery="reply-1", thread="origin"):
        return self.slack._store({"envelope_id": delivery, "payload": {"event": {
            "user": actor, "channel": "channel", "thread_ts": thread, "ts": delivery, "text": text}}})

    def run_stub(self, actions, **run_args):
        case = self

        class Stub(codex.CodexClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                case.client = self
                self._notifications = VirtualInbox(actions)

            def start(self):
                self._popen = SimpleNamespace(pid=12345)
                return self._popen

            def handshake(self):
                return {}

            def start_thread(self, **kwargs):
                return "thread"

            def request(self, method, params=None, **kwargs):
                if method == "turn/start":
                    self._handle_server_request({"id": 7, "method": case.method, "params": case.params})
                    # A pending RPC must not have been answered by the reader.
                    if case.immediate:
                        case.assertEqual(len(case.sent), 1)
                        case.assertEqual(self.interrupts, {})
                    else:
                        case.assertEqual(case.sent, [])
                    return {"turn": {"id": "turn"}}
                if method == "turn/interrupt":
                    self._notifications.put(("turn/completed", {"turn": {"status": "interrupted"}}))
                return {}

            def _send(self, payload):
                case.sent.append(payload)
                if case.response_hook:
                    case.response_hook(self, payload)
                else:
                    self._notifications.put(("turn/completed", {"turn": {"status": "completed"}}))

            def close(self, **kwargs):
                return "terminated"

        with mock.patch("bothy.runner.codex.CodexClient", Stub), \
                mock.patch("bothy.codex.clock.monotonic", side_effect=lambda: self.now), \
                mock.patch("bothy.codex.threading.Thread", ImmediateThread), \
                mock.patch.object(self.registry, "register"), mock.patch.object(self.registry, "unregister"):
            origin = run_args.pop("reply_to", {"slack": {"channel": "channel", "thread_ts": "origin"}})
            if run_args.pop("drain_event", False):
                self.slack._store({"envelope_id": "original", "payload": {"event": {
                    "user": "human", "channel": "channel", "ts": "origin", "text": "go"}}})
                results = []
                run = self.runner.run
                def execute(**args):
                    self.assertEqual(args["reply_to"], origin)
                    result = run(**args)
                    results.append(result)
                    return result
                with mock.patch.object(self.runner, "run", side_effect=execute):
                    self.assertEqual(self.daemon.drain(), 1)
                return results[0]
            return self.runner.run(subject="chat", prompt="go", reply_to=origin, **run_args)

    def interrupts(self, action):
        return [r for r in self.audit.records() if r["kind"] == "interrupt" and r["action"] == action]

    def test_slack_question_answer_reaches_turn_and_does_not_store_wake(self):
        def answer():
            self.now = 299
            self.assertTrue(self.reply())
            # Redelivery is consumed even after the first answer resolves.
        result = self.run_stub([answer], drain_event=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent, [{"id": 7, "result": {
            "answers": {"destination": {"answers": ["archive-bucket"]}}}}])
        [record] = self.interrupts("answered")
        self.assertEqual(record["actor"], "human")
        self.assertEqual(record["data"]["decision"], "answer")
        self.assertEqual(list(self.daemon.store.pending()), [])
        self.slack.client.post.assert_called_once()
        self.assertEqual(self.slack.client.post.call_args.kwargs["thread_ts"], "origin")
        self.assertTrue(self.reply())
        self.assertEqual(list(self.daemon.store.pending()), [])
        self.assertEqual(len(self.interrupts("answered")), 1)
        turn = next(r for r in self.audit.records() if r["kind"] == "turn")
        for record in self.interrupts("asked") + self.interrupts("answered"):
            self.assertEqual(record["span"]["parent_span_id"], turn["span"]["span_id"])

    def test_discord_question_reply_matches_posted_id_and_is_consumed(self):
        bridge = DiscordBridge(bot_token="unused", store=self.daemon.store, audit=self.audit,
                               allow_from=["human"], interrupt_board=self.daemon.interrupt_board)
        bridge.client.post = mock.Mock(return_value=["question-message"])
        bridge.client.react = mock.Mock()
        self.daemon.discord = bridge
        def reply():
            self.now = 20
            self.assertTrue(bridge._store({"id": "reply", "channel_id": "d-channel", "author": {"id": "human"},
                                          "content": "discord answer", "message_reference": {"message_id": "question-message"}}))
        result = self.run_stub([reply], reply_to={"discord": {"channel": "d-channel", "message_id": "original"}})
        self.assertTrue(result.ok, result.error)
        bridge.client.post.assert_called_once()
        self.assertEqual(bridge.client.post.call_args.kwargs["reply_to"], "original")
        self.assertEqual(self.sent[0]["result"]["answers"]["destination"]["answers"], ["discord answer"])
        self.assertEqual(list(self.daemon.store.pending()), [])

    def test_webhook_payload_cannot_choose_chat_reply_origin(self):
        self.daemon.store.append({"id": "forged-origin", "route": "/webhook", "subject": "webhook", "prompt": "inspect",
                                  "payload": {"slack": {"channel": "attacker", "thread_ts": "t"},
                                              "discord": {"channel": "attacker", "message_id": "m"}}, "processed": False})
        result = RunResult(run_id="stub", subject="webhook", status="completed", started_at="now")
        with mock.patch.object(self.runner, "run", return_value=result) as run:
            self.assertEqual(self.daemon.drain(), 1)
        self.assertEqual(run.call_args.kwargs["reply_to"], {})
        self.slack.client.post.assert_not_called()

    def test_secret_question_batch_is_redacted_declined_and_never_posted(self):
        self.immediate = True
        self.params = copy.deepcopy(QUESTION)
        self.params["questions"].append({"id": "private", "question": "SECRET_QUESTION_TEXT",
                                         "header": "SECRET_HEADER", "isSecret": True,
                                         "options": [{"label": "SECRET_OPTION", "description": "SECRET_DESCRIPTION"}]})
        result = self.run_stub([])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent, [{"id": 7, "result": codex.question_decline(params=self.params)}])
        self.slack.client.post.assert_not_called()
        self.assertEqual(result.questions[0]["secret"], True)
        records = list(self.audit.records())
        record = next(r for r in records if r["action"] == "question")
        self.assertEqual(record["status"], "refused")
        self.assertIn("isSecret", record["data"]["reason"])
        self.assertNotIn("SECRET_", json.dumps(records) + json.dumps(result.as_dict()))
        self.assertNotIn("SECRET_", json.dumps(self.client.questions[0].params))

    def test_background_without_chat_declines_immediately_and_has_no_live_control(self):
        self.immediate = True
        self.runner.post_question = None
        result = self.run_stub([], reply_to=None)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent[0]["result"], codex.question_decline(params=QUESTION))
        self.assertEqual(self.client.interrupts, {})
        self.assertEqual(list(self.config.control_dir.glob("*.pending")), [])
        self.assertEqual(self.now, 0)

    def test_failed_chat_post_declines_without_a_300_second_hold(self):
        self.slack.client.post.side_effect = RuntimeError("offline")
        result = self.run_stub([])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent[0]["result"], codex.question_decline(params=QUESTION))
        self.assertEqual(self.now, 0)
        self.assertEqual(self.client._hold_used, 0)
        self.assertEqual(list(self.config.control_dir.glob("*.pending")), [])

    def test_approval_prompt_includes_fenced_truncated_arguments(self):
        self.confirm_profile()
        self.method = "mcpServer/elicitation/request"
        self.params = mcp_approval(_meta={"codex_approval_kind": "mcp_tool_call", "tool_name": "send_message",
                                         "arguments": {"to": "person@example.invalid", "body": "x" * 1000}})
        result = self.run_stub([lambda: self.reply("approve")], profile="confirm")
        self.assertTrue(result.ok, result.error)
        prompt = self.slack.client.post.call_args.args[1]
        self.assertIn("person@example.invalid", prompt)
        self.assertIn("<<untrusted-", prompt)
        self.assertIn("showing 300", prompt)
        self.assertNotIn("x" * 400, prompt)

    def test_answer_audit_keeps_bounded_text_and_full_digest(self):
        text = "é" * 2500
        result = self.run_stub([lambda: self.reply(text)])
        self.assertTrue(result.ok, result.error)
        record = self.interrupts("answered")[0]
        self.assertEqual(record["data"]["text"], text[:2000])
        self.assertEqual(record["data"]["text_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(self.sent[0]["result"]["answers"]["destination"]["answers"], [text])

    def test_unanswered_question_declines_at_300_seconds_and_continues(self):
        def expire():
            self.assertEqual(self.sent, [])
            self.now = 300
        result = self.run_stub([expire])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent[0]["result"], codex.question_decline(params=QUESTION))
        self.assertIsNone(result.stop_reason)
        self.assertEqual(len(self.interrupts("expired")), 1)

    def test_daemon_signal_declines_on_next_tick_within_two_seconds(self):
        def stop():
            self.now = 40
            self.daemon._stopping.set()  # the first action in Daemon.stop(SIGTERM)
        result = self.run_stub([stop])
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(self.sent[0]["result"], codex.question_decline(params=QUESTION))
        self.assertEqual(self.now, 40)  # no additional wait before declining
        self.assertEqual(len(self.interrupts("stopped")), 1)

    def test_unallowlisted_reply_is_audited_ignored_and_cannot_answer(self):
        def replies():
            self.now = 10
            self.reply("attacker", actor="stranger", delivery="unauthorized")
            self.assertEqual(self.sent, [])
            self.assertIsNone(next(iter(self.client.interrupts.values())).text)
            self.reply("safe", delivery="authorized")
        result = self.run_stub([replies])
        self.assertTrue(result.ok, result.error)
        [record] = self.interrupts("ignored")
        self.assertEqual(record["actor"], "stranger")
        self.assertEqual(self.sent[0]["result"]["answers"]["destination"]["answers"], ["safe"])
        self.assertEqual(list(self.daemon.store.pending()), [])

    def confirm_profile(self, server="mail", tool="send_message"):
        raw = {"approvals": [{"server": server, "tool": tool, "verdict": "confirm",
                              "unless_approved_within": 900, "reason": "needs a human"}]}
        if server == "mail":
            raw.update(mcp_servers={"mail": {"command": "unused"}}, mcp_tools={"mail": [tool]})
        else:
            raw.update(tools=[tool])
        self.config.profiles["confirm"] = raw

    def test_confirm_chat_approval_accepts_call_and_second_call_uses_window(self):
        self.confirm_profile()
        self.method = "mcpServer/elicitation/request"
        self.params = mcp_approval()
        def answered(client, payload):
            if payload["id"] == 7:
                self.assertEqual(payload["result"], {"action": "accept", "content": {}})
                client._handle_server_request({"id": 8, "method": self.method, "params": self.params})
            else:
                client._notifications.put(("turn/completed", {"turn": {"status": "completed"}}))
        self.response_hook = answered
        result = self.run_stub([lambda: self.reply("approve")], profile="confirm")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent, [{"id": 7, "result": {"action": "accept", "content": {}}},
                                    {"id": 8, "result": {"action": "accept", "content": {}}}])
        self.slack.client.post.assert_called_once()
        [record] = self.interrupts("answered")
        self.assertEqual(record["data"], {"decision": "approve", "server": "mail", "tool": "send_message"})
        self.assertEqual(record["actor"], "human")
        approvals = [r for r in self.audit.records() if r["kind"] == "approval"]
        self.assertEqual([r["action"] for r in approvals], ["confirm", "proceed"])

    def test_confirm_other_text_declines(self):
        self.confirm_profile()
        self.method, self.params = "mcpServer/elicitation/request", mcp_approval()
        result = self.run_stub([lambda: self.reply("no")], profile="confirm")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent[0]["result"], {"action": "decline"})
        self.assertEqual(self.interrupts("answered")[0]["data"]["decision"], "decline")

    def test_confirm_hosted_tool_waits_and_executes_only_after_yes(self):
        self.confirm_profile("bothy", "bothy_note")
        self.method, self.params = "item/tool/call", {"threadId": "thread", "turnId": "turn", "callId": "note",
                                                                  "tool": "bothy_note", "arguments": {"ref": "T-1"}}
        tools = ToolRegistry()
        handler = mock.Mock(return_value="recorded")
        tools.add(DynamicTool("bothy_note", "stub", {"type": "object"}, handler))
        def reply():
            handler.assert_not_called()
            self.reply("yes")
        with mock.patch.object(self.runner, "_registry", return_value=tools):
            result = self.run_stub([reply], profile="confirm")
        self.assertTrue(result.ok, result.error)
        handler.assert_called_once()
        self.assertTrue(self.sent[0]["result"]["success"])

    def test_cli_file_is_consumed_by_tick_without_bot_origin(self):
        def reply():
            pending = next(iter(self.client.interrupts.values()))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_answer(argparse.Namespace(interrupt_id=pending.id, text="cli answer"), self.config), 0)
        self.runner.post_question = None
        with contextlib.redirect_stderr(io.StringIO()) as hints:
            result = self.run_stub([reply], reply_to=None, foreground=True)
        self.assertIn("bothy answer int_", hints.getvalue())
        self.assertTrue(result.ok, result.error)
        self.slack.client.post.assert_not_called()
        self.assertEqual(self.sent[0]["result"]["answers"]["destination"]["answers"], ["cli answer"])
        self.assertEqual(self.interrupts("answered")[0]["actor"], "cli")
        self.assertEqual(list(self.config.control_dir.glob("*.answer")), [])

    def test_unauditable_approval_cannot_proceed(self):
        self.confirm_profile()
        self.method, self.params = "mcpServer/elicitation/request", mcp_approval()
        def reply():
            original = self.audit.append
            def append(**fields):
                if fields.get("action") == "answered":
                    raise AuditError("disk full")
                return original(**fields)
            with mock.patch.object(self.audit, "append", side_effect=append):
                self.reply("approve")
            self.now = 300
        result = self.run_stub([reply], profile="confirm")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.sent[0]["result"], {"action": "decline"})
        self.assertEqual(self.interrupts("answered"), [])


class HoldLimits(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_load_refuses_equal_and_above_ttl(self):
        for hold in (2700, 2701):
            with self.assertRaisesRegex(ValueError, "reservation_ttl_seconds"):
                load(self.home / "missing", home=self.home, question_hold_seconds=hold)
        self.assertEqual(load(self.home / "missing", home=self.home, question_hold_seconds=2699).question_hold_seconds, 2699)

    def test_profile_and_stored_job_limits_checked_at_load(self):
        with self.assertRaisesRegex(ValueError, "reservation_ttl_seconds"):
            load(self.home / "missing", home=self.home, question_hold_seconds=300,
                 profiles={"long": {"wall_clock_seconds": 3300}})
        config = Config(home=self.home)
        Schedule(config.state_dir / "jobs.json").put(Job(id="long", kind="every", spec="3600", wall_clock_seconds=3300))
        with self.assertRaisesRegex(ValueError, "reservation_ttl_seconds"):
            load(self.home / "missing", home=self.home, question_hold_seconds=300)

    def test_invalid_holds_refused_and_default_is_zero(self):
        self.assertEqual(load(self.home / "missing", home=self.home).question_hold_seconds, 0)
        for hold in (-1, float("inf"), float("nan"), True, "300"):
            with self.assertRaisesRegex(ValueError, "question_hold_seconds"):
                load(self.home / "missing", home=self.home, question_hold_seconds=hold)

    def test_cli_rejects_traversal_and_fsyncs_before_acknowledging(self):
        config = Config(home=self.home)
        for interrupt_id in ("../outside", "int_../x", "int_", "INT_" + "a" * 32):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cmd_answer(argparse.Namespace(interrupt_id=interrupt_id, text="x"), config), 2)
        with control_transaction(config.control_dir):
            write_control(config.control_dir, "int_" + "a" * 32 + ".pending",
                          {"owner": pending_owner("run_test"), "deadline": codex.clock.monotonic() + 300})
        order = []
        with mock.patch("bothy.cli.os.fsync", side_effect=lambda fd: order.append("fsync")), \
                mock.patch("builtins.print", side_effect=lambda *args, **kwargs: order.append("ack")):
            self.assertEqual(cmd_answer(argparse.Namespace(interrupt_id="int_" + "a" * 32, text="answer"), config), 0)
        self.assertEqual(order, ["fsync", "fsync", "ack"])

    def test_cli_rejects_unknown_expired_and_dead_owner_markers_without_answer_files(self):
        config = Config(home=self.home)
        config.ensure_dirs()
        identifier = "int_" + "f" * 32
        args = argparse.Namespace(interrupt_id=identifier, text="late")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_answer(args, config), 1)
        for expires, owner in ((codex.clock.monotonic() - 1, pending_owner("r")),
                               (codex.clock.monotonic() + 300, {**pending_owner("r"), "start_ticks": "wrong identity"})):
            with control_transaction(config.control_dir):
                write_control(config.control_dir, identifier + ".pending", {"owner": owner, "deadline": expires})
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cmd_answer(args, config), 1)
            self.assertFalse((config.control_dir / (identifier + ".answer")).exists())
            self.assertFalse((config.control_dir / (identifier + ".pending")).exists())


    def test_cli_identity_probe_cannot_block_shutdown_decline_or_leave_stale_answer(self):
        config = Config(home=self.home)
        config.ensure_dirs()
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                              control_dir=config.control_dir, on_interrupt=lambda p: True)
        c._send = mock.Mock()
        c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": QUESTION})
        pending = next(iter(c.interrupts.values()))
        c.activate_interrupt(pending)
        entered, release = threading.Event(), threading.Event()
        results = []
        def probe(*args):
            entered.set()
            release.wait(timeout=1)
            return True
        def answer():
            with contextlib.redirect_stderr(io.StringIO()):
                results.append(cmd_answer(argparse.Namespace(interrupt_id=pending.id, text="late"), config))
        with mock.patch("bothy.interrupts.live_pending", side_effect=probe):
            worker = REAL_THREAD(target=answer, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=0.5))
                stopped = threading.Event()
                stop = REAL_THREAD(target=lambda: (c._tick_interrupts(stopping=True), stopped.set()), daemon=True)
                stop.start()
                self.assertTrue(stopped.wait(timeout=0.3), "CLI process probe held the control lock")
                c._send.assert_called_once()
                stop.join(timeout=0.5)
            finally:
                release.set()
                worker.join(timeout=0.5)
        self.assertEqual(results, [1])
        self.assertEqual(list(config.control_dir.glob("*.answer")), [])

    def test_cli_double_dash_keeps_leading_dash_answer_text(self):
        from bothy.cli import main
        config = Config(home=self.home)
        config.ensure_dirs()
        config_path = self.home / "config.json"
        config_path.write_text(json.dumps({"home": str(self.home)}))
        identifier = "int_" + "b" * 32
        with control_transaction(config.control_dir):
            write_control(config.control_dir, identifier + ".pending",
                          {"owner": pending_owner("r"), "deadline": codex.clock.monotonic() + 300})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(config_path), "answer", identifier, "--", "-decline"]), 0)
        self.assertEqual(json.loads((config.control_dir / (identifier + ".answer")).read_text())["text"], "-decline")



class DroveReplies(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.audit = AuditLog(self.home / "audit.jsonl")
        sender = mock.patch("bothy.drove.threading.Thread", ImmediateThread)
        sender.start()
        self.addCleanup(sender.stop)
        self.cli = DroveCli()
        self.cli.run = mock.Mock(return_value=Outcome(ok=True))
        self.forwarder = AttentionForwarder(self.home / "attention.json", self.cli,
                                             lambda n, r: (True, {}), self.audit)
        self.store = WakeStore(self.home / "wakes")
        self.bridge = DiscordBridge(bot_token="unused", store=self.store, audit=self.audit,
                                    allow_from=["human"], attention=self.forwarder)
        self.bridge.client.react = mock.Mock()

    def item(self, kind="question", run="run-1", seat="w-hold"):
        self.forwarder.file.save({"items": {"q": {"state": "open", "refs": {"discord": "posted"},
            "item": {"id": "q", "run": run, "seat": seat, "kind": kind}}}})

    def reply(self, actor="human", text="a quoted 'answer'; $(false)\nsecond line", delivery="reply"):
        return self.bridge._store({"id": delivery, "channel_id": "channel", "author": {"id": actor},
                                   "content": text, "message_reference": {"message_id": "posted"}})

    def test_discord_question_reply_sends_exactly_once_with_text_as_one_argument(self):
        self.item()
        self.assertTrue(self.reply())
        self.assertTrue(self.reply())
        self.cli.run.assert_called_once_with(["drove", "send", "w-hold", "--run", "run-1", "--kind", "answer", "--",
                                             "a quoted 'answer'; $(false)\nsecond line"], timeout=15.0)
        # A new forwarder (daemon restart) also sees the durable dispatch claim.
        self.bridge.attention = AttentionForwarder(self.home / "attention.json", self.cli,
                                                    lambda n, r: (True, {}), self.audit)
        self.reply()
        self.cli.run.assert_called_once()
        self.assertEqual(list(self.store.pending()), [])

    def test_distinct_followup_is_forwarded_but_its_replay_is_consumed(self):
        self.item("blocked")
        self.reply(delivery="first")
        self.reply(text="additional context", delivery="followup")
        self.reply(text="additional context", delivery="followup")
        self.assertEqual(self.cli.run.call_count, 2)

    def test_slow_drove_send_does_not_hold_gateway_or_attention_lock(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow(argv, **kwargs):
            self.assertEqual(kwargs["timeout"], 15.0)
            entered.set()
            release.wait(timeout=1)
            finished.set()
            return Outcome(True)
        self.cli.run.side_effect = slow
        self.item()
        senders = []
        def sender(**kwargs):
            worker = REAL_THREAD(**kwargs)
            senders.append(worker)
            return worker
        try:
            with mock.patch("bothy.drove.threading.Thread", side_effect=sender):
                self.assertTrue(self.reply())
                self.assertTrue(entered.wait(timeout=0.5))
                self.assertFalse(finished.is_set())
                self.assertTrue(self.forwarder.file.lock.acquire(timeout=0.2))
                self.forwarder.file.lock.release()
                self.reply()  # replay remains consumed while the subprocess waits
                self.cli.run.assert_called_once()
        finally:
            release.set()
            self.assertTrue(finished.wait(timeout=0.5))
            for worker in senders:
                worker.join(timeout=0.5)
                self.assertFalse(worker.is_alive())

    def test_asking_and_blocked_are_answerable_but_permission_trust_dialog_are_pane_only(self):
        for kind in ("asking", "blocked"):
            self.item(kind)
            self.reply(delivery=kind)
        self.assertEqual(self.cli.run.call_count, 2)
        for kind in ("permission", "trust", "dialog"):
            self.item(kind)
            self.reply(delivery=kind)
        self.assertEqual(self.cli.run.call_count, 2)
        self.assertEqual(list(self.store.pending()), [])

    def test_discord_allowlist_and_invalid_seat_or_run_prevent_send(self):
        self.item()
        self.reply(actor="stranger")
        for run, seat in (("--run", "seat"), ("run", "../seat"), ("run", "--flag")):
            self.item(run=run, seat=seat)
            self.reply()
        self.cli.run.assert_not_called()


class ProtocolSafety(unittest.TestCase):
    def test_slow_approved_hosted_tool_does_not_block_another_answer_or_registration(self):
        entered, release, answered, ended = (threading.Event() for _ in range(4))
        def slow(args):
            entered.set()
            release.wait(timeout=1)
            return "done"
        tools = ToolRegistry([DynamicTool("slow", "slow", {}, slow)])
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                              on_interrupt=lambda p: True, tools=tools,
                              tool_approval=lambda p: codex.ApprovalDecision("decline", hold=True))
        c._send = mock.Mock()
        c._handle_server_request({"id": 1, "method": "item/tool/call", "params": {"tool": "slow", "arguments": {}}})
        first = next(iter(c.interrupts.values()))
        c.activate_interrupt(first)
        self.assertTrue(c.answer(first.id, "approve"))
        c._handle_server_request({"id": 2, "method": "item/tool/requestUserInput", "params": QUESTION})
        second = next(p for p in c.interrupts.values() if p.rpc_id == 2)
        c.activate_interrupt(second)
        def tick():
            try:
                c._tick_interrupts()
            finally:
                ended.set()
        worker = REAL_THREAD(target=tick, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=0.5))
        results = []
        def answer():
            results.append(c.answer(second.id, "another answer"))
            c._handle_server_request({"id": 3, "method": "item/tool/requestUserInput", "params": QUESTION})
            answered.set()
        responder = REAL_THREAD(target=answer, daemon=True)
        try:
            responder.start()
            self.assertTrue(answered.wait(timeout=0.3), "approved tool held the pending lock")
            self.assertEqual(results, [True])
            self.assertTrue(any(p.rpc_id == 3 for p in c.interrupts.values()))
        finally:
            release.set()
            self.assertTrue(ended.wait(timeout=0.5))
            responder.join(timeout=0.5)

    def test_implicit_slack_and_discord_ambiguity_requires_id_and_replies_with_choices(self):
        for source in ("slack", "discord"):
            audit, notify = mock.Mock(), mock.Mock()
            board = InterruptBoard(audit, notify)
            callbacks = [mock.Mock(return_value=True), mock.Mock(return_value=True)]
            for index in range(2):
                board.register(Entry("int_" + str(index) * 32, 300, "r", "s",
                                     {source: {"channel": "c", "thread_ts": "t"}}, callbacks[index], refs={"t"}))
            with mock.patch("bothy.interrupts.clock.monotonic", return_value=0), \
                    mock.patch("bothy.interrupts.threading.Thread", ImmediateThread):
                self.assertTrue(board.receive(source=source, channel="c", reference="t", text="yes",
                                              actor="human", allowed=True, delivery="ambiguous"))
                for callback in callbacks:
                    callback.assert_not_called()
                self.assertEqual(audit.append.call_args.kwargs["action"], "ambiguous")
                choices = notify.call_args.args[1]
                self.assertIn("int_" + "0" * 32, choices)
                self.assertIn("int_" + "1" * 32, choices)
                board.receive(source=source, channel="c", reference="t", text="answer int_" + "1" * 32 + " yes",
                              actor="human", allowed=True, delivery="chosen")
            callbacks[0].assert_not_called()
            callbacks[1].assert_called_once_with("yes", "human")

    def test_rejected_and_expired_matches_are_audited_and_replied_without_delivery(self):
        for expires in (300, -1):
            audit, notify = mock.Mock(), mock.Mock()
            callback = mock.Mock(return_value=False)
            board = InterruptBoard(audit, notify)
            board.register(Entry("int_" + "a" * 32, expires, "r", "s",
                                 {"slack": {"channel": "c", "thread_ts": "t"}}, callback))
            with mock.patch("bothy.interrupts.clock.monotonic", return_value=0), \
                    mock.patch("bothy.interrupts.threading.Thread", ImmediateThread):
                self.assertTrue(board.receive(source="slack", channel="c", reference="t", text="reply",
                                              actor="human", allowed=True, delivery="d"))
            self.assertEqual(audit.append.call_args.kwargs["action"], "failed" if expires > 0 else "late")
            self.assertIn("closed", notify.call_args.args[1])

    def test_reader_registers_without_waiting_and_notifier_cannot_stall_stop(self):
        entered, release = threading.Event(), threading.Event()
        def notifier(pending):
            entered.set()
            release.wait(timeout=1)
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True, on_interrupt=notifier)
        sent = []
        c._send = sent.append
        c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": QUESTION})
        self.assertFalse(entered.is_set())
        self.assertEqual(sent, [])
        try:
            c._tick_interrupts()
            self.assertTrue(entered.wait(timeout=0.5))
            c._tick_interrupts(stopping=True)
            self.assertEqual(sent[0]["result"], codex.question_decline(params=QUESTION))
        finally:
            release.set()

    def test_zero_hold_and_missing_reply_channel_keep_immediate_decline(self):
        for seconds, notify in ((0, lambda pending: None), (300, None)):
            c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=seconds, reply_channel=notify is not None, on_interrupt=notify)
            sent = []
            c._send = sent.append
            c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": QUESTION})
            self.assertEqual(sent[0]["result"], codex.question_decline(params=QUESTION))
            self.assertEqual(c.interrupts, {})

    def test_unknown_explicit_answer_is_ignored_without_becoming_a_wake(self):
        audit = mock.Mock()
        board = InterruptBoard(audit)
        self.assertTrue(board.receive(source="slack", channel="c", reference="", text="answer int_" + "a" * 32 + " late",
                                      actor="human", allowed=True, delivery="late"))
        self.assertEqual(audit.append.call_args.kwargs["action"], "late")

    def test_pending_holds_and_notifier_threads_have_ceilings(self):
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                              on_interrupt=lambda p: True)
        sent = []
        c._send = sent.append
        for index in range(17):
            c._handle_server_request({"id": index, "method": "item/tool/requestUserInput", "params": QUESTION})
        self.assertEqual(len(c.interrupts), 16)
        self.assertEqual(len(sent), 1)
        slots = threading.BoundedSemaphore(2)
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        with mock.patch("bothy.codex._NOTIFY_SLOTS", slots), mock.patch("bothy.codex.threading.Thread") as thread:
            c._tick_interrupts()
            thread.assert_not_called()
            self.assertFalse(any(p.announced for p in c.interrupts.values()))
        slots.release()
        slots.release()

    def test_held_time_extends_wall_clock_with_a_total_hard_cap(self):
        now = [0.0]
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                              on_interrupt=lambda pending: True)
        sent = []
        c._send = sent.append
        def advance(value):
            now[0] = value
        c._notifications = VirtualInbox([lambda: advance(300), lambda: advance(1199), lambda: advance(1200)])
        def request(method, *args, **kwargs):
            if method == "turn/start":
                c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": QUESTION})
            if method == "turn/interrupt":
                c._notifications.put(("turn/completed", {"turn": {"status": "interrupted"}}))
            return {"turn": {"id": "turn"}}
        c.request = request
        with mock.patch("bothy.codex.clock.monotonic", side_effect=lambda: now[0]), \
                mock.patch("bothy.codex.threading.Thread", ImmediateThread):
            outcome = c.run_turn(thread_id="thread", text="go", wall_clock_seconds=900)
        self.assertEqual(outcome.stop_reason, "wall_clock")
        self.assertEqual(now[0], 1200)
        self.assertEqual(c._hold_used, 300)
        self.assertEqual(sent[0]["result"], codex.question_decline(params=QUESTION))

    def test_repeated_questions_share_one_total_hold_allowance(self):
        now = [0.0]
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                              on_interrupt=lambda pending: True)
        def answer_at(value):
            now[0] = value
            self.assertTrue(c.answer(next(iter(c.interrupts)), "text"))
        def advance(value):
            now[0] = value
        c._notifications = VirtualInbox([lambda: answer_at(200), lambda: None, lambda: answer_at(499),
                                         lambda: advance(1199), lambda: advance(1200)])
        def send(payload):
            if payload["id"] == 7:
                c._handle_server_request({"id": 8, "method": "item/tool/requestUserInput", "params": QUESTION})
        c._send = send
        def request(method, *args, **kwargs):
            if method == "turn/start":
                c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": QUESTION})
            if method == "turn/interrupt":
                c._notifications.put(("turn/completed", {"turn": {"status": "interrupted"}}))
            return {"turn": {"id": "turn"}}
        c.request = request
        with mock.patch("bothy.codex.clock.monotonic", side_effect=lambda: now[0]), \
                mock.patch("bothy.codex.threading.Thread", ImmediateThread):
            outcome = c.run_turn(thread_id="thread", text="go", wall_clock_seconds=900)
        self.assertEqual(outcome.stop_reason, "wall_clock")
        self.assertEqual(now[0], 1200)
        self.assertEqual(c._hold_used, 300)

    def test_approval_and_legacy_methods_have_central_wire_builders(self):
        self.assertEqual(codex.approval_answer("mcpServer/elicitation/request", "cancel"), {"action": "cancel"})
        modern = {"threadId": "thread", "turnId": "turn", "itemId": "item", "startedAtMs": 0}
        fixtures = [
            ("item/commandExecution/requestApproval", modern, {"decision": "accept"}),
            ("item/fileChange/requestApproval", modern, {"decision": "accept"}),
            ("item/permissions/requestApproval", {**modern, "cwd": "/tmp", "permissions": {"network": {"enabled": True}}},
             {"permissions": {}, "scope": "turn"}),
            ("execCommandApproval", {"callId": "call", "conversationId": "thread", "cwd": "/tmp", "command": ["true"],
                                     "parsedCmd": []}, {"decision": "approved"}),
            ("applyPatchApproval", {"callId": "call", "conversationId": "thread", "fileChanges": {}},
             {"decision": "approved"}),
        ]
        for method, params, expected in fixtures:
            c = codex.CodexClient(run_id="r", codex_home="unused", approval=lambda m, p: codex.ApprovalDecision("accept", reason="audit only"))
            sent = []
            c._send = sent.append
            c._handle_server_request({"id": 1, "method": method, "params": params})
            self.assertEqual(sent, [{"id": 1, "result": expected}])

    def test_secret_question_is_redacted_before_every_observer_even_with_zero_hold(self):
        params = copy.deepcopy(QUESTION)
        params["questions"][0].update(isSecret=True, question="SECRET_BODY", header="SECRET_HEADER")
        for seconds in (0, 300):
            events, questions, sent = [], [], []
            c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=seconds,
                                  on_interrupt=lambda p: self.fail("secret question was held"),
                                  on_event=lambda method, body: events.append(body), on_question=questions.append)
            c._send = sent.append
            c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": params})
            self.assertEqual(sent, [{"id": 7, "result": codex.question_decline(params=params)}])
            self.assertEqual(c.interrupts, {})
            observed = json.dumps(events) + json.dumps(codex.TurnOutcome(status="completed", questions=questions).as_dict())
            self.assertNotIn("SECRET_", observed)

    def test_mcp_observed_identity_order_and_message_fallback(self):
        for meta, message, expected in [
            ({"codex_approval_kind": "mcp_tool_call", "tool_name": "name", "tool_title": "title"}, 'run tool "quoted"', "name"),
            ({"codex_approval_kind": "mcp_tool_call", "tool_title": "title"}, 'run tool "quoted"', "title"),
            ({"codex_approval_kind": "mcp_tool_call"}, 'Allow connector to run tool "quoted"', "quoted"),
            ({"codex_approval_kind": "mcp_tool_call", "tool_name": 42}, 'run tool "quoted"', "quoted"),
            ({"tool_name": "name"}, 'run tool "quoted"', ""),
            ("untyped metadata", 'run tool "quoted"', ""),
        ]:
            self.assertEqual(policy.identify("mcpServer/elicitation/request", mcp_approval(_meta=meta, message=message)), ("mail", expected))

    def test_real_mcp_forms_and_urls_decline_even_with_a_proceed_rule_or_custom_accept(self):
        facts = policy.Facts(rules=(policy.ApprovalRule("mail", "send_message", "proceed"),))
        form = mcp_approval(_meta={"tool_name": "send_message"})
        url = {"serverName": "mail", "threadId": "thread", "mode": "url", "elicitationId": "auth",
               "message": 'Open authentication for tool "send_message"', "url": "https://example.invalid/auth"}
        for params in (form, url, mcp_approval(_meta={"codex_approval_kind": "other", "tool_name": "send_message"})):
            decision = policy.evaluate("mcpServer/elicitation/request", params, facts)
            self.assertEqual(decision.verdict, "deny")
            c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                                  on_interrupt=lambda p: self.fail("form was held"),
                                  approval=lambda m, p: codex.ApprovalDecision("accept", hold=True))
            sent = []
            c._send = sent.append
            c._handle_server_request({"id": 1, "method": "mcpServer/elicitation/request", "params": params})
            self.assertEqual(sent, [{"id": 1, "result": {"action": "decline"}}])
            self.assertEqual(c.interrupts, {})

    def test_permission_requests_always_get_empty_turn_grant(self):
        params = {"threadId": "thread", "turnId": "turn", "itemId": "permissions", "cwd": "/tmp", "startedAtMs": 0,
                  "permissions": {"network": {"enabled": True}, "fileSystem": {"write": ["/tmp"]}},
                  "server": "mail", "tool": "send_message"}
        facts = policy.Facts(rules=(policy.ApprovalRule("mail", "send_message", "proceed"),))
        self.assertEqual(policy.evaluate("item/permissions/requestApproval", params, facts).verdict, "deny")
        self.assertEqual(codex.approval_answer("item/permissions/requestApproval", "accept"), {"permissions": {}, "scope": "turn"})

    def test_legacy_decline_uses_schema_rejection_and_modern_reasons_stay_off_wire(self):
        for method in ("execCommandApproval", "applyPatchApproval"):
            self.assertEqual(codex.approval_answer(method, "decline", "denied in audit"),
                             {"decision": {"denied": {"rejection": "denied in audit"}}})
        self.assertEqual(codex.approval_answer("item/commandExecution/requestApproval", "decline", "audit only"),
                         {"decision": "decline"})
        self.assertEqual(codex.approval_answer("mcpServer/elicitation/request", "accept"), {"action": "accept", "content": {}})

    def test_guide_reason_reaches_hosted_tool_content_text(self):
        tools = ToolRegistry([DynamicTool("bothy_note", "note", {}, mock.Mock())])
        c = codex.CodexClient(run_id="r", codex_home="unused", tools=tools,
                              tool_approval=lambda p: codex.ApprovalDecision("decline", reason="use the task comment"))
        sent = []
        c._send = sent.append
        c._handle_server_request({"id": 1, "method": "item/tool/call", "params": {
            "threadId": "thread", "turnId": "turn", "callId": "note", "tool": "bothy_note", "arguments": {}}})
        self.assertFalse(sent[0]["result"]["success"])
        self.assertEqual(sent[0]["result"]["contentItems"], [{"type": "inputText", "text": "use the task comment"}])
        tools._tools["bothy_note"].handler.assert_not_called()

    def test_expired_answer_cannot_create_a_policy_approval(self):
        c = codex.CodexClient(run_id="r", codex_home="unused", question_hold_seconds=300, reply_channel=True,
                              on_interrupt=lambda p: True, on_answer=mock.Mock(return_value=True))
        c._send = mock.Mock()
        with mock.patch("bothy.codex.clock.monotonic", return_value=0):
            c._handle_server_request({"id": 7, "method": "item/tool/requestUserInput", "params": QUESTION})
        pending = next(iter(c.interrupts.values()))
        with mock.patch("bothy.codex.clock.monotonic", return_value=0):
            c.activate_interrupt(pending)
        with mock.patch("bothy.codex.clock.monotonic", return_value=300):
            self.assertFalse(c.answer(pending.id, "approve"))
        c._on_answer.assert_not_called()

    def test_explicit_id_selects_correct_hold_in_shared_slack_thread(self):
        board = InterruptBoard(mock.Mock())
        answers = [mock.Mock(return_value=True), mock.Mock(return_value=True)]
        for index in range(2):
            board.register(Entry("int_" + str(index) * 32, 300, "r", "s",
                                 {"slack": {"channel": "c", "thread_ts": "t"}}, answers[index]))
        with mock.patch("bothy.interrupts.clock.monotonic", return_value=0):
            self.assertTrue(board.receive(source="slack", channel="c", reference="t",
                                          text="answer int_" + "1" * 32 + " second answer", actor="human",
                                          allowed=True, delivery="d"))
        answers[0].assert_not_called()
        answers[1].assert_called_once_with("second answer", "human")


if __name__ == "__main__":
    unittest.main()
