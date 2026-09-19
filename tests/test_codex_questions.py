"""An agent asking a person must reach one.

Codex asks through `.../requestUserInput`. Bothy routed that into the approval
handler, which declines by default — correct for "may I run this command",
catastrophic for "I need information only you have". The question was refused in
milliseconds, nobody was told, and the one moment the agent reached for a human
was the one moment it was guaranteed not to get one.

CodexClient's request dispatch had no tests at all, which is how that survived.
"""

from __future__ import annotations

import unittest
from typing import Any

from bothy import codex


def client(**kwargs: Any) -> codex.CodexClient:
    """A client wired for dispatch only — nothing here starts a subprocess."""
    c = codex.CodexClient(run_id="run_test", codex_home="/tmp/bothy-test-home", **kwargs)
    c.sent: list[dict[str, Any]] = []            # type: ignore[attr-defined]
    c._send = lambda payload: c.sent.append(payload)   # type: ignore[assignment]
    return c


def ask(c: codex.CodexClient, method: str, params: dict[str, Any] | None = None) -> None:
    c._handle_server_request({"id": 7, "method": method, "params": params or {}})


class QuestionsReachAHuman(unittest.TestCase):
    def test_a_question_is_recorded_rather_than_silently_declined(self) -> None:
        c = client()
        ask(c, "thread/requestUserInput", {"prompt": "Which bucket should I write to?"})

        self.assertEqual(len(c.questions), 1)
        self.assertEqual(c.questions[0].prompt, "Which bucket should I write to?")
        self.assertEqual(c.questions[0].method, "thread/requestUserInput")
        self.assertTrue(c.questions[0].asked_at, "a question with no timestamp cannot be chased")

    def test_the_handler_is_called_while_the_run_is_still_going(self) -> None:
        # Alerting at the end would be the same silence with extra steps: by
        # then the turn has already carried on without the answer.
        seen: list[codex.Question] = []
        c = client(on_question=seen.append)
        ask(c, "thread/requestUserInput", {"prompt": "Need the staging key"})
        self.assertEqual([q.prompt for q in seen], ["Need the staging key"])

    def test_the_turn_is_told_why_no_answer_is_coming(self) -> None:
        c = client()
        ask(c, "thread/requestUserInput", {"prompt": "?"})
        result = c.sent[0]["result"]
        self.assertEqual(result["decision"], "decline")
        # The old reason was "no approval policy configured for …", which is
        # both wrong and unactionable for a question.
        self.assertIn("recorded", result["reason"])
        self.assertIn("operator", result["reason"])
        self.assertNotIn("approval policy", result["reason"])

    def test_a_question_does_not_reach_the_approval_handler(self) -> None:
        approvals: list[str] = []

        def approval(method: str, params: dict[str, Any]) -> codex.ApprovalDecision:
            approvals.append(method)
            return codex.ApprovalDecision("decline")

        c = client(approval=approval)
        ask(c, "thread/requestUserInput", {"prompt": "?"})
        self.assertEqual(approvals, [], "a question is not a permission request")

    def test_approvals_still_go_to_the_approval_handler(self) -> None:
        # The other half must not regress: declining by default is right for a
        # harness running unattended on someone else's machine.
        approvals: list[str] = []

        def approval(method: str, params: dict[str, Any]) -> codex.ApprovalDecision:
            approvals.append(method)
            return codex.ApprovalDecision("decline", reason="nope")

        c = client(approval=approval)
        ask(c, "item/requestApproval", {"command": "rm -rf /"})
        self.assertEqual(approvals, ["item/requestApproval"])
        self.assertEqual(c.sent[0]["result"]["decision"], "decline")
        self.assertEqual(c.questions, [], "an approval is not a question")

    def test_a_failing_notifier_does_not_take_the_run_down(self) -> None:
        def explode(question: codex.Question) -> None:
            raise RuntimeError("discord is down")

        c = client(on_question=explode)
        ask(c, "thread/requestUserInput", {"prompt": "still important"})
        # The durable record survives the notifier, and the turn still gets an
        # answer rather than hanging.
        self.assertEqual(len(c.questions), 1)
        self.assertEqual(c.sent[0]["result"]["decision"], "decline")

    def test_an_unknown_request_is_still_refused_explicitly(self) -> None:
        c = client()
        ask(c, "something/nobody-implements", {})
        self.assertIn("error", c.sent[0], "an unanswered request hangs the turn")


class QuestionTextExtraction(unittest.TestCase):
    def test_the_known_payload_shapes(self) -> None:
        for params, expected in [
            ({"prompt": "a"}, "a"),
            ({"question": "b"}, "b"),
            ({"message": "c"}, "c"),
            ({"text": "d"}, "d"),
            ({"title": "e"}, "e"),
            ({"input": {"message": "  nested  "}}, "nested"),
            ({"request": {"prompt": "deeper"}}, "deeper"),
        ]:
            self.assertEqual(codex._question_text(params), expected)

    def test_an_unknown_shape_keeps_the_payload_rather_than_losing_it(self) -> None:
        # A question nobody can read is no better than one nobody was told
        # about, so the fallback is the whole payload, not "".
        text = codex._question_text({"surprise": {"deep": [1, 2]}})
        self.assertIn("surprise", text)

    def test_a_whitespace_only_field_falls_through_to_the_next(self) -> None:
        # An empty `prompt` beside a real `message` must not win just because
        # it is checked first.
        self.assertEqual(codex._question_text({"prompt": "  ", "message": "real"}), "real")


if __name__ == "__main__":
    unittest.main()
