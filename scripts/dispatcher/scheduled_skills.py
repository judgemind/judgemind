"""Generalized cron-like scheduler for periodic skill dispatch.

Issue #3374. Provides a minimal 5-field cron parser and a
``should_fire_cron`` helper that answers the question "given a cron
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

Algorithm (issue #4326)
-----------------------
``should_fire_cron`` is implemented in O(1) per call via
:func:`previous_match` — a backward search from ``now`` that walks
candidate days until it finds the most-recent datetime ≤ ``now`` matching
the schedule. Because the search is bounded at 366 days (one full year +
a leap-day cushion) it supports every cadence cron can express —
hourly, daily, weekly, monthly, quarterly, yearly — with no per-cadence
"walk cap" tuning. Issue #4317 replaced the old 24h cap with an 8-day
cap to support weekly cron; #4326 collapses the cap entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Lookback window used when ``last_triggered_at`` is NULL (first-ever fire or
#: manually reset).  Walking 24 hours ensures that a cron whose exact minute
#: already passed today is still found and fired once on the next daemon tick,
#: matching standard cron "fire exactly once if missed" semantics.
_NULL_ANCHOR_LOOKBACK = timedelta(hours=24)

#: Maximum number of days :func:`previous_match` will walk backward from
#: ``now`` before giving up. 366 covers every realistic cadence (yearly
#: schedules included, with a leap-day cushion).  An expression that
#: matches no minute in the past 366 days returns None — this is treated
#: as "no past match in the working range" by callers.
_MAX_LOOKBACK_DAYS = 366


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


def _day_matches(schedule: CronSchedule, when: datetime) -> bool:
    """True if *when*'s month/day-of-month/day-of-week match *schedule*.

    Used by :func:`previous_match` to skip whole days that cannot
    contain any matching minute, so the inner hour/minute search only
    runs on days the schedule could fire.
    """
    return (
        when.month in schedule.months
        and when.day in schedule.days_of_month
        and ((when.weekday() + 1) % 7) in schedule.days_of_week
    )


def previous_match(now: datetime, schedule: CronSchedule) -> datetime | None:
    """Return the most-recent datetime ≤ *now* that matches *schedule*.

    Returns ``None`` if no matching minute exists in the preceding
    :data:`_MAX_LOOKBACK_DAYS` (366 days). Comparison is at minute
    resolution — seconds and microseconds on *now* are ignored when
    comparing to the candidate slot.

    The algorithm steps backward day-by-day (starting at ``now``'s
    date). For each candidate day:

    * If the day's month/day-of-month/day-of-week do not match the
      schedule, skip the entire day (no minute on that day can match).
    * Otherwise, look for the largest ``(hour, minute)`` slot on that
      day that is allowed by the schedule and ≤ ``now`` (when the day
      is ``now``'s date) or simply the largest allowed slot (when the
      day is earlier than ``now``'s date).

    O(1) per call: the day-loop is bounded by ``_MAX_LOOKBACK_DAYS``
    and the hour/minute search inside each candidate day enumerates at
    most 24 × 60 = 1440 slots in the worst case (and typically far
    fewer because hours/minutes are usually small frozensets).
    """
    # Drop seconds/microseconds — cron is minute-resolution.
    cursor = now.replace(second=0, microsecond=0)
    # Pre-sort the allowed hours and minutes once so the inner search
    # can do bounded backward iteration without re-sorting per day.
    sorted_hours = sorted(schedule.hours, reverse=True)
    sorted_minutes = sorted(schedule.minutes, reverse=True)
    if not sorted_hours or not sorted_minutes:
        return None

    cutoff = cursor - timedelta(days=_MAX_LOOKBACK_DAYS)
    # ``cursor.date()`` candidate first; then walk back day by day.
    candidate_day = cursor
    days_walked = 0
    while candidate_day >= cutoff:
        if _day_matches(schedule, candidate_day):
            # Search for the largest matching (hour, minute) on this day
            # that is ≤ cursor when candidate_day is cursor's date, or
            # any matching slot otherwise.
            same_day = (
                candidate_day.year == cursor.year
                and candidate_day.month == cursor.month
                and candidate_day.day == cursor.day
            )
            for hour in sorted_hours:
                if same_day and hour > cursor.hour:
                    continue
                for minute in sorted_minutes:
                    # When the candidate hour equals cursor.hour, the
                    # minute must be ≤ cursor.minute on the same day.
                    if same_day and hour == cursor.hour and minute > cursor.minute:
                        continue
                    return candidate_day.replace(hour=hour, minute=minute)
        candidate_day = candidate_day - timedelta(days=1)
        days_walked += 1
        if days_walked > _MAX_LOOKBACK_DAYS:
            break
    return None


def should_fire_cron(
    expression: str,
    last_triggered_at: datetime | None,
    now: datetime,
) -> bool:
    """Should a cron-triggered skill fire on the current supervisor tick?

    The supervisor tick fires every 2 minutes by default, so we cannot
    rely on the tick lining up with a specific minute. Instead: locate
    the most-recent past match (``previous_match``) and decide whether
    it falls in the "should fire" window.

    Semantics:

    * ``last_triggered_at is None`` (first-ever fire / manually reset):
      fire if the most-recent past match falls within the last
      :data:`_NULL_ANCHOR_LOOKBACK` (24 hours). This matches standard
      cron "fire exactly once if missed" semantics — a target minute
      that already passed today is still picked up on the next tick.
    * ``last_triggered_at`` is set: fire if the most-recent past match
      is strictly after ``last_triggered_at``. This guarantees we
      neither double-fire (the same matching minute) nor lose fires
      across long gaps (an O(1) reach to the most-recent match supports
      every cadence cron can express).
    * Defensive: if ``last_triggered_at`` is in the future (clock skew
      or restored DB snapshot), do not fire — treat as up-to-date.

    Performance is O(1) regardless of cadence — issue #4326 replaced
    the prior O(N) minute-walk + per-cadence cap with a bounded
    backward search via :func:`previous_match`.
    """
    schedule = parse_cron(expression)
    # Defensive: if last_triggered is in the future (clock skew or
    # restored DB snapshot), don't fire — treat as up-to-date.
    if last_triggered_at is not None and last_triggered_at >= now:
        return False
    prev = previous_match(now, schedule)
    if prev is None:
        return False
    if last_triggered_at is None:
        return prev >= now - _NULL_ANCHOR_LOOKBACK
    return prev > last_triggered_at


__all__ = [
    "CronParseError",
    "CronSchedule",
    "matches",
    "parse_cron",
    "previous_match",
    "should_fire_cron",
]
