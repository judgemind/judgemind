"""Generalized cron-like scheduler for periodic skill dispatch.

Issue #3374. Provides a minimal 5-field cron parser and a
``next_fire_time`` helper that answers the question "given a cron
expression, an anchor time (``last_triggered_at`` or NULL), and now,
should the daemon fire this skill on this tick?".

The daemon's ``_scheduled_skills_tick`` (in ``daemon.py``) consumes
this module to decide which rows in ``dispatcher.scheduled_skills``
to fire. The parser is intentionally minimal — enough to support the
expressions Judgemind actually uses (wildcard ``*``, fixed integers,
the ``*/N`` step form, and comma-separated lists). No ranges, no
nicknames, no day-of-week modulo arithmetic. If we ever need a fuller
parser, swap in ``croniter`` here without touching the daemon.

Design notes
------------
* Pure functions — easy to unit-test with a frozen ``now``.
* Returns ``True`` for "fire now"; the daemon writes
  ``last_triggered_at = now()`` on fire so the next tick re-anchors.
* On parse error: log + ``False`` (skip this row this tick). A bad
  cron expression should never break the supervisor loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


# Field bounds, matching the standard 5-field cron layout:
#   minute hour day_of_month month day_of_week
# Day-of-week uses 0–6 with Sunday=0 to match POSIX cron.
_FIELD_BOUNDS: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week (Sun=0)
)


@dataclass(frozen=True)
class CronSchedule:
    """Parsed 5-field cron expression.

    Each field is a frozenset of integers giving the allowed values for
    that field. ``*`` parses to "every value in the field's bounds".
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]


class CronParseError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def parse_cron(expression: str) -> CronSchedule:
    """Parse a 5-field cron expression.

    Supported forms per field:
      - ``*``                  — every allowed value
      - ``N``                  — fixed integer
      - ``*/N``                — every Nth value starting at the field's lower bound
      - ``A,B,C``              — comma-separated list of any of the above

    Raises :class:`CronParseError` on malformed input.
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        raise CronParseError(
            f"expected 5 cron fields (minute hour dom month dow); got {len(fields)} "
            f"in {expression!r}"
        )
    parsed: list[frozenset[int]] = []
    for value, (lo, hi) in zip(fields, _FIELD_BOUNDS, strict=True):
        parsed.append(_parse_field(value, lo, hi))
    return CronSchedule(
        minutes=parsed[0],
        hours=parsed[1],
        days_of_month=parsed[2],
        months=parsed[3],
        days_of_week=parsed[4],
    )


def _parse_field(value: str, lo: int, hi: int) -> frozenset[int]:
    """Parse a single cron field into the set of allowed integers."""
    out: set[int] = set()
    # Comma list — each entry parses independently.
    for term in value.split(","):
        term = term.strip()
        if not term:
            raise CronParseError(f"empty term in field {value!r}")
        if term == "*":
            out.update(range(lo, hi + 1))
            continue
        if term.startswith("*/"):
            try:
                step = int(term[2:])
            except ValueError as exc:
                raise CronParseError(f"non-integer step in {term!r}") from exc
            if step <= 0:
                raise CronParseError(f"step must be positive: {term!r}")
            out.update(range(lo, hi + 1, step))
            continue
        # Fixed integer.
        try:
            n = int(term)
        except ValueError as exc:
            raise CronParseError(f"unrecognized cron term {term!r}") from exc
        if not lo <= n <= hi:
            raise CronParseError(f"value {n} out of range [{lo},{hi}] in {term!r}")
        out.add(n)
    return frozenset(out)


def matches(schedule: CronSchedule, when: datetime) -> bool:
    """Return True if *when* matches *schedule*.

    Compared at minute resolution (cron's native granularity). Seconds
    and microseconds are ignored.
    """
    return (
        when.minute in schedule.minutes
        and when.hour in schedule.hours
        and when.day in schedule.days_of_month
        and when.month in schedule.months
        # POSIX cron uses Sun=0..Sat=6; Python's weekday() is Mon=0..Sun=6.
        # Convert: (weekday + 1) % 7 -> Sun=0..Sat=6.
        and ((when.weekday() + 1) % 7) in schedule.days_of_week
    )


def should_fire_cron(
    expression: str,
    last_triggered_at: datetime | None,
    now: datetime,
) -> bool:
    """Should a cron-triggered skill fire on the current supervisor tick?

    The supervisor tick fires every 2 minutes by default, so we cannot
    rely on the tick lining up with a specific minute. Instead: scan
    the minute-resolution range (``last_triggered_at`` exclusive, ``now``
    inclusive) and return True if any minute in that range matches the
    cron schedule.

    On first-ever fire (``last_triggered_at is None``) the anchor is
    set to ``now - 1 minute`` so that a cron expression matching the
    *current* minute fires on this tick rather than waiting until the
    next match.

    Bounds:
      - The walk is capped at 24 hours so a daemon that was offline
        for days does not enumerate hundreds of thousands of minutes.
        A cron that fires at most daily is guaranteed to find its
        match in 1440 iterations.
    """
    schedule = parse_cron(expression)
    if last_triggered_at is None:
        # First fire — let "now matches" trigger immediately.
        anchor = now - timedelta(minutes=1)
    else:
        anchor = last_triggered_at
    # Defensive: if last_triggered is in the future (clock skew or
    # restored DB snapshot), don't fire — treat as up-to-date.
    if anchor >= now:
        return False
    # Walk minute by minute from anchor+1 minute up to now (inclusive).
    cursor = anchor.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now.replace(second=0, microsecond=0)
    iterations = 0
    while cursor <= end:
        if matches(schedule, cursor):
            return True
        cursor += timedelta(minutes=1)
        iterations += 1
        if iterations > 60 * 24:  # 24h safety cap
            break
    return False


__all__ = [
    "CronParseError",
    "CronSchedule",
    "matches",
    "parse_cron",
    "should_fire_cron",
]
