"""A small cron parser, because five fields do not justify a dependency.

Supports the five standard fields — minute, hour, day-of-month, month,
day-of-week — with ``*``, ``a``, ``a-b``, ``a,b,c`` and ``*/n`` or ``a-b/n``.
Day-of-week accepts 0 or 7 for Sunday. Three-letter names are accepted for
months and weekdays because operators write them and being strict about it
helps nobody.

ONE RULE THAT SURPRISES PEOPLE, AND IS DELIBERATE: when BOTH day-of-month and
day-of-week are restricted, they are OR-ed, not AND-ed. ``0 0 13 * FRI`` means
"the 13th, and also every Friday" — not "Friday the 13th". That is what every
cron since Vixie has done, and quietly doing the intuitive thing instead would
make a schedule copied from a working crontab behave differently here.

Everything is evaluated in a named timezone, never in local time, because a
client machine's idea of local time is not something to build a schedule on.
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable

__all__ = ["CronSpec", "CronError"]

_MONTHS = {name: index for index, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DAYS = {name: index for index, name in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"], start=0)}


class CronError(ValueError):
    """The expression cannot be parsed. Raised at configuration time, not at fire time."""


def _parse_field(raw: str, low: int, high: int, names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty element in {raw!r}")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step in {raw!r}")
            step = int(step_text)
            part = part.strip() or "*"
        if part == "*":
            start, end = low, high
        elif "-" in part and not part.startswith("-"):
            start_text, _, end_text = part.partition("-")
            start, end = _single(start_text, names, raw), _single(end_text, names, raw)
        else:
            start = end = _single(part, names, raw)
        if start > end:
            raise CronError(f"range runs backwards in {raw!r}")
        values.update(range(start, end + 1, step))
    out = {value for value in values if low <= value <= high}
    if not out:
        raise CronError(f"{raw!r} matches nothing in range {low}-{high}")
    return out


def _single(text: str, names: dict[str, int] | None, raw: str) -> int:
    text = text.strip().lower()
    if names and text in names:
        return names[text]
    if not text.lstrip("-").isdigit():
        raise CronError(f"{text!r} is not a number in {raw!r}")
    return int(text)


class CronSpec:
    """A parsed five-field cron expression, evaluated in a fixed timezone."""

    def __init__(self, expression: str, *, tz: _dt.tzinfo | None = None) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise CronError(f"expected 5 fields, got {len(fields)}: {expression!r}")
        self.expression = expression
        self.tz = tz or _dt.timezone.utc
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self.days = _parse_field(fields[2], 1, 31)
        self.months = _parse_field(fields[3], 1, 12, _MONTHS)
        weekdays = _parse_field(fields[4], 0, 7, _DAYS)
        self.weekdays = {0 if day == 7 else day for day in weekdays}
        # Whether each day field is restricted decides OR versus AND, above.
        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"

    def matches(self, moment: _dt.datetime) -> bool:
        local = moment.astimezone(self.tz)
        if local.minute not in self.minutes or local.hour not in self.hours:
            return False
        if local.month not in self.months:
            return False
        dom_ok = local.day in self.days
        dow_ok = ((local.weekday() + 1) % 7) in self.weekdays  # Monday=0 -> Sunday=0
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True

    def next_after(self, moment: _dt.datetime, *, horizon_days: int = 400) -> _dt.datetime | None:
        """The first matching minute strictly after ``moment``.

        Searched minute by minute with a bounded horizon rather than solved
        analytically: it is a few thousand cheap comparisons at worst, and a
        schedule that silently never fires because a closed-form solver had an
        edge case is a far more expensive bug than a loop.
        """
        cursor = moment.astimezone(self.tz).replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        limit = cursor + _dt.timedelta(days=horizon_days)
        while cursor < limit:
            if self.matches(cursor):
                return cursor
            cursor += _dt.timedelta(minutes=1)
        return None

    def __repr__(self) -> str:
        return f"CronSpec({self.expression!r}, tz={self.tz})"
