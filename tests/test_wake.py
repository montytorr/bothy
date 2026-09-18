"""Signatures, replays, and the two doors."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from bothy.ratelimit import RateLimiter
from bothy.wake import Route, WakeServer, WakeStore, verify_signature

SECRET = "a-shared-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = json.dumps({"issue": {"id": 7}}).encode()
        self.sig = sign(self.body)

    def test_a_genuine_signature_is_accepted(self) -> None:
        self.assertTrue(verify_signature(secret=SECRET, body=self.body, signature=self.sig)[0])

    def test_prefixed_forms_are_accepted(self) -> None:
        for prefix in ("sha256=", "v1,", "v1="):
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    verify_signature(secret=SECRET, body=self.body, signature=prefix + self.sig)[0])

    def test_content_changes_are_refused(self) -> None:
        for label, body in [
            ("changed value", json.dumps({"issue": {"id": 8}}).encode()),
            ("added field", json.dumps({"issue": {"id": 7}, "evil": True}).encode()),
            ("removed field", b"{}"),
        ]:
            with self.subTest(label):
                self.assertFalse(verify_signature(secret=SECRET, body=body, signature=self.sig)[0])

    def test_the_canonical_fallback_verifies_content_not_bytes(self) -> None:
        """Documented rather than accidental.

        It is safe only because the payload is always taken from json.loads and
        the raw bytes are never used again. If anything downstream re-reads
        them, this equivalence stops being harmless.
        """
        canonical = b'{"a":1}'
        signature = sign(canonical)
        for variant in (b'{"a":1} ', b'{ "a" : 1 }', b'{"a":9,"a":1}'):
            with self.subTest(variant=variant):
                ok, _ = verify_signature(secret=SECRET, body=variant, signature=signature)
                self.assertTrue(ok)
                self.assertEqual(json.loads(variant), json.loads(canonical))

    def test_a_wrong_secret_is_refused(self) -> None:
        self.assertFalse(verify_signature(secret="other", body=self.body, signature=self.sig)[0])

    def test_a_stale_timestamp_is_refused_even_with_a_valid_signature(self) -> None:
        stale = str(int(time.time()) - 4000)
        signature = hmac.new(SECRET.encode(), f"{stale}.".encode() + self.body, hashlib.sha256).hexdigest()
        ok, reason = verify_signature(secret=SECRET, body=self.body,
                                      signature=signature, timestamp=stale)
        self.assertFalse(ok)
        self.assertIn("window", reason)

    def test_an_unsigned_route_cannot_be_configured(self) -> None:
        with self.assertRaises(ValueError):
            Route(path="/x", secret="")


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = WakeStore(self.dir)
        self.server = WakeServer(
            store=self.store, port=0, require_tailnet=True,
            limiter=RateLimiter(route_burst=200, route_rate=200,
                                caller_burst=200, caller_rate=200),
            routes=[Route(path="/ops", secret=SECRET),
                    Route(path="/hook/gh", secret=SECRET, public=True,
                          subject_from="issue.id", subject_prefix="gh-")],
        )
        self.server.serve_forever_in_background()
        self.host, self.port = self.server.address
        self.body = json.dumps({"issue": {"id": 7}}).encode()

    def tearDown(self) -> None:
        self.server.shutdown()

    def post(self, path: str, headers: dict[str, str] | None = None) -> int:
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}{path}", data=self.body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Webhook-Signature": sign(self.body), **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_a_public_route_needs_no_tailnet_identity(self) -> None:
        self.assertEqual(self.post("/hook/gh", {"X-Webhook-Delivery-Id": "a"}), 202)

    def test_a_tailnet_route_refuses_an_unidentified_caller(self) -> None:
        self.assertEqual(self.post("/ops"), 403)

    def test_a_tailnet_route_refuses_funnel_traffic(self) -> None:
        """tailscaled sets this header and it cannot be forged either way."""
        self.assertEqual(self.post("/ops", {"Tailscale-Funnel-Request": "?1"}), 403)

    def test_a_replay_is_answered_200_so_the_sender_stops(self) -> None:
        headers = {"X-Webhook-Delivery-Id": "dup-1"}
        self.assertEqual(self.post("/hook/gh", headers), 202)
        self.assertEqual(self.post("/hook/gh", headers), 200)
        self.assertEqual(len(self.store.pending()), 1, "only one wake was recorded")

    def test_dedupe_survives_a_restart(self) -> None:
        headers = {"X-Webhook-Delivery-Id": "dup-2"}
        self.post("/hook/gh", headers)
        reopened = WakeStore(self.dir)
        self.assertTrue(reopened.seen_before("/hook/gh:dup-2"))

    def test_the_subject_comes_from_the_payload_so_one_issue_is_one_lane(self) -> None:
        self.post("/hook/gh", {"X-Webhook-Delivery-Id": "s-1"})
        self.assertEqual(self.store.pending()[0]["subject"], "gh-7")

    def test_a_bad_signature_leaks_nothing(self) -> None:
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/hook/gh", data=self.body, method="POST",
            headers={"Content-Type": "application/json", "X-Webhook-Signature": "deadbeef"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(json.loads(caught.exception.read())["error"], "unauthorized")

    def test_health_answers_without_a_signature(self) -> None:
        with urllib.request.urlopen(f"http://{self.host}:{self.port}/", timeout=5) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
