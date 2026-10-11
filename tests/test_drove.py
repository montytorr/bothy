"""Drove, started and listened to as a plain process.

The other side is written independently: a small executable standing in for
`drove`, whose replies are the shapes in Drove's README ("Unattended runs") and
src/attention.ts, not anything derived from bothy.drove. Nothing here needs
herdr, Cairn or a network.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from bothy import clock
from bothy.alert import Alerter, CompositeSink, DiscordWebhookSink, StderrSink
from bothy.audit import AuditLog
from bothy.budget import BudgetLedger, BudgetPolicy
from bothy.config import Config
from bothy.daemon import Daemon
from bothy.drove import (
    MAX_DELIVERY_ATTEMPTS, MAX_SENDS_PER_POLL, UNKNOWN_ALERT_AFTER, ActionError, AttentionForwarder,
    DroveCli, DroveRuns, check_action, format_item, parse_action, resolve_action,
)
from bothy.pool import Admission, PoolPolicy
from bothy.proc import ProcessRegistry
from bothy.schedule import Job, Schedule, ScheduleError
from bothy.wake import Route, WakeServer, WakeStore

FAKE = f"""#!{sys.executable}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "home": os.environ.get("DROVE_HOME")}}) + "\\n")
path = os.path.join(here, "script.json")
script = json.load(open(path))
reply = script.get(argv[0])
if reply is None:
    sys.stderr.write(json.dumps({{"ok": False, "code": "usage", "error": "no reply scripted"}}))
    sys.exit(2)
if isinstance(reply, list):
    current = reply[0]
    if len(reply) > 1:
        script[argv[0]] = reply[1:]
        json.dump(script, open(path, "w"))
    reply = current
if reply.get("sleep"):
    time.sleep(reply["sleep"])
sys.stdout.write(reply.get("stdout", ""))
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("exit", 0))
"""


def started(run: str = "drove-20261011-0200-a1b2", **extra: Any) -> dict[str, Any]:
    return {"exit": 0, "stdout": json.dumps({
        "run": run, "repo": "/srv/repo", "status": "up", "workspace": "w2", "tab": "w2:t7",
        "planner": {"kind": "claude", "pane": "w2:p7", "name": "planner-a1b2", "state": "working"},
        "goal": "DROVE-5: Detach the planner", "cairn": "DROVE-5", **extra})}


def refusal(code: str, error: str = "no") -> dict[str, Any]:
    return {"exit": 1, "stderr": json.dumps({"ok": False, "code": code, "error": error})}


def roster(status: str = "up", planner: str = "working", last: dict | None = None) -> dict[str, Any]:
    return {"exit": 0, "stdout": json.dumps({
        "run": {"id": "drove-20261011-0200-a1b2", "status": status}, "seats": [], "attention": [],
        "planner": {"state": planner}, "lastReport": last, "updatedAt": clock.iso()})}


def item(suffix: str = "trust:1", *, kind: str = "trust", answer: dict | None = None,
         run: str = "drove-20261011-0200-a1b2", text: str = "Claude is asking to trust the folder") -> dict:
    return {"id": f"{run}:w-api:{suffix}", "run": run, "seat": "w-api", "kind": kind, "text": text,
            "since": "2026-10-11T02:20:00.000Z",
            "answer": answer or {"type": "pane", "pane": "w2:p9",
                                 "inspect": f"drove capture w-api --run {run}", "note": "Answer it."}}


def snapshot(*items: dict, runs: list[str] | None = None) -> dict[str, Any]:
    return {"exit": 0, "stdout": json.dumps({
        "v": 1, "at": clock.iso(), "runs": runs if runs is not None else ["drove-20261011-0200-a1b2"],
        "items": list(items)})}


class Recorder:
    """Stands in for the Alerter: records what was said and whether it 'arrived'."""

    def __init__(self) -> None:
        self.host = "test-site"
        self.said: list[str] = []
        self.incidents: list[dict[str, Any]] = []
        self.delivering = True

    def say(self, message: str) -> None:
        self.said.append(message)

    def incident(self, **kw: Any) -> bool:
        self.incidents.append(kw)
        return True

    def resolved(self, **kw: Any) -> None:
        pass

    def deliver(self, message: str) -> bool:
        if self.delivering:
            self.said.append(message)
        return self.delivering


class FakeDroveMixin:
    def make_fake(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.binary = self.dir / "drove"
        self.binary.write_text(FAKE)
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IEXEC)
        self.script: dict[str, Any] = {}
        self.write_script()
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.alerts = Recorder()

    def write_script(self) -> None:
        (self.dir / "script.json").write_text(json.dumps(self.script))

    def reply(self, verb: str, value: Any) -> None:
        self.script[verb] = value
        self.write_script()

    def calls(self) -> list[dict[str, Any]]:
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def cli(self, **kwargs: Any) -> DroveCli:
        return DroveCli(binary=str(self.binary), **kwargs)

    def runs(self, **kwargs: Any) -> DroveRuns:
        kwargs.setdefault("repo", str(self.repo))
        return DroveRuns(self.dir / "drove-runs.json", self.cli(timeout=10), self.audit, self.alerts, **kwargs)

    def audit_actions(self, kind: str = "drove") -> list[tuple[str, str]]:
        return [(r["action"], r["status"]) for r in self.audit.records() if r["kind"] == kind]


class ActionTests(unittest.TestCase):
    def test_the_three_shapes_become_the_commands_the_readme_documents(self) -> None:
        self.assertEqual(
            parse_action({"action": "plan", "from_cairn": "DROVE-5"}).argv("drove"),
            ["drove", "plan", "--from-cairn", "DROVE-5", "--detached", "--json"])
        self.assertEqual(
            parse_action({"action": "plan", "next": "DROVE"}).argv("drove"),
            ["drove", "plan", "--next", "DROVE", "--detached", "--json"])
        self.assertEqual(
            parse_action({"action": "up", "run": "drove-20261009-1042-a3f1"}).argv("drove"),
            ["drove", "up", "--run", "drove-20261009-1042-a3f1", "--detached", "--json"])

    def test_a_value_that_could_be_an_option_or_a_second_command_is_refused(self) -> None:
        """A signed webhook proves who sent it, not that its contents are safe."""
        for value in ("--repo", "-x", "DROVE-5 --goal x", "DROVE-5;rm", "DROVE-5\n", "$(id)", "", "5-DROVE"):
            with self.subTest(value=value), self.assertRaises(ActionError):
                parse_action({"action": "plan", "from_cairn": value})
        with self.assertRaises(ActionError):
            parse_action({"action": "up", "run": "--detached"})

    def test_an_action_names_exactly_one_target_and_no_strangers(self) -> None:
        for bad in ({"action": "plan"}, {"action": "plan", "next": "A", "from_cairn": "A-1"},
                    {"action": "plan", "run": "x"}, {"action": "up", "next": "A"},
                    {"action": "down", "run": "x"}, {"action": "plan", "next": "A", "extra": 1},
                    {"action": "plan", "next": "A", "repo": "relative/path"}, "plan", None):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                parse_action(bad)

    def test_a_placeholder_is_filled_from_the_payload_and_checked_like_a_literal(self) -> None:
        template = {"action": "plan", "from_cairn": {"payload": "task.ref"}}
        check_action(template)
        self.assertEqual(resolve_action(template, {"task": {"ref": "DROVE-12"}}),
                         {"action": "plan", "from_cairn": "DROVE-12"})
        for payload in ({}, {"task": {"ref": 12}}, {"task": {"ref": "--repo /"}}, {"task": {"ref": ["X-1"]}}):
            with self.subTest(payload=payload), self.assertRaises(ActionError):
                resolve_action(template, payload)

    def test_a_refusal_does_not_repeat_what_a_stranger_wrote(self) -> None:
        try:
            resolve_action({"action": "plan", "next": {"payload": "p"}}, {"p": "evil value"})
        except ActionError as exc:
            self.assertNotIn("evil value", str(exc))
        else:
            self.fail("should have refused")

    def test_a_job_cannot_carry_a_placeholder_it_has_no_payload_to_fill(self) -> None:
        schedule = Schedule(Path(tempfile.mkdtemp()) / "jobs.json")
        with self.assertRaises(ScheduleError):
            schedule.put(Job(id="n", kind="every", spec="3600",
                             drove={"action": "plan", "next": {"payload": "x"}}))
        job = schedule.put(Job(id="n", kind="every", spec="3600", drove={"action": "plan", "next": "DROVE"}))
        self.assertEqual(job.prompt, "")
        self.assertEqual(schedule.get("n").drove, {"action": "plan", "next": "DROVE"})


class RouteTests(unittest.TestCase):
    """A webhook can name a task. It cannot name a command."""

    def setUp(self) -> None:
        import hashlib
        import hmac

        self.hmac, self.hashlib = hmac, hashlib
        self.store = WakeStore(Path(tempfile.mkdtemp()))
        self.server = WakeServer(
            store=self.store, port=0,
            routes=[Route(path="/drove", secret="s",
                          drove={"action": "plan", "from_cairn": {"payload": "task.ref"}}),
                    Route(path="/nightly", secret="s", drove={"action": "plan", "next": "DROVE"})])
        self.server.serve_forever_in_background()
        self.host, self.port = self.server.address

    def tearDown(self) -> None:
        self.server.shutdown()

    def post(self, path: str, payload: dict) -> int:
        body = json.dumps(payload).encode()
        signature = self.hmac.new(b"s", body, self.hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_the_action_rides_beside_the_payload_resolved(self) -> None:
        self.assertEqual(self.post("/drove", {"task": {"ref": "DROVE-12"}}), 202)
        event = self.store.pending()[0]
        self.assertEqual(event["drove"], {"action": "plan", "from_cairn": "DROVE-12"})

    def test_a_payload_that_carries_its_own_action_is_not_obeyed(self) -> None:
        """The payload is the sender's; an action only ever comes from config."""
        self.assertEqual(self.post("/nightly", {"drove": {"action": "plan", "from_cairn": "EVIL-1"}}), 202)
        event = self.store.pending()[0]
        self.assertEqual(event["drove"], {"action": "plan", "next": "DROVE"})
        self.assertEqual(event["payload"]["drove"]["from_cairn"], "EVIL-1", "kept, as data, and not read")

    def test_a_payload_without_a_valid_ref_is_refused_and_not_queued(self) -> None:
        self.assertEqual(self.post("/drove", {"task": {"ref": "--goal x"}}), 422)
        self.assertEqual(self.store.pending(), [])

    def test_a_route_with_a_bad_action_cannot_be_configured(self) -> None:
        with self.assertRaises(ValueError):
            Route(path="/x", secret="s", drove={"action": "plan", "next": "a b"})


class CliTests(FakeDroveMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_fake()

    def test_it_runs_exactly_what_it_was_given_in_the_repo_with_the_home(self) -> None:
        self.reply("plan", started())
        runs = self.runs()
        runs.cli.home = "/srv/drove-home"
        result = runs.start({"action": "plan", "from_cairn": "DROVE-5"})
        self.assertEqual(result.status, "started")
        call = self.calls()[0]
        self.assertEqual(call["argv"], ["plan", "--from-cairn", "DROVE-5", "--detached", "--json"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.repo))
        self.assertEqual(call["home"], "/srv/drove-home")

    def test_a_refusal_is_a_recorded_refusal_not_a_crash(self) -> None:
        self.reply("plan", refusal("herdr-down", "herdr is not reachable"))
        result = self.runs().start({"action": "plan", "next": "DROVE"}, wake="wak_1", source="schedule:n")
        self.assertEqual((result.status, result.code), ("refused", "herdr-down"))
        self.assertTrue(result.transient, "herdr may come up; the wake waits, counted")
        records = [r for r in self.audit.records() if r["kind"] == "drove"]
        self.assertEqual((records[0]["action"], records[0]["status"]), ("refused", "refused"))
        self.assertEqual(records[0]["data"]["code"], "herdr-down")
        self.assertEqual(self.runs().watching(), [], "nothing is watched for a run that never began")

    def test_a_refusal_about_the_request_is_not_transient(self) -> None:
        self.reply("plan", refusal("task-closed"))
        result = self.runs().start({"action": "plan", "from_cairn": "DROVE-5"})
        self.assertFalse(result.transient, "asking again says the same thing")

    def test_exit_zero_with_nothing_readable_is_not_a_start(self) -> None:
        """Exit 0 is not proof of work."""
        self.reply("plan", {"exit": 0, "stdout": "started, I think\n"})
        result = self.runs().start({"action": "plan", "next": "DROVE"})
        self.assertEqual((result.status, result.code), ("failed", "unparsed"))
        self.assertEqual(self.runs().watching(), [])

    def test_a_start_that_never_answers_is_cut_off_and_reported_unknown(self) -> None:
        self.reply("plan", {"exit": 0, "sleep": 30, "stdout": "{}"})
        runs = DroveRuns(self.dir / "r.json", self.cli(timeout=0.5), self.audit, self.alerts, repo=str(self.repo))
        began = time.monotonic()
        result = runs.start({"action": "plan", "next": "DROVE"})
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual((result.status, result.code), ("failed", "timeout"))
        self.assertFalse(result.transient, "it may have made a run; retrying could make two")

    def test_a_missing_binary_is_a_failure_with_a_name_not_an_exception(self) -> None:
        runs = DroveRuns(self.dir / "r.json", DroveCli(binary=str(self.dir / "nope")), self.audit,
                         self.alerts, repo=str(self.repo))
        result = runs.start({"action": "plan", "next": "DROVE"})
        self.assertEqual((result.status, result.code), ("failed", "not-found"))

    def test_there_is_no_default_repository(self) -> None:
        """The daemon's working directory is Bothy's own checkout."""
        result = DroveRuns(self.dir / "r.json", self.cli(), self.audit, self.alerts).start(
            {"action": "plan", "next": "DROVE"})
        self.assertEqual(result.code, "no-repo")
        self.assertEqual(self.calls(), [], "Drove was never asked")


class RunWatchTests(FakeDroveMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_fake()
        self.reply("plan", started())
        self.action = {"action": "plan", "from_cairn": "DROVE-5"}

    def begin(self, **kwargs: Any) -> DroveRuns:
        kwargs.setdefault("poll_seconds", 0.0)
        runs = self.runs(**kwargs)
        self.assertEqual(runs.start(self.action).status, "started")
        return runs

    def test_a_started_run_is_watched_until_it_is_down(self) -> None:
        runs = self.begin()
        self.reply("roster", roster("up", "working"))
        runs.tick()
        self.assertEqual(len(runs.watching()), 1)
        self.reply("roster", roster("down", "down", {"seat": "w-api", "kind": "done", "summary": "merged"}))
        runs.tick()
        self.assertEqual(runs.watching(), [])
        self.assertEqual(runs.records()[0]["status"], "down")
        self.assertIn("merged", self.alerts.said[-1], "the last report is what the human wants to know")
        self.assertIn(("finished", "ok"), self.audit_actions())

    def test_the_planner_state_is_recorded_when_it_changes_and_not_otherwise(self) -> None:
        runs = self.begin()
        self.reply("roster", [roster("up", "working"), roster("up", "working"), roster("up", "idle")])
        for _ in range(3):
            runs.tick()
        self.assertEqual([a for a, _ in self.audit_actions()].count("state"), 1)

    def test_an_unreadable_roster_is_not_a_finished_run(self) -> None:
        """"I cannot tell" is a third state, and it is never filed under down."""
        runs = self.begin()
        self.reply("roster", refusal("herdr-down"))
        for _ in range(UNKNOWN_ALERT_AFTER + 2):
            runs.tick()
        self.assertEqual(len(runs.watching()), 1, "still watched")
        self.assertEqual(len(self.alerts.incidents), 1, "said once, after the fifth miss")
        self.reply("roster", roster("down", "down"))
        runs.tick()
        self.assertEqual(runs.watching(), [])

    def test_a_deadline_stops_the_watch_and_says_so_without_stopping_the_run(self) -> None:
        runs = self.begin(deadline_seconds=0.0)
        self.reply("roster", roster("up", "working"))
        runs.tick()
        self.assertEqual(runs.records()[0]["status"], "deadline")
        self.assertIn("did not stop it", self.alerts.incidents[-1]["error"])
        self.assertNotIn("down", [c["argv"][0] for c in self.calls()], "Bothy never stops a run")

    def test_the_deadline_applies_even_when_the_roster_cannot_be_read(self) -> None:
        runs = self.begin(deadline_seconds=0.0)
        self.reply("roster", refusal("herdr-down"))
        runs.tick()
        self.assertEqual(runs.records()[0]["status"], "deadline")

    def test_a_run_drove_has_forgotten_is_closed_not_watched_forever(self) -> None:
        runs = self.begin()
        self.reply("roster", refusal("no-run", "no run named x"))
        runs.tick()
        self.assertEqual(runs.records()[0]["status"], "vanished")
        self.assertTrue(self.alerts.incidents)

    def test_the_watch_survives_a_restart(self) -> None:
        self.begin()
        reopened = self.runs(poll_seconds=0.0)
        self.assertEqual([r["run"] for r in reopened.watching()], ["drove-20261011-0200-a1b2"])
        self.reply("roster", roster("down", "down"))
        reopened.tick()
        self.assertEqual(reopened.watching(), [])

    def test_a_second_trigger_for_the_same_target_does_not_make_a_second_run(self) -> None:
        runs = self.begin()
        again = runs.start(self.action)
        self.assertEqual((again.status, again.code), ("refused", "already_running"))
        self.assertEqual(len([c for c in self.calls() if c["argv"][0] == "plan"]), 1)

    def test_the_cap_on_runs_is_a_wait_not_a_loss(self) -> None:
        runs = self.begin(max_runs=1)
        other = runs.start({"action": "plan", "next": "OTHER"})
        self.assertEqual((other.code, other.transient), ("capacity", True))

    def test_a_run_is_not_due_before_its_poll_interval(self) -> None:
        runs = self.begin(poll_seconds=3600.0)
        self.reply("roster", roster("up"))
        self.assertEqual(runs.tick(), 1)
        self.assertEqual(runs.tick(), 0, "read once, then left alone until it is due")

    def test_finished_runs_are_not_kept_forever(self) -> None:
        runs = self.begin(poll_seconds=0.0, max_runs=100)
        for index in range(60):
            self.reply("plan", started(run=f"drove-2026-{index:03d}"))
            runs.start({"action": "up", "run": f"drove-old-{index:03d}"})
        self.reply("roster", roster("down", "down"))
        runs.tick()
        self.assertLessEqual(len([r for r in runs.records() if r["status"] != "watching"]), 50)


def forwarder(case: Any, **kwargs: Any) -> AttentionForwarder:
    deliver = kwargs.pop("deliver", case.alerts.deliver)
    return AttentionForwarder(case.dir / "drove-attention.json", case.cli(), deliver, case.audit,
                              site="test-site", alerter=case.alerts, **kwargs)


class AttentionTests(FakeDroveMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_fake()

    def sent(self) -> list[str]:
        return self.alerts.said

    def test_an_item_is_told_once_however_often_it_is_seen(self) -> None:
        self.reply("attention", snapshot(item()))
        f = forwarder(self)
        for _ in range(4):
            f.poll()
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("NEEDS A PERSON", self.sent()[0])
        self.assertIn("drove-20261011-0200-a1b2:w-api:trust:1", self.sent()[0])

    def test_a_restart_tells_nothing_twice(self) -> None:
        """drove attention --follow repeats everything open at start; a snapshot always has it."""
        self.reply("attention", snapshot(item()))
        forwarder(self).poll()
        forwarder(self).poll()
        self.assertEqual(len(self.sent()), 1)

    def test_it_says_when_the_item_stops_waiting_and_says_it_once(self) -> None:
        self.reply("attention", [snapshot(item()), snapshot(), snapshot(), snapshot()])
        f = forwarder(self)
        f.poll()
        f.poll()
        self.assertEqual(len(self.sent()), 1, "one miss is not yet a resolution")
        f.poll()
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("no longer waiting", self.sent()[1])
        f.poll()
        self.assertEqual(len(self.sent()), 2)

    def test_an_item_that_blinks_out_for_one_poll_is_not_announced_resolved(self) -> None:
        """Drove omits the planner's dialog when herdr cannot be asked, for a moment."""
        self.reply("attention", [snapshot(item()), snapshot(), snapshot(item())])
        f = forwarder(self)
        for _ in range(3):
            f.poll()
        self.assertEqual(len(self.sent()), 1)

    def test_a_failed_read_never_resolves_anything(self) -> None:
        self.reply("attention", [snapshot(item()), refusal("herdr-down")])
        f = forwarder(self)
        f.poll()
        for _ in range(5):
            self.assertEqual(f.poll().status, "failed")
        self.assertEqual(len(self.sent()), 1, "absence in a failed read proves nothing")

    def test_a_shape_it_does_not_know_is_a_failure_not_an_empty_list(self) -> None:
        self.reply("attention", {"exit": 0, "stdout": json.dumps({"v": 2, "items": []})})
        self.assertEqual(forwarder(self).poll().status, "failed")
        self.reply("attention", {"exit": 0, "stdout": "[]"})
        self.assertEqual(forwarder(self).poll().status, "failed")

    def test_what_the_planner_handles_is_not_sent_to_a_person(self) -> None:
        planner = item("stalled:1", kind="stalled", answer={"type": "planner", "note": "its own"})
        self.reply("attention", snapshot(planner, item("gone:1", kind="gone",
                                                       answer={"type": "command", "command": "drove up --run x"})))
        forwarder(self).poll()
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("gone", self.sent()[0])
        self.assertIn("`drove up --run x`", self.sent()[0], "the exact command to answer with")

    def test_items_addressed_to_the_planner_are_not_sent_and_a_missing_field_means_a_person(self) -> None:
        """DROVE-108: `for` names the audience. Older Drove has no such field."""
        to_planner = {**item("idle-without-report:1", kind="idle-without-report"), "for": "planner"}
        to_human = {**item("question:1", kind="question",
                           answer={"type": "command", "command": "drove send w-api --kind answer <answer>"}),
                    "for": "human"}
        unmarked = item("dialog:1", kind="dialog")
        self.reply("attention", snapshot(to_planner, to_human, unmarked))
        forwarder(self).poll()
        text = "\n".join(self.sent())
        self.assertEqual(len(self.sent()), 2)
        self.assertNotIn("idle-without-report", text)
        self.assertIn("question", text)
        self.assertIn("dialog", text, "no `for` at all is a person's, as before")

    def test_the_audience_flag_is_not_passed_so_older_drove_still_answers(self) -> None:
        self.reply("attention", snapshot(item()))
        forwarder(self).poll()
        self.assertEqual(self.calls()[0]["argv"], ["attention", "--all", "--json"])

    def test_an_undelivered_item_is_tried_again_and_not_marked_told(self) -> None:
        self.reply("attention", snapshot(item()))
        f = forwarder(self)
        self.alerts.delivering = False
        self.assertEqual(f.poll().told, 0)
        self.assertEqual(f.poll().pending, 1)
        self.alerts.delivering = True
        self.assertEqual(f.poll().told, 1)
        self.assertEqual(len(self.sent()), 1)

    def test_a_crash_between_storing_and_sending_sends_it_again(self) -> None:
        """Prefer a duplicate to a person never being told."""
        self.reply("attention", snapshot(item()))

        def die(message: str) -> bool:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            forwarder(self, deliver=die).poll()
        self.assertEqual(self.sent(), [])
        forwarder(self).poll()
        self.assertEqual(len(self.sent()), 1)

    def test_giving_up_is_recorded_and_said_and_does_not_repeat(self) -> None:
        self.reply("attention", snapshot(item()))
        f = forwarder(self)
        self.alerts.delivering = False
        for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
            f.poll()
        self.assertIn(("attention-abandoned", "failed"), self.audit_actions())
        self.assertEqual(len(self.alerts.incidents), 1)
        self.alerts.delivering = True
        f.poll()
        self.assertEqual(self.sent(), [], "an abandoned item is not announced later")

    def test_a_backlog_is_sent_over_several_polls(self) -> None:
        many = [item(f"dialog:{index}", kind="dialog") for index in range(MAX_SENDS_PER_POLL + 5)]
        self.reply("attention", snapshot(*many))
        f = forwarder(self)
        self.assertEqual(f.poll().told, MAX_SENDS_PER_POLL)
        self.assertEqual(f.poll().told, 5)
        self.assertEqual(f.poll().told, 0)

    def test_the_follow_up_says_when_the_whole_run_went_away(self) -> None:
        self.reply("attention", [snapshot(item()), snapshot(runs=[]), snapshot(runs=[])])
        f = forwarder(self)
        for _ in range(3):
            f.poll()
        self.assertIn("no longer up", self.sent()[1])

    def test_text_a_model_wrote_cannot_ping_a_channel_or_open_a_fence(self) -> None:
        message = format_item("site", item(text="@everyone <!channel> ``` rm -rf <@U123>"))
        for danger in ("@everyone", "<!channel>", "<@U123>", "```"):
            self.assertNotIn(danger, message)

    def test_by_hand_it_shows_what_it_would_send_and_sends_nothing(self) -> None:
        self.reply("attention", snapshot(item()))
        f = forwarder(self)
        self.assertEqual([state for _, state in f.view()[0]], ["unseen"])
        self.assertEqual(self.sent(), [])
        f.poll()
        self.assertEqual([state for _, state in f.view()[0]], ["open"])

    def test_every_forward_is_in_the_audit_with_its_id(self) -> None:
        self.reply("attention", snapshot(item()))
        forwarder(self).poll()
        record = [r for r in self.audit.records() if r["action"] == "attention-forwarded"][0]
        self.assertEqual(record["subject"], item()["id"])
        self.assertEqual(self.audit.verify(), len(list(self.audit.records())))


class DeliveryTests(unittest.TestCase):
    """The alerter has to say whether a message arrived, or "told" is a guess."""

    class Sink:
        def __init__(self, result: bool | None) -> None:
            self.result = result

        def alert(self, message: str) -> bool | None:
            return self.result

    def test_a_failed_webhook_is_not_a_delivery(self) -> None:
        self.assertFalse(Alerter(self.Sink(False)).deliver("x"))
        self.assertTrue(Alerter(self.Sink(True)).deliver("x"))

    def test_one_working_channel_is_enough_and_a_dead_one_alone_is_not(self) -> None:
        self.assertTrue(Alerter(CompositeSink(self.Sink(None), self.Sink(False), self.Sink(True))).deliver("x"))
        self.assertFalse(Alerter(CompositeSink(self.Sink(None), self.Sink(False))).deliver("x"))

    def test_the_webhook_sinks_report_what_happened(self) -> None:
        sink = DiscordWebhookSink("http://127.0.0.1:1/unreachable", timeout=0.5)
        self.assertIs(sink.alert("hello"), False)
        self.assertIsNone(StderrSink().alert("hello"))


class CommandTests(FakeDroveMixin, unittest.TestCase):
    """The operator's commands, driven through main() like a person at a shell."""

    def setUp(self) -> None:
        self.make_fake()
        self.home = Path(tempfile.mkdtemp())
        self.config_path = self.dir / "config.json"
        self.config_path.write_text(json.dumps({
            "home": str(self.home), "drove_binary": str(self.binary), "drove_repo": str(self.repo)}))

    def bothy(self, *argv: str) -> tuple[int, str]:
        import contextlib
        import io

        from bothy.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--config", str(self.config_path), *argv])
        return code, out.getvalue()

    def test_a_schedule_entry_can_start_drove_and_says_so_when_listed(self) -> None:
        code, _ = self.bothy("schedule", "add", "nightly", "--kind", "cron", "--spec", "0 2 * * *",
                             "--drove-next", "DROVE")
        self.assertEqual(code, 0)
        code, listing = self.bothy("schedule", "list")
        self.assertIn("nightly", listing)
        self.assertIn("drove next=DROVE", listing)
        job = Schedule(self.home / "state" / "jobs.json").get("nightly")
        self.assertEqual((job.drove, job.prompt), ({"action": "plan", "next": "DROVE"}, ""))

    def test_a_bad_value_is_refused_when_the_job_is_written(self) -> None:
        code, output = self.bothy("schedule", "add", "bad", "--drove-from-cairn", "DROVE-5;rm -rf")
        self.assertEqual(code, 78)
        self.assertIn("from_cairn", output)
        self.assertEqual(Schedule(self.home / "state" / "jobs.json").jobs(), [], "nothing was saved")

    def test_start_by_hand_records_the_run_and_status_lists_it(self) -> None:
        self.reply("plan", started())
        code, output = self.bothy("drove", "start", "--from-cairn", "DROVE-5")
        self.assertEqual(code, 0)
        self.assertIn("drove-20261011-0200-a1b2", output)
        code, listing = self.bothy("drove", "status")
        self.assertIn("watching", listing)
        self.assertIn("plan:from-cairn:DROVE-5", listing)

    def test_start_by_hand_reports_a_refusal_with_its_code_and_a_non_zero_exit(self) -> None:
        self.reply("plan", refusal("herdr-down", "herdr is not reachable"))
        code, output = self.bothy("drove", "start", "--next", "DROVE")
        self.assertEqual(code, 1)
        self.assertIn("herdr-down", output)

    def test_attention_by_hand_shows_what_is_waiting_and_sends_nothing(self) -> None:
        self.reply("attention", snapshot(item()))
        code, output = self.bothy("drove", "attention")
        self.assertEqual(code, 0)
        self.assertIn("unseen", output)
        self.assertIn("trust", output)
        self.assertFalse((self.home / "state" / "drove-attention.json").exists())


class FakeRunner:
    alerter = None

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *, subject: str, **kwargs: Any) -> Any:
        self.calls.append(subject)
        raise AssertionError("a Drove wake must never reach the Codex runner")


class DaemonTests(FakeDroveMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_fake()
        self.config = Config(home=Path(tempfile.mkdtemp()), drove_binary=str(self.binary),
                             drove_repo=str(self.repo), drove_poll_seconds=0.0)
        self.config.ensure_dirs()
        self.audit = AuditLog(self.config.audit_path)
        self.store = WakeStore(self.config.wake_dir)
        self.runner = FakeRunner()

    def daemon(self, **kwargs: Any) -> Daemon:
        admission = Admission(PoolPolicy(), BudgetLedger(self.config.budget_path, BudgetPolicy(mode="api")))
        return Daemon(self.config, runner=self.runner, admission=admission, audit=self.audit,
                      registry=ProcessRegistry(self.config.registry_path), alerter=self.alerts,
                      routes=[Route(path="/x", secret="s")], **kwargs)

    def enqueue(self, wake_id: str = "w1", action: Any = None, **extra: Any) -> None:
        self.store.append({"id": wake_id, "received_at": clock.iso(), "route": "/nightly",
                           "subject": "drove:nightly", "payload": {}, "processed": False,
                           "drove": action or {"action": "plan", "next": "DROVE"}, **extra})

    def test_a_drove_wake_starts_a_run_and_never_touches_codex(self) -> None:
        self.reply("plan", started())
        self.enqueue()
        daemon = self.daemon()
        daemon.drain()
        self.assertEqual(self.store.pending()[0]["id"], "w1", "the drain leaves it alone")
        self.assertEqual(daemon.drove_wakes(), 1)
        self.assertEqual(self.store.pending(), [])
        done = self.store.all_events()[0]
        self.assertEqual((done["outcome"], done["drove_run"]), ("started", "drove-20261011-0200-a1b2"))
        self.assertEqual(self.runner.calls, [])

    def test_a_payload_key_called_drove_is_not_an_action(self) -> None:
        self.store.append({"id": "w2", "received_at": clock.iso(), "route": "/x", "subject": "s",
                           "payload": {"drove": {"action": "plan", "next": "EVIL"}, "prompt": "hi"},
                           "processed": False})
        daemon = self.daemon()
        self.assertEqual(daemon.drove_wakes(), 0)
        self.assertEqual(self.calls(), [])

    def test_a_scheduled_drove_job_is_a_wake_with_no_prompt(self) -> None:
        daemon = self.daemon()
        job = Job(id="nightly", kind="cron", spec="0 2 * * *", drove={"action": "plan", "next": "DROVE"})
        wake = daemon.fire(job)
        event = [e for e in self.store.pending() if e["id"] == wake][0]
        self.assertEqual(event["drove"], {"action": "plan", "next": "DROVE"})
        self.assertNotIn("prompt", event["payload"])

    def test_herdr_not_running_waits_and_then_starts(self) -> None:
        self.reply("plan", [refusal("herdr-down"), started()])
        self.enqueue()
        daemon = self.daemon()
        self.assertEqual(daemon.drove_wakes(), 0)
        pending = self.store.pending()[0]
        self.assertEqual((pending["attempts"], pending["last_gate"]), (1, "herdr-down"))
        self.assertEqual(daemon.drove_wakes(), 1)
        self.assertEqual(self.store.pending(), [])

    def test_waiting_for_herdr_is_bounded_and_ends_out_loud(self) -> None:
        self.reply("plan", refusal("herdr-down"))
        self.enqueue()
        daemon = self.daemon(max_wake_attempts=3)
        for _ in range(5):
            daemon.drove_wakes()
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.all_events()[0]["outcome"], "abandoned")
        self.assertTrue(self.alerts.incidents)

    def test_a_refusal_about_the_request_is_settled_alerted_and_not_retried(self) -> None:
        self.reply("plan", refusal("task-closed", "DROVE-5 is done"))
        self.enqueue(action={"action": "plan", "from_cairn": "DROVE-5"})
        daemon = self.daemon()
        daemon.drove_wakes()
        daemon.drove_wakes()
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.store.all_events()[0]["outcome"], "refused")
        self.assertIn("task-closed", self.alerts.incidents[0]["error"])

    def test_a_trigger_for_a_run_already_going_is_quiet(self) -> None:
        self.reply("plan", started())
        self.enqueue("w1")
        self.enqueue("w2")
        daemon = self.daemon()
        daemon.drove_wakes()
        self.assertEqual(self.alerts.incidents, [], "the answer to a second trigger is: already going")
        outcomes = {e["id"]: e["outcome"] for e in self.store.all_events()}
        self.assertEqual(outcomes, {"w1": "started", "w2": "refused"})

    def test_one_pass_starts_watches_and_listens(self) -> None:
        self.config.drove_attention_interval_seconds = 30.0
        self.config.discord_webhook_url = "http://127.0.0.1:1/x"
        self.reply("plan", started())
        self.reply("roster", roster("up", "blocked"))
        self.reply("attention", snapshot(item()))
        self.enqueue()
        daemon = self.daemon()
        daemon.drove_once()
        self.assertEqual(len(self.alerts.said), 1)
        self.assertIn("NEEDS A PERSON", self.alerts.said[0])
        self.assertEqual([c["argv"][0] for c in self.calls()], ["plan", "roster", "attention"])

    def test_attention_with_nowhere_to_send_it_refuses_to_start(self) -> None:
        self.config.drove_attention_interval_seconds = 30.0
        daemon = self.daemon()
        with self.assertRaises(RuntimeError) as caught:
            daemon.start()
        self.assertIn("half-connected", str(caught.exception))

    def test_a_failing_attention_read_backs_off_and_says_so_once(self) -> None:
        self.config.drove_attention_interval_seconds = 30.0
        self.reply("attention", refusal("herdr-down"))
        daemon = self.daemon()
        daemon._attend()
        self.assertEqual(len(self.alerts.incidents), 1)
        self.assertEqual(self.alerts.incidents[0]["job"], "drove:attention")
        self.assertGreater(daemon._next_attention, clock.monotonic() + 30 * 3, "backed off, not hammering")
        self.assertIn(("attention-failed", "failed"), self.audit_actions())


if __name__ == "__main__":
    unittest.main()
