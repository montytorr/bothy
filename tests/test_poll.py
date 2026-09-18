"""Polling: cheap when unchanged, and never replaying work already done."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bothy.poll import PollState, Poller, Source, dig


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeApi:
    """A tiny API that honours If-None-Match, written independently of bothy.poll."""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.etag = "v1"
        self.calls = 0

    def __call__(self, request, timeout=None):  # noqa: ANN001
        self.calls += 1
        if request.get_header("If-none-match") == self.etag:
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified",
                                         {"ETag": self.etag}, None)
        body = json.dumps({"events": self.items}).encode()
        return FakeResponse(body, 200, {"ETag": self.etag})


class DigTests(unittest.TestCase):
    def test_it_follows_a_dotted_path(self) -> None:
        self.assertEqual(dig({"a": {"b": {"c": 1}}}, "a.b.c"), 1)

    def test_absence_yields_none_rather_than_raising(self) -> None:
        """An API that changes shape should show as "no events", not stop the loop."""
        self.assertIsNone(dig({"a": 1}, "a.b.c"))
        self.assertIsNone(dig(None, "a"))


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.state = PollState(self.dir / "poll.json")
        self.api = FakeApi([{"id": "a"}, {"id": "b"}])
        self.poller = Poller(self.state, opener=self.api)
        self.source = Source(name="issues", url="https://api.example/issues",
                             items_path="events", id_path="id",
                             subject_from="id", subject_prefix="issue-")

    def test_the_first_poll_finds_everything(self) -> None:
        result, wakes = self.poller.poll(self.source)
        self.assertEqual((result.status, result.fetched, result.new), ("ok", 2, 0 + 2))
        self.assertEqual([w["subject"] for w in wakes], ["issue-a", "issue-b"])

    def test_an_unchanged_resource_costs_a_304_and_nothing_else(self) -> None:
        """The whole economy of polling."""
        self.poller.poll(self.source)
        result, wakes = self.poller.poll(self.source)
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(result.http_status, 304)
        self.assertEqual(wakes, [])

    def test_only_genuinely_new_items_wake_anything(self) -> None:
        self.poller.poll(self.source)
        self.api.etag = "v2"
        self.api.items.append({"id": "c"})
        result, wakes = self.poller.poll(self.source)
        self.assertEqual((result.new, result.duplicates), (1, 2))
        self.assertEqual([w["subject"] for w in wakes], ["issue-c"])

    def test_a_restart_replays_nothing(self) -> None:
        """Replaying work in an agent harness means spending money twice."""
        self.poller.poll(self.source)
        self.api.etag = "v2"
        fresh = Poller(PollState(self.dir / "poll.json"), opener=self.api)
        result, wakes = fresh.poll(self.source)
        self.assertEqual(wakes, [])
        self.assertEqual(result.duplicates, 2)

    def test_an_item_without_a_stable_id_is_skipped_not_replayed(self) -> None:
        self.api.items = [{"no_id": 1}]
        result, wakes = self.poller.poll(self.source)
        self.assertEqual((result.new, result.duplicates), (0, 1))

    def test_a_failure_backs_off_and_never_raises(self) -> None:
        def broken(request, timeout=None):  # noqa: ANN001
            raise OSError("connection refused")
        result, wakes = Poller(self.state, opener=broken).poll(self.source)
        self.assertEqual(result.status, "failed")
        self.assertIn("connection refused", result.detail)
        self.assertEqual(wakes, [])

    def test_a_missing_credential_is_a_configuration_problem(self) -> None:
        """Waiting does not make an unset environment variable appear."""
        source = Source(name="needs-token", url="https://x",
                        header_env={"Authorization": "DEFINITELY_UNSET_TOKEN"})
        result, _ = self.poller.poll(source)
        self.assertEqual(result.status, "skipped")
        self.assertIn("DEFINITELY_UNSET_TOKEN", result.detail)

    def test_a_non_json_response_fails_rather_than_crashing(self) -> None:
        def html(request, timeout=None):  # noqa: ANN001
            return FakeResponse(b"<html>nope</html>", 200, {})
        result, _ = Poller(self.state, opener=html).poll(self.source)
        self.assertEqual(result.status, "failed")
        self.assertIn("not JSON", result.detail)

    def test_the_seen_set_is_bounded(self) -> None:
        """An unbounded one is a leak that only shows on the oldest deployment."""
        state = PollState(self.dir / "capped.json", seen_cap=50)
        state.remember("s", new_ids=[str(index) for index in range(500)])
        self.assertEqual(len(state.get("s")["seen"]), 50)

    def test_a_batch_is_capped_oldest_first(self) -> None:
        """A truncated batch leaves the newest for next time, never strands the oldest."""
        self.api.items = [{"id": str(index)} for index in range(10)]
        self.source.max_items_per_poll = 3
        _, wakes = self.poller.poll(self.source)
        self.assertEqual([w["subject"] for w in wakes], ["issue-0", "issue-1", "issue-2"])


if __name__ == "__main__":
    unittest.main()
