"""Acting without being asked.

Proactivity is much smaller than it looks. In the system Bothy learns from it is
not a special subsystem at all: the heartbeat is an ORDINARY recurring job whose
prompt ends "if nothing needs attention, reply NO_REPLY", plus a prose document
the agent maintains itself and which is appended to that prompt. That is the
whole mechanism, and it is the single highest-leverage idea in the design.

So there is no heartbeat loop here. There is a scheduler, and the heartbeat is
a row in it.

Three kinds, deliberately no more:

    at      once, at a moment
    every   on an interval
    cron    five fields, in a named timezone

FIRING PRODUCES A WAKE, not a run. A scheduled job goes onto exactly the same
durable queue as a webhook and through exactly the same admission gate. One
path means one place where lanes, budget and concurrency are enforced — a second
path would eventually disagree with the first, and the disagreement would be
found in production.

FOUR GUARDS STOP IT BECOMING A SPAM LOOP, each answering something observed:

  active hours     a schedule that fires at 3am on a client's machine had
                   better be one somebody chose deliberately
  minimum spacing  a job cannot fire again within its own floor, whatever the
                   schedule says
  misfire collapse a job that missed six firings while the box was off fires
                   ONCE now, not six times — and the backlog is dropped rather
                   than deferred, because deferring it forever is the other
                   failure and it is harder to notice
  stagger          jobs due on the hour are spread deterministically, so a
                   client's machine does not do everything at once at 09:00

NOTHING IS SAID WHEN THERE IS NOTHING TO SAY. ``NO_REPLY`` is filtered from
every outgoing path. A scheduled report that speaks every day teaches everyone
to ignore it, and then the day it matters it is ignored too.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import clock
from .cronspec import CronError, CronSpec

__all__ = ["Job", "Schedule", "NO_REPLY", "strip_no_reply", "ScheduleError"]

NO_REPLY = "NO_REPLY"
MAX_STAGGER_SECONDS = 300


class ScheduleError(ValueError):
    """A job definition that cannot be honoured. Raised when it is written, not when it fires."""


def strip_no_reply(messages: list[str]) -> list[str]:
    """Remove the agent's way of saying "nothing needed me".

    Matched at the start or end of a message rather than anywhere inside, so a
    run that genuinely discusses the convention is not silenced by mentioning it.
    """
    kept: list[str] = []
    for message in messages:
        stripped = message.strip()
        if not stripped:
            continue
        if stripped == NO_REPLY:
            continue
        if stripped.startswith(NO_REPLY) or stripped.endswith(NO_REPLY):
            remainder = stripped.removeprefix(NO_REPLY).removesuffix(NO_REPLY).strip()
            # A bare acknowledgement with a sentence attached is still an
            # acknowledgement; anything substantial is a real answer.
            if len(remainder) <= 300:
                continue
            kept.append(remainder)
            continue
        kept.append(stripped)
    return kept


@dataclasses.dataclass
class Job:
    """One scheduled thing."""

    id: str
    kind: str                       # at | every | cron
    spec: str                       # ISO instant | seconds | five cron fields
    prompt: str
    subject: str | None = None
    enabled: bool = True
    tz: str = "UTC"
    active_hours: list[int] | None = None      # [start, end) in the job's tz
    min_spacing_seconds: float = 60.0
    heartbeat: bool = False                    # append the standing checklist
    profile: str | None = None                 # capability bundle; None = built-ins only
    last_fired_at: str | None = None
    next_due_at: str | None = None
    misfires: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Job":
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})

    def zone(self) -> _dt.tzinfo:
        if self.tz.upper() == "UTC":
            return _dt.timezone.utc
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(self.tz)
        except Exception as exc:  # noqa: BLE001
            raise ScheduleError(f"job {self.id}: unknown timezone {self.tz!r}") from exc

    def lane(self) -> str:
        """The subject this job's runs occupy, so a slow job cannot overlap itself."""
        return self.subject or f"job:{self.id}"

    def _stagger(self) -> float:
        """A stable offset in seconds, derived from the job id.

        Deterministic rather than random so a restart does not reshuffle every
        schedule, and so two jobs keep their relative order between runs.
        """
        digest = hashlib.sha256(self.id.encode("utf-8")).digest()
        return (int.from_bytes(digest[:4], "big") % MAX_STAGGER_SECONDS)

    def period_seconds(self) -> float | None:
        if self.kind == "every":
            return float(self.spec)
        if self.kind == "cron":
            return None
        return None

    def compute_next(self, *, after: _dt.datetime | None = None) -> _dt.datetime | None:
        """When this job should next fire, or None if it never should again."""
        moment = after or clock.utcnow()
        if self.kind == "at":
            due = clock.parse(self.spec)
            return due if self.last_fired_at is None else None
        if self.kind == "every":
            try:
                seconds = float(self.spec)
            except ValueError as exc:
                raise ScheduleError(f"job {self.id}: 'every' needs seconds, got {self.spec!r}") from exc
            if seconds <= 0:
                raise ScheduleError(f"job {self.id}: interval must be positive")
            base = clock.parse(self.last_fired_at) if self.last_fired_at else moment
            return base + _dt.timedelta(seconds=seconds)
        if self.kind == "cron":
            try:
                spec = CronSpec(self.spec, tz=self.zone())
            except CronError as exc:
                raise ScheduleError(f"job {self.id}: {exc}") from exc
            nxt = spec.next_after(moment)
            if nxt is None:
                return None
            # Spread top-of-hour jobs so a client box does not do everything at 09:00.
            if nxt.minute == 0:
                nxt = nxt + _dt.timedelta(seconds=self._stagger())
            return nxt.astimezone(_dt.timezone.utc)
        raise ScheduleError(f"job {self.id}: unknown kind {self.kind!r}")

    def within_active_hours(self, moment: _dt.datetime | None = None) -> bool:
        if not self.active_hours:
            return True
        start, end = self.active_hours[0], self.active_hours[1]
        hour = (moment or clock.utcnow()).astimezone(self.zone()).hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end   # a window that wraps midnight

    def spaced_enough(self, moment: _dt.datetime | None = None) -> bool:
        if self.last_fired_at is None:
            return True
        return clock.age_seconds(self.last_fired_at, reference=moment or clock.utcnow()) >= self.min_spacing_seconds


class Schedule:
    """The job store, and the decision about what is due."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # ---- storage -------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name, suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(data, handle, sort_keys=True, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise

    def jobs(self) -> list[Job]:
        return [Job.from_dict(raw) for raw in self._load().values()]

    def get(self, job_id: str) -> Job | None:
        raw = self._load().get(job_id)
        return Job.from_dict(raw) if raw else None

    def put(self, job: Job) -> Job:
        """Add or replace a job, validating it now rather than at fire time."""
        job.compute_next()          # raises ScheduleError on a bad spec
        job.zone()
        with self._lock:
            data = self._load()
            if job.next_due_at is None:
                due = job.compute_next()
                job.next_due_at = clock.iso(due) if due else None
            data[job.id] = job.as_dict()
            self._save(data)
        return job

    def remove(self, job_id: str) -> bool:
        with self._lock:
            data = self._load()
            if data.pop(job_id, None) is None:
                return False
            self._save(data)
            return True

    # ---- the decision --------------------------------------------------

    def due(self, moment: _dt.datetime | None = None) -> list[Job]:
        """Jobs that should fire now, after every guard has had its say."""
        now = moment or clock.utcnow()
        ready: list[Job] = []
        for job in self.jobs():
            if not job.enabled or job.next_due_at is None:
                continue
            try:
                due_at = clock.parse(job.next_due_at)
            except ValueError:
                continue
            if due_at > now:
                continue
            if not job.within_active_hours(now) or not job.spaced_enough(now):
                continue
            ready.append(job)
        return ready

    def mark_fired(self, job_id: str, *, moment: _dt.datetime | None = None) -> Job | None:
        """Record a firing and schedule the next one, collapsing any backlog.

        A job that missed several firings while the machine was off fires once
        and then moves on. Replaying the backlog would flood; deferring it
        forever is the other failure mode and is much harder to notice, so the
        misses are counted and dropped.
        """
        now = moment or clock.utcnow()
        with self._lock:
            data = self._load()
            raw = data.get(job_id)
            if raw is None:
                return None
            job = Job.from_dict(raw)
            missed = 0
            period = job.period_seconds()
            if job.next_due_at:
                with contextlib.suppress(ValueError):
                    behind = clock.age_seconds(job.next_due_at, reference=now)
                    if period and behind > period:
                        missed = int(behind // period)
            job.last_fired_at = clock.iso(now)
            job.misfires += max(0, missed)
            try:
                nxt = job.compute_next(after=now)
            except ScheduleError:
                nxt = None
                job.enabled = False
            job.next_due_at = clock.iso(nxt) if nxt else None
            if job.kind == "at" and nxt is None:
                job.enabled = False
            data[job_id] = job.as_dict()
            self._save(data)
            return job
