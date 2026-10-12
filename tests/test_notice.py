"""What a person reads: embeds, blocks, and a resolution that edits the original."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from bothy.alert import Alerter, CompositeSink, DiscordWebhookSink, SlackWebhookSink, StderrSink
from bothy.drove import notice_for
from bothy.notice import discord_payload, slack_payload, text_of

RUN = "dispofi-api-20261011-1926-c973"
SINCE = "2026-10-12T07:43:47.414Z"
HOOK = "https://discord.example/api/webhooks/1/secret-token"


def attention(kind: str, *, seat: str = "planner", text: str = "", answer: dict | None = None) -> dict[str, Any]:
    return {"id": f"{RUN}:{seat}:{kind}:1", "run": RUN, "seat": seat, "kind": kind, "text": text,
            "since": SINCE, "answer": answer or {"type": "pane", "pane": "w2M:p1", "inspect": "herdr pane read w2M:p1"}}


GONE = attention("gone", text=f"The planner pane w2M:p1 is gone while run {RUN} is up.",
                 answer={"type": "command", "command": f"drove up --run {RUN} --detached"})
QUESTION = attention("question", seat="w-api", text="Which database should the migration target?",
                     answer={"type": "command", "command": "drove send w-api --kind answer <answer>"})
KINDS = ["dialog", "permission", "trust", "hook-trust", "channel-confirm", "codex-version-mismatch",
         "usage-limit", "gone", "question", "asking", "blocked", "promote", "pane-budget"]


class EmbedTests(unittest.TestCase):
    def embed(self, item: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return discord_payload(notice_for("mac", item, **kw))["embeds"][0]

    def test_a_planner_that_stopped_reads_as_a_sentence_with_the_command_to_run(self) -> None:
        embed = self.embed(GONE)
        self.assertEqual(embed["title"], "dispofi-api · the planner stopped")
        self.assertEqual(embed["description"], "The planner stopped, so nothing is steering this run.")
        self.assertEqual(embed["fields"][0], {"name": "What to do", "inline": False,
                                              "value": f"```\ndrove up --run {RUN} --detached\n```"})
        self.assertEqual(embed["fields"][1]["name"], "Run")
        self.assertEqual(embed["footer"], {"text": "Bothy on mac"})
        self.assertEqual(embed["timestamp"], SINCE)
        self.assertEqual(embed["color"], 0xE74C3C)

    def test_a_question_carries_the_question_and_the_way_to_answer_it(self) -> None:
        embed = self.embed(QUESTION)
        self.assertEqual(embed["title"], "dispofi-api · a question for you")
        self.assertIn("> Which database should the migration target?", embed["description"])
        self.assertIn(f"drove send w-api --run {RUN} --kind answer <answer>", embed["fields"][0]["value"])
        self.assertEqual(embed["color"], 0x3498DB)

    def test_the_internal_item_id_is_never_visible_and_no_marker_shouts(self) -> None:
        for item in (GONE, QUESTION):
            rendered = json.dumps(discord_payload(notice_for("mac", item))) + json.dumps(slack_payload(notice_for("mac", item)))
            self.assertNotIn(item["id"], rendered)
            self.assertNotIn("NEEDS A PERSON", rendered)
            self.assertNotIn("!!", rendered)

    def test_every_kind_has_its_own_plain_words(self) -> None:
        seen = set()
        for kind in KINDS:
            embed = self.embed(attention(kind, seat="w-api", text="x"))
            self.assertTrue(embed["title"].startswith("dispofi-api · "), kind)
            self.assertNotIn("{", embed["title"] + embed["description"], kind)
            self.assertNotIn("something needs you", embed["title"], kind)
            seen.add(embed["title"])
        self.assertGreaterEqual(len(seen), 11, "kinds are told apart")

    def test_a_kind_drove_adds_later_still_says_something(self) -> None:
        embed = self.embed(attention("brand-new", seat="w-api", text="Do the thing"))
        self.assertIn("Do the thing", embed["description"])

    def test_a_pane_answer_says_where_and_how_to_look(self) -> None:
        embed = self.embed(attention("dialog"))
        value = embed["fields"][0]["value"]
        self.assertIn("planner's pane", value)
        self.assertIn("herdr pane read w2M:p1", value)

    def test_worker_text_cannot_ping_or_close_the_code_block(self) -> None:
        item = attention("question", seat="w-api", text="@everyone <!channel> ``` boom",
                         answer={"type": "command", "command": "run ``` @everyone"})
        payload = discord_payload(notice_for("mac", item))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        value = payload["embeds"][0]["fields"][0]["value"]
        self.assertEqual(value.count("```"), 2)

    def test_a_run_with_no_stamp_falls_back_to_the_site(self) -> None:
        embed = self.embed({**GONE, "run": "odd"})
        self.assertTrue(embed["title"].startswith("mac · "))

    def test_resolved_is_muted_and_has_no_call_to_act(self) -> None:
        embed = self.embed(GONE, resolved=True, run_up=True)
        self.assertEqual(embed["title"], "Resolved · dispofi-api · the planner stopped")
        self.assertEqual(embed["color"], 0x95A5A6)
        self.assertEqual([f["name"] for f in embed["fields"]], ["Run"])
        self.assertIn("answered or cleared", embed["description"])
        self.assertIn("no longer up", self.embed(GONE, resolved=True, run_up=False)["description"])


class RunInCommandTests(unittest.TestCase):
    """A human runs it from their own terminal, where no DROVE_RUN is set."""

    def command(self, command: str) -> str:
        item = attention("question", seat="w-api", answer={"type": "command", "command": command})
        return notice_for("mac", item).command or ""

    def test_a_command_with_no_run_gets_one_before_its_first_option(self) -> None:
        self.assertEqual(self.command("drove send w-api --kind answer <answer>"),
                         f"drove send w-api --run {RUN} --kind answer <answer>")
        self.assertEqual(self.command("drove budget 3"), f"drove budget 3 --run {RUN}")

    def test_a_command_that_names_its_run_or_all_is_left_alone(self) -> None:
        self.assertEqual(self.command("drove up --run x --detached"), "drove up --run x --detached")
        self.assertEqual(self.command("drove attention --all"), "drove attention --all")

    def test_only_drove_commands_are_touched_and_the_look_first_command_too(self) -> None:
        self.assertEqual(self.command("bothy audit"), "bothy audit")
        item = attention("dialog", answer={"type": "pane", "inspect": "drove capture planner"})
        self.assertEqual(notice_for("mac", item).command, f"drove capture planner --run {RUN}")


class IncidentTests(unittest.TestCase):
    """What Bothy says about itself is drawn the same way."""

    def sent(self, **kw: Any) -> dict[str, Any]:
        wire = FakeWire()
        alerter = Alerter(DiscordWebhookSink(HOOK), host="mac")
        with mock.patch("bothy.alert._send_json", wire):
            alerter.incident(**kw)
        return wire.calls[0][2]["embeds"][0]

    def test_an_incident_is_a_plain_title_a_sentence_and_what_to_do(self) -> None:
        embed = self.sent(job="poll:cairn", error="HTTP 500 from cairn\ntraceback...", severity="warning",
                          title="the cairn poll is failing", summary="Bothy could not read that source.",
                          reaction="To reproduce it by hand:", command="bothy poll cairn")
        self.assertEqual(embed["title"], "Bothy · the cairn poll is failing")
        self.assertEqual(embed["description"], "Bothy could not read that source.")
        names = [field["name"] for field in embed["fields"]]
        self.assertEqual(names, ["What to do", "Details"])
        self.assertIn("```\nbothy poll cairn\n```", embed["fields"][0]["value"])
        self.assertIn("HTTP 500 from cairn", embed["fields"][1]["value"])
        self.assertEqual(embed["color"], 0xF1A431)
        self.assertEqual(embed["footer"], {"text": "Bothy on mac"})
        self.assertNotIn("WARNING", json.dumps(embed))

    def test_a_project_leads_the_title_and_the_run_and_facts_are_fields(self) -> None:
        embed = self.sent(job="x", error="boom", severity="critical", project="dispofi-api", title="the run vanished",
                          run_id=RUN, detail={"subject": "task 5"})
        self.assertEqual(embed["title"], "dispofi-api · the run vanished")
        self.assertEqual(embed["color"], 0xE74C3C)
        self.assertIn({"name": "subject", "value": "task 5", "inline": True}, embed["fields"])
        self.assertEqual(embed["fields"][-1]["name"], "Run")

    def test_without_words_of_its_own_the_first_line_of_the_error_stands_in(self) -> None:
        embed = self.sent(job="janitor", error="disk full\nmore")
        self.assertEqual(embed["title"], "Bothy · janitor")
        self.assertEqual(embed["description"], "disk full")

    def test_a_repeat_is_still_one_message(self) -> None:
        wire = FakeWire()
        alerter = Alerter(DiscordWebhookSink(HOOK), host="mac")
        with mock.patch("bothy.alert._send_json", wire):
            self.assertTrue(alerter.incident(job="j", error="same 1"))
            self.assertFalse(alerter.incident(job="j", error="same 2"))
        self.assertEqual(len(wire.calls), 1)

    def test_tell_sends_a_notice_unconditionally(self) -> None:
        wire = FakeWire()
        alerter = Alerter(DiscordWebhookSink(HOOK), host="mac")
        with mock.patch("bothy.alert._send_json", wire):
            alerter.tell(notice_for("mac", GONE))
            alerter.tell(notice_for("mac", GONE))
        self.assertEqual(len(wire.calls), 2)


class SlackTests(unittest.TestCase):
    def test_blocks_have_header_section_what_to_do_and_context(self) -> None:
        payload = slack_payload(notice_for("mac", GONE))
        attachment = payload["attachments"][0]
        kinds = [block["type"] for block in attachment["blocks"]]
        self.assertEqual(kinds, ["header", "section", "section", "context"])
        self.assertEqual(attachment["blocks"][0]["text"]["text"], "dispofi-api · the planner stopped")
        self.assertIn(f"*What to do*\n```\ndrove up --run {RUN} --detached\n```", attachment["blocks"][2]["text"]["text"])
        context = attachment["blocks"][3]["elements"][0]["text"]
        self.assertIn(f"`{RUN}`", context)
        self.assertIn("Bothy on mac", context)
        self.assertIn("<!date^", context)
        self.assertEqual(attachment["color"], "#e74c3c")
        self.assertEqual(payload["text"], "dispofi-api · the planner stopped")

    def test_a_question_in_blocks(self) -> None:
        attachment = slack_payload(notice_for("mac", QUESTION))["attachments"][0]
        self.assertIn("Which database", attachment["blocks"][1]["text"]["text"])
        self.assertEqual(attachment["color"], "#3498db")

    def test_slack_markup_in_worker_text_is_escaped(self) -> None:
        item = attention("question", seat="w-api", text="<!channel> & <@U1>")
        text = slack_payload(notice_for("mac", item))["attachments"][0]["blocks"][1]["text"]["text"]
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<@U1>", text)

    def test_resolved_has_no_what_to_do_and_is_grey(self) -> None:
        attachment = slack_payload(notice_for("mac", GONE, resolved=True))["attachments"][0]
        self.assertEqual([b["type"] for b in attachment["blocks"]], ["header", "section", "context"])
        self.assertEqual(attachment["color"], "#95a5a6")


class PlainTextTests(unittest.TestCase):
    def test_the_fallback_reads_plainly_with_no_item_id(self) -> None:
        text = text_of(notice_for("mac", GONE))
        self.assertEqual(text.splitlines()[0], "dispofi-api · the planner stopped")
        self.assertIn(f"What to do: drove up --run {RUN} --detached", text)
        self.assertNotIn(GONE["id"], text)


class FakeWire:
    """Stands in for the network: records every request and answers like Discord."""

    def __init__(self, *, patch_status: str = "200", post_ok: bool = True) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.patch_status = patch_status
        self.post_ok = post_ok

    def __call__(self, method: str, url: str, payload: dict[str, Any], *, timeout: float = 10.0):
        self.calls.append((method, url, payload))
        if method == "PATCH":
            return self.patch_status == "200", self.patch_status if self.patch_status == "200" else f"HTTP {self.patch_status}", None
        if not self.post_ok:
            return False, "HTTP 500", None
        return True, "200", {"id": "9001"}


class DiscordSinkTests(unittest.TestCase):
    def sink(self, url: str = HOOK) -> DiscordWebhookSink:
        return DiscordWebhookSink(url)

    def test_a_post_asks_for_the_message_back_and_keeps_its_id(self) -> None:
        wire = FakeWire()
        with mock.patch("bothy.alert._send_json", wire):
            ok, refs = self.sink().notify(notice_for("mac", GONE), {})
        self.assertTrue(ok)
        self.assertEqual(refs, {"discord": "9001"})
        method, url, payload = wire.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{HOOK}?wait=true")
        self.assertIn("embeds", payload)

    def test_an_existing_query_on_the_webhook_is_kept(self) -> None:
        wire = FakeWire()
        with mock.patch("bothy.alert._send_json", wire):
            self.sink(f"{HOOK}?thread_id=77").notify(notice_for("mac", GONE), {})
        self.assertIn("thread_id=77", wire.calls[0][1])
        self.assertIn("wait=true", wire.calls[0][1])

    def test_a_resolution_patches_the_original_and_posts_nothing_new(self) -> None:
        wire = FakeWire()
        with mock.patch("bothy.alert._send_json", wire):
            ok, refs = self.sink(f"{HOOK}?thread_id=77").notify(
                notice_for("mac", GONE, resolved=True), {"discord": "9001"})
        self.assertTrue(ok)
        self.assertEqual(refs, {"discord": "9001"})
        self.assertEqual(len(wire.calls), 1)
        method, url, payload = wire.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(url, f"https://discord.example/api/webhooks/1/secret-token/messages/9001?thread_id=77")
        self.assertEqual(payload["embeds"][0]["color"], 0x95A5A6)

    def test_a_deleted_original_gets_a_fresh_resolution_instead(self) -> None:
        wire = FakeWire(patch_status="404")
        with mock.patch("bothy.alert._send_json", wire):
            ok, _ = self.sink().notify(notice_for("mac", GONE, resolved=True), {"discord": "9001"})
        self.assertTrue(ok)
        self.assertEqual([call[0] for call in wire.calls], ["PATCH", "POST"])

    def test_another_edit_failure_is_a_failed_delivery_to_retry(self) -> None:
        wire = FakeWire(patch_status="500")
        with mock.patch("bothy.alert._send_json", wire), mock.patch("sys.stderr"):
            ok, _ = self.sink().notify(notice_for("mac", GONE, resolved=True), {"discord": "9001"})
        self.assertFalse(ok)
        self.assertEqual([call[0] for call in wire.calls], ["PATCH"])

    def test_a_resolution_with_no_remembered_message_posts(self) -> None:
        wire = FakeWire()
        with mock.patch("bothy.alert._send_json", wire):
            self.sink().notify(notice_for("mac", GONE, resolved=True), {})
        self.assertEqual([call[0] for call in wire.calls], ["POST"])


class SlackSinkTests(unittest.TestCase):
    def test_slack_posts_blocks_and_resolves_with_a_follow_up(self) -> None:
        wire = FakeWire()
        sink = SlackWebhookSink("https://hooks.slack.example/services/T/B/x")
        with mock.patch("bothy.alert._send_json", wire):
            ok, refs = sink.notify(notice_for("mac", GONE), {})
            sink.notify(notice_for("mac", GONE, resolved=True), refs)
        self.assertTrue(ok)
        self.assertEqual([call[0] for call in wire.calls], ["POST", "POST"])
        self.assertNotIn("wait=true", wire.calls[0][1])
        self.assertIn("attachments", wire.calls[0][2])
        self.assertTrue(wire.calls[1][2]["attachments"][0]["blocks"][0]["text"]["text"].startswith("Resolved"))


class CompositeTests(unittest.TestCase):
    def test_refs_are_kept_per_sink_and_one_failure_does_not_silence_the_rest(self) -> None:
        wire = FakeWire()

        class Broken:
            name = "broken"

            def notify(self, notice: Any, refs: dict[str, str]):
                raise RuntimeError("down")

        sink = CompositeSink(StderrSink(), Broken(), DiscordWebhookSink(HOOK))
        with mock.patch("bothy.alert._send_json", wire), mock.patch("sys.stderr"):
            ok, refs = sink.notify(notice_for("mac", GONE), {})
        self.assertTrue(ok)
        self.assertEqual(refs, {"discord": "9001"})

    def test_a_sink_with_only_alert_gets_the_plain_text(self) -> None:
        heard: list[str] = []

        class Old:
            def alert(self, message: str) -> bool:
                heard.append(message)
                return True

        ok, refs = CompositeSink(Old()).notify(notice_for("mac", GONE), {})
        self.assertTrue(ok)
        self.assertEqual(refs, {})
        self.assertIn("dispofi-api · the planner stopped", heard[0])


if __name__ == "__main__":
    unittest.main()
