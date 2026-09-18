"""Cron, the heartbeat, and the guards that stop it spamming."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from bothy import clock
from bothy.cronspec import CronError, CronSpec
from bothy.schedule import Job, Schedule, strip_no_reply

UTC = dt.timezone.utc


class CronSpecTests(unittest.TestCase):
    def test_the_or_rule_for_day_of_month_and_day_of_week(self) -> None:
        """Every cron since Vixie ORs them when both are restricted.

        "0 0 13 * FRI" means the 13th AND every Friday, not Friday the 13th.
        Quietly doing the intuitive thing would make a schedule copied from a
        working crontab behave differently here.
        """
        spec = CronSpec("0 0 13 * fri", tz=UTC)
        self.assertTrue(spec.matches(dt.datetime(2026, 9, 18, 0, 0, tzinfo=UTC)), "a Friday")
        self.assertTrue(spec.matches(dt.datetime(2026, 9, 13, 0, 0, tzinfo=UTC)), "the 13th")
        self.assertFalse(spec.matches(dt.datetime(2026, 9, 19, 0, 0, tzinfo=UTC)), "neither")

    def test_and_semantics_when_only_one_is_restricted(self) -> None:
        spec = CronSpec("0 0 13 * *", tz=UTC)
        self.assertTrue(spec.matches(dt.datetime(2026, 9, 13, 0, 0, tzinfo=UTC)))
        self.assertFalse(spec.matches(dt.datetime(2026, 9, 18, 0, 0, tzinfo=UTC)))

    def test_field_forms(self) -> None:
        base = dt.datetime(2026, 9, 18, 7, 3, tzinfo=UTC)
        for expression, expected in [
            ("*/15 * * * *", dt.datetime(2026, 9, 18, 7, 15, tzinfo=UTC)),
            ("0 9 * * mon-fri", dt.datetime(2026, 9, 18, 9, 0, tzinfo=UTC)),
            ("30 2 * * *", dt.datetime(2026, 9, 19, 2, 30, tzinfo=UTC)),
            ("0 8 1 jan *", dt.datetime(2027, 1, 1, 8, 0, tzinfo=UTC)),
        ]:
            with self.subTest(expression):
                self.assertEqual(CronSpec(expression, tz=UTC).next_after(base), expected)

    def test_bad_expressions_are_refused_when_written(self) -> None:
        for bad in ("* * * *", "60 * * * *", "5-1 * * * *", "* * * xyz *", "* * * * * *"):
            with self.subTest(bad), self.assertRaises(CronError):
                CronSpec(bad)


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.schedule = Schedule(self.dir / "jobs.json")

    def test_a_missed_backlog_fires_once_and_is_counted(self) -> None:
        """Replaying floods; deferring forever is harder to notice."""
        now = clock.utcnow()
        job = Job(id="hb", kind="every", spec="3600", prompt="?")
        self.schedule.put(job)
        job.next_due_at = clock.iso(now - dt.timedelta(hours=6))
        self.schedule.put(job)

        self.assertEqual([j.id for j in self.schedule.due(now)], ["hb"])
        fired = self.schedule.mark_fired("hb", moment=now)
        self.assertEqual(fired.misfires, 6)
        self.assertEqual(self.schedule.due(now), [], "the backlog was dropped, not replayed")

    def test_active_hours_gate_a_job(self) -> None:
        job = Job(id="office", kind="every", spec="600", prompt="?",
                  active_hours=[9, 18], tz="Europe/Paris")
        self.assertFalse(job.within_active_hours(dt.datetime(2026, 9, 18, 3, 0, tzinfo=UTC)))
        self.assertTrue(job.within_active_hours(dt.datetime(2026, 9, 18, 12, 0, tzinfo=UTC)))
        self.assertFalse(job.within_active_hours(dt.datetime(2026, 9, 18, 22, 0, tzinfo=UTC)))

    def test_a_window_may_wrap_midnight(self) -> None:
        job = Job(id="night", kind="every", spec="600", prompt="?", active_hours=[22, 6], tz="UTC")
        self.assertTrue(job.within_active_hours(dt.datetime(2026, 9, 18, 23, 0, tzinfo=UTC)))
        self.assertTrue(job.within_active_hours(dt.datetime(2026, 9, 18, 2, 0, tzinfo=UTC)))
        self.assertFalse(job.within_active_hours(dt.datetime(2026, 9, 18, 12, 0, tzinfo=UTC)))

    def test_minimum_spacing_holds_whatever_the_schedule_says(self) -> None:
        job = Job(id="fast", kind="every", spec="1", prompt="?", min_spacing_seconds=300,
                  last_fired_at=clock.iso())
        self.assertFalse(job.spaced_enough())

    def test_a_one_shot_disables_itself(self) -> None:
        moment = clock.utcnow()
        self.schedule.put(Job(id="once", kind="at", spec=clock.iso(moment - dt.timedelta(minutes=1)),
                              prompt="?"))
        self.assertEqual([j.id for j in self.schedule.due(moment)], ["once"])
        fired = self.schedule.mark_fired("once", moment=moment)
        self.assertFalse(fired.enabled)
        self.assertIsNone(fired.next_due_at)

    def test_top_of_hour_jobs_are_staggered_deterministically(self) -> None:
        """So a client box does not do everything at once at 09:00."""
        base = dt.datetime(2026, 9, 18, 8, 5, tzinfo=UTC)
        offsets = {
            job_id: CronSpec("0 9 * * *", tz=UTC) and
            Job(id=job_id, kind="cron", spec="0 9 * * *", prompt="?").compute_next(after=base).second
            for job_id in ("alpha", "beta", "gamma")
        }
        self.assertGreater(len(set(offsets.values())), 1, "different jobs get different offsets")
        again = Job(id="alpha", kind="cron", spec="0 9 * * *", prompt="?").compute_next(after=base).second
        self.assertEqual(offsets["alpha"], again, "and the same job always gets the same one")

    def test_a_bad_spec_is_refused_at_write_time(self) -> None:
        with self.assertRaises(Exception):
            self.schedule.put(Job(id="broken", kind="cron", spec="99 * * * *", prompt="?"))


class NoReplyTests(unittest.TestCase):
    def test_silence_when_there_is_nothing_to_say(self) -> None:
        for quiet in (["NO_REPLY"], ["  NO_REPLY  "], ["NO_REPLY - nothing needed me"]):
            with self.subTest(quiet):
                self.assertEqual(strip_no_reply(quiet), [])

    def test_a_real_answer_survives(self) -> None:
        said = ["Disk is at 94%, you should look."]
        self.assertEqual(strip_no_reply(said), said)

    def test_a_substantial_message_mentioning_the_token_is_not_silenced(self) -> None:
        long_answer = "NO_REPLY " + ("the convention matters because " * 20)
        self.assertEqual(len(strip_no_reply([long_answer])), 1)


if __name__ == "__main__":
    unittest.main()
