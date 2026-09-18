"""Clock, ids, rate limiting, tailnet and retention."""

from __future__ import annotations

import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from bothy import clock, ids
from bothy.audit import AuditLog
from bothy.config import Config
from bothy.ratelimit import RateLimiter
from bothy.retention import RetentionPolicy, sweep
from bothy.tailnet import TailnetError, whois
from bothy.vendor.a2a_reactor.queue import append_event
from bothy.wake import WakeStore


class ClockTests(unittest.TestCase):
    def test_a_naive_timestamp_is_refused(self) -> None:
        """Three separate incidents elsewhere were timezone bugs.

        The worst used mktime on a UTC log timestamp, shifting it by the local
        offset, so a reply twenty seconds old read as idle and was aborted.
        """
        with self.assertRaises(ValueError):
            clock.parse("2026-09-18T05:00:00")

    def test_z_means_utc(self) -> None:
        self.assertEqual(clock.parse("2026-09-18T05:00:00Z").utcoffset(), dt.timedelta(0))

    def test_serialising_a_naive_datetime_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            clock.iso(dt.datetime(2026, 9, 18, 5, 0))

    def test_epoch_conversion_for_rate_limit_resets(self) -> None:
        """"resets at 1789903902" is a refusal nobody can act on."""
        self.assertEqual(clock.iso(clock.from_epoch(1789903902)), "2026-09-20T11:31:42+00:00")
        self.assertIsNone(clock.from_epoch(None))

    def test_a_future_timestamp_reads_negative_rather_than_zero(self) -> None:
        """A clock-skewed peer is worth seeing, not flattening."""
        ahead = clock.utcnow() + dt.timedelta(minutes=5)
        self.assertLess(clock.age_seconds(ahead), 0)


class IdTests(unittest.TestCase):
    def test_ids_sort_chronologically(self) -> None:
        first = ids.run_id()
        time.sleep(1.05)
        self.assertLess(first, ids.run_id())

    def test_shape_is_checkable(self) -> None:
        self.assertTrue(ids.is_run_id(ids.run_id()))
        self.assertFalse(ids.is_run_id(ids.wake_id()))
        self.assertFalse(ids.is_run_id("run_nope"))

    def test_kinds_are_distinguishable(self) -> None:
        self.assertNotEqual(ids.run_id()[:3], ids.wake_id()[:3])


class RateLimitTests(unittest.TestCase):
    def test_one_noisy_caller_does_not_punish_everyone(self) -> None:
        limiter = RateLimiter(route_burst=100, route_rate=100, caller_burst=3, caller_rate=0.01)
        for _ in range(3):
            self.assertTrue(limiter.check("/hook", "1.2.3.4")[0])
        self.assertFalse(limiter.check("/hook", "1.2.3.4")[0])
        self.assertTrue(limiter.check("/hook", "9.9.9.9")[0], "a different caller is unaffected")

    def test_the_route_bucket_protects_against_everyone_at_once(self) -> None:
        limiter = RateLimiter(route_burst=3, route_rate=0.01, caller_burst=100, caller_rate=100)
        for index in range(3):
            self.assertTrue(limiter.check("/hook", f"10.0.0.{index}")[0])
        allowed, retry_after, which = limiter.check("/hook", "10.0.0.99")
        self.assertFalse(allowed)
        self.assertEqual(which, "route")
        self.assertGreater(retry_after, 0)

    def test_the_caller_table_is_bounded(self) -> None:
        """A flood from many addresses must not become a memory leak."""
        limiter = RateLimiter(max_callers=100)
        for index in range(400):
            limiter.check("/hook", f"10.1.{index // 256}.{index % 256}")
        self.assertLessEqual(limiter.snapshot()["callers_tracked"], 100)


class TailnetTests(unittest.TestCase):
    def test_a_non_tailnet_address_is_refused_rather_than_waved_through(self) -> None:
        """A verifier that fails open is not a verifier."""
        for address in ("8.8.8.8", "192.0.2.1"):
            with self.subTest(address), self.assertRaises(TailnetError):
                whois(address)

    def test_no_daemon_means_refusal_not_permission(self) -> None:
        with self.assertRaises(TailnetError):
            whois("100.64.0.1", sock="/definitely/not/a/socket")


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(home=Path(tempfile.mkdtemp()))
        self.config.ensure_dirs()

    def test_unfinished_work_is_never_swept(self) -> None:
        queue = self.config.wake_dir / "wakes.jsonl"
        append_event(queue, {"id": "old", "received_at": "2020-01-01T00:00:00+00:00", "processed": True})
        append_event(queue, {"id": "live", "received_at": "2020-01-01T00:00:00+00:00", "processed": False})
        report = sweep(self.config, RetentionPolicy(wake_keep_days=1))
        remaining = {event["id"] for event in WakeStore(self.config.wake_dir).all_events()}
        self.assertIn("live", remaining)
        self.assertNotIn("old", remaining)
        self.assertEqual(report.wakes_dropped, 1)

    def test_a_young_worker_home_is_left_alone(self) -> None:
        """Removing a home from under a starting worker is worse than waiting."""
        home = self.config.homes_dir / "run_new"
        home.mkdir(parents=True)
        sweep(self.config, RetentionPolicy(home_orphan_minutes=60))
        self.assertTrue(home.exists())

    def test_sweeping_twice_changes_nothing(self) -> None:
        self.assertFalse(sweep(self.config, RetentionPolicy()).did_anything())

    def test_the_audit_rotates_when_it_grows(self) -> None:
        log = AuditLog(self.config.audit_path, max_bytes=500)
        for index in range(8):
            log.append(kind="run", action="finished", run_id=f"r{index}")
        report = sweep(self.config, RetentionPolicy(audit_max_bytes=500))
        self.assertIsNotNone(report.audit_rotated)
        self.assertEqual(AuditLog(self.config.audit_path).verify(), 1)


if __name__ == "__main__":
    unittest.main()


class PeerTests(unittest.TestCase):
    """Tags, because one tailnet per client means access by tag, not by device."""

    def test_a_tagged_peer_is_labelled_by_its_tag(self) -> None:
        """A tagged node has no human owner; naming one would be a lie."""
        from bothy.tailnet import Peer

        tagged = Peer(login="", node="mini.ts.net", node_id="nABC", tags=("tag:bothy",))
        self.assertTrue(tagged.is_tagged)
        self.assertEqual(tagged.label(), "tag:bothy@mini.ts.net")

    def test_an_untagged_peer_is_labelled_by_its_login(self) -> None:
        from bothy.tailnet import Peer

        peer = Peer(login="you@example.com", node="laptop.ts.net", node_id="nDEF")
        self.assertFalse(peer.is_tagged)
        self.assertEqual(peer.label(), "you@example.com@laptop.ts.net")


class CapabilityValidationTests(unittest.TestCase):
    """Contradictory grants are refused when the profile is read."""

    def test_gating_a_tool_must_not_be_what_enables_it(self) -> None:
        from bothy.capability import Profile

        with self.assertRaises(ValueError) as caught:
            Profile.from_dict("mail", {
                "mcp_servers": {"gmail": {"command": "x"}},
                "mcp_tools": {"gmail": ["search"]},
                "mcp_ask": {"gmail": ["send"]},
            })
        self.assertIn("must not be what enables it", str(caught.exception))

    def test_scoping_a_server_the_profile_does_not_grant_is_refused(self) -> None:
        from bothy.capability import Profile

        with self.assertRaises(ValueError):
            Profile.from_dict("p", {"mcp_servers": {}, "mcp_tools": {"slack": ["post"]}})

    def test_a_scoped_server_renders_enabled_tools(self) -> None:
        """Least privilege rendered, rather than remembered."""
        from bothy.capability import Profile, render_config_toml

        profile = Profile.from_dict("mail", {
            "mcp_servers": {"gmail": {"command": "mcp-gmail"}},
            "mcp_tools": {"gmail": ["search", "send"]},
            "mcp_ask": {"gmail": ["send"]},
        })
        rendered = render_config_toml(profile)
        self.assertIn('enabled_tools = ["search", "send"]', rendered)
        self.assertIn("[mcp_servers.gmail.tools.send]", rendered)
        self.assertIn('approval_mode = "always"', rendered)

    def test_an_unscoped_server_says_so_loudly(self) -> None:
        from bothy.capability import Profile

        profile = Profile.from_dict("wide", {"mcp_servers": {"gmail": {"command": "x"}}})
        self.assertIn("ALL TOOLS", profile.summary())
