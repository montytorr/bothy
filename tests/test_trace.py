"""Span export end to end with a protocol stub and loopback HTTP only."""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bothy import clock, codex
from bothy.audit import AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.capability import ToolRegistry
from bothy.cli import main
from bothy.config import Config, load
from bothy.pool import Admission
from bothy.proc import ProcessRegistry
from bothy.runner import Runner
from bothy.trace import build_trace, post_trace, render_trace, span_fields, summarize_items


def spans(trace):
    return trace["resourceSpans"][0]["scopeSpans"][0]["spans"]


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(home=Path(self.temp.name), budget=BudgetPolicy(mode="api"),
                             codex_credentials_dir=Path(self.temp.name) / "credentials")
        self.config.ensure_dirs()
        self.audit = AuditLog(self.config.audit_path)
        self.admission = Admission(self.config.pool, BudgetLedger(self.config.budget_path, self.config.budget))
        self.registry = ProcessRegistry(self.config.registry_path)
        self.runner = Runner(self.config, admission=self.admission, audit=self.audit, registry=self.registry)
        self.run_id = "run_trace_test"
        self.config_path = self.config.home / "config.json"
        self.config_path.write_text(json.dumps({"home": str(self.config.home), "budget": {"mode": "api"}}))

    def run_stub(self, *, failed=False, raise_turn=False, hosted_calls=2, rotate=False):
        audit = self.audit

        class Stub(codex.CodexClient):
            def start(self):
                self._popen = SimpleNamespace(pid=12345)
                return self._popen

            def handshake(self):
                if rotate:
                    audit.rotate()

            def start_thread(self, **kwargs):
                return "thread-1"

            def request(self, method, params=None, **kwargs):
                if method == "turn/start":
                    if raise_turn:
                        raise codex.CodexError("stub turn failure")
                    self._notifications.put(("thread/tokenUsage/updated", {
                        "tokenUsage": {"total": {"inputTokens": 11, "outputTokens": 22, "cachedInputTokens": 3}}}))
                    # A real server may request tools immediately after turn/start.
                    for index in range(hosted_calls):
                        self._handle_tool_call({"id": index}, {"tool": "stub_tool", "arguments": "x" * 900,
                                                                "turnId": "turn-1", "threadId": "thread-1"})
                    self._notifications.put(("item/started", {"observed_at": "2026-10-13T07:00:00Z",
                                                               "item": {"id": "command", "type": "commandExecution"}}))
                    self._notifications.put(("item/completed", {"observed_at": "2026-10-13T07:00:01Z", "item": {
                        "id": "command", "type": "commandExecution", "command": "x" * 900,
                        "aggregatedOutput": "unbounded output" * 900}}))
                    if failed:
                        self._notifications.put(("error", {"error": {"message": "failed", "codexErrorInfo": "serverError"}}))
                    self._notifications.put(("turn/completed", {"turn": {"status": "failed" if failed else "completed"}}))
                    return {"turn": {"id": "turn-1"}}
                return {}

            def _send(self, payload):
                pass

            def close(self, **kwargs):
                return "terminated"

        tools = ToolRegistry()
        tools.call = lambda name, args: (True, "done")
        with mock.patch("bothy.runner.codex.CodexClient", Stub), \
                mock.patch.object(self.runner, "_registry", return_value=tools), \
                mock.patch.object(self.registry, "register"), \
                mock.patch("bothy.runner.ids.run_id", return_value=self.run_id):
            return self.runner.run(subject="span-test", prompt="go")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.config_path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_has_root_turn_and_two_hosted_tool_children(self):
        result = self.run_stub()
        self.assertTrue(result.ok, result.error)
        code, output, error = self.cli("trace", self.run_id, "--json")
        self.assertEqual((code, error), (0, ""))
        trace = json.loads(output)
        made = spans(trace)
        self.assertEqual(len(made), 4)
        root = next(s for s in made if "parentSpanId" not in s)
        turn = next(s for s in made if s["name"].startswith("turn "))
        tools = [s for s in made if s["name"].startswith("tool ")]
        self.assertEqual(turn["parentSpanId"], root["spanId"])
        self.assertEqual([s["parentSpanId"] for s in tools], [turn["spanId"]] * 2)
        expected = hashlib.sha256((self.run_id + "\0trace").encode()).hexdigest()[:32]
        self.assertEqual({s["traceId"] for s in made}, {expected})
        for span in made:
            self.assertRegex(span["traceId"], r"^[0-9a-f]{32}$")
            self.assertRegex(span["spanId"], r"^[0-9a-f]{16}$")
            self.assertRegex(span["startTimeUnixNano"], r"^\d+$")
            self.assertEqual(span["status"]["code"], 1)
        attrs = {a["key"]: a["value"] for a in turn["attributes"]}
        self.assertEqual(attrs["gen_ai.usage.input_tokens"], {"intValue": "11"})
        self.assertEqual(attrs["gen_ai.usage.output_tokens"], {"intValue": "22"})
        finished = next(r for r in self.audit.records() if r["kind"] == "turn" and r["action"] == "finished")
        item = finished["data"]["items"][0]
        self.assertEqual(len(item["arguments"]), 500)
        self.assertNotIn("aggregatedOutput", item)
        self.assertEqual((item["start"], item["end"]), ("2026-10-13T07:00:00Z", "2026-10-13T07:00:01Z"))
        started = next(r for r in self.audit.records() if r["kind"] == "turn" and r["action"] == "started")
        self.assertEqual(started["data"]["turn_id"], "turn-1")
        self.assertEqual(len([r for r in self.audit.records() if r["kind"] == "tool"]), 2)
        self.assertEqual(code, 0)
        code, output, _ = self.cli("trace", self.run_id)
        self.assertEqual(code, 0)
        self.assertIn("\n  turn turn-1", output)
        self.assertEqual(output.count("\n    tool stub_tool"), 2)
        self.assertEqual(self.audit.verify(), len(list(self.audit.records())))

    def test_trace_and_audit_run_read_rotated_admission(self):
        self.assertTrue(self.run_stub(rotate=True).ok)
        self.assertFalse(any(r["action"] == "admitted" for r in self.audit.records()))
        made = spans(build_trace(self.audit, self.run_id))
        self.assertEqual(len([s for s in made if "parentSpanId" not in s]), 1)
        code, output, _ = self.cli("audit", "--run", self.run_id, "--json")
        self.assertEqual(code, 0)
        self.assertIn("admitted", [json.loads(line)["action"] for line in output.splitlines()])
        self.assertEqual(build_trace(self.config.home, self.run_id), build_trace(self.audit, self.run_id))

    def test_old_rotated_run_falls_back_to_admitted_finished_times(self):
        admitted = self.audit.append(kind="run", action="admitted", run_id=self.run_id)
        self.audit.rotate()
        finished = self.audit.append(kind="run", action="finished", run_id=self.run_id)
        root, = spans(build_trace(self.audit, self.run_id))
        self.assertEqual(root["startTimeUnixNano"], str(int(clock.parse(admitted["ts"]).timestamp() * 1_000_000) * 1000))
        self.assertGreaterEqual(int(root["endTimeUnixNano"]), int(root["startTimeUnixNano"]))
        self.assertEqual(root["status"]["code"], 1)

    def test_failed_turn_is_otlp_error(self):
        result = self.run_stub(failed=True)
        self.assertEqual(result.status, "failed")
        made = spans(build_trace(self.audit, self.run_id))
        turn = next(s for s in made if s["name"].startswith("turn "))
        self.assertEqual(turn["status"], {"code": 2})
        attrs = {a["key"]: a["value"] for a in turn["attributes"]}
        self.assertEqual(attrs["bothy.error_code"], {"stringValue": "serverError"})

    def test_exception_in_turn_still_closes_turn_and_root(self):
        self.assertEqual(self.run_stub(raise_turn=True).status, "failed")
        made = spans(build_trace(self.audit, self.run_id))
        self.assertEqual(len(made), 2)
        self.assertTrue(all(s["status"]["code"] == 2 for s in made))
        self.assertEqual(self.admission.snapshot()["lanes"], {})

    def test_export_deadline_settles_before_a_non_answering_endpoint(self):
        release = threading.Event()
        accepted = threading.Event()
        observed = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(outer.admission.snapshot()["lanes"])
                accepted.set()
                release.wait(5)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.config.otlp_endpoint = f"http://127.0.0.1:{server.server_port}"
        started = time.monotonic()
        try:
            result = self.run_stub(hosted_calls=0)
            elapsed = time.monotonic() - started
            self.assertTrue(result.ok, result.error)
            self.assertTrue(accepted.is_set())
            self.assertGreater(elapsed, 1.8)
            self.assertLess(elapsed, 2.8)
            self.assertEqual(observed, [{}])
            failed = [r for r in self.audit.records() if r["kind"] == "export" and r["action"] == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertFalse(failed[0]["data"]["retry"])
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_otlp_headers_and_endpoint_path(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers["Authorization"],
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        trace = build_trace([], "missing")
        try:
            with mock.patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20test"}):
                post_trace(trace, f"http://127.0.0.1:{server.server_port}/base/v1/traces")
            self.assertEqual(received, [("/base/v1/traces", "Bearer test", trace)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_items_are_capped_and_arguments_truncated(self):
        items = [{"id": str(i), "type": "mcpToolCall", "arguments": {"text": "z" * 1000}} for i in range(250)]
        summary = summarize_items(items)
        self.assertEqual(len(summary), 200)
        self.assertTrue(all(len(item["arguments"]) == 500 for item in summary))

    def test_unknown_run_cli_fails(self):
        code, output, error = self.cli("trace", "missing", "--json")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("no recorded run", error)

    def test_endpoint_is_a_real_config_field(self):
        self.config_path.write_text(json.dumps({"otlp_endpoint": "http://127.0.0.1:4318"}))
        self.assertEqual(load(self.config_path).otlp_endpoint, "http://127.0.0.1:4318")
        self.assertIsNone(Config().otlp_endpoint)

    def test_timestamps_use_exact_microseconds_and_offsets(self):
        start = "2026-10-13T09:00:00.123456+02:00"
        span = span_fields(self.run_id, "run", start=start, end=start, status="ok")
        trace = build_trace([{"kind": "run", "action": "finished", "run_id": self.run_id,
                              "ts": start, "span": span}], self.run_id)
        root, = spans(trace)
        self.assertEqual(root["startTimeUnixNano"], "1791874800123456000")


if __name__ == "__main__":
    unittest.main()
