"""Cron parser + ``should_fire_cron`` tests (issue #3374).

Fake-clock tests that assert:
* a cron expression matching the current minute fires.
* a cron expression matching no minute since last_triggered does not fire.
* the ``*/N`` step form parses correctly.
* malformed expressions raise :class:`CronParseError`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.scheduled_skills import (  # noqa: E402
    CronParseError,
    matches,
    parse_cron,
    should_fire_cron,
)


# --------------------------------------------------------------------------
# Parser tests
# --------------------------------------------------------------------------


class TestParseCron:
    def test_wildcard_every_field(self) -> None:
        sched = parse_cron("* * * * *")
        assert sched.minutes == frozenset(range(60))
        assert sched.hours == frozenset(range(24))
        assert sched.days_of_month == frozenset(range(1, 32))
        assert sched.months == frozenset(range(1, 13))
        assert sched.days_of_week == frozenset(range(7))

    def test_fixed_values(self) -> None:
        sched = parse_cron("0 14 * * *")  # 14:00 every day
        assert sched.minutes == frozenset({0})
        assert sched.hours == frozenset({14})

    def test_step_form(self) -> None:
        sched = parse_cron("*/5 * * * *")  # every 5 minutes
        assert sched.minutes == frozenset(range(0, 60, 5))

    def test_comma_list(self) -> None:
        sched = parse_cron("0,15,30,45 * * * *")
        assert sched.minutes == frozenset({0, 15, 30, 45})

    def test_too_few_fields(self) -> None:
        with pytest.raises(CronParseError):
            parse_cron("0 14 * *")

    def test_too_many_fields(self) -> None:
        with pytest.raises(CronParseError):
            parse_cron("0 14 * * * *")

    def test_out_of_range(self) -> None:
        with pytest.raises(CronParseError):
            parse_cron("60 * * * *")
        with pytest.raises(CronParseError):
            parse_cron("0 24 * * *")

    def test_non_numeric_term(self) -> None:
        with pytest.raises(CronParseError):
            parse_cron("foo * * * *")


# --------------------------------------------------------------------------
# matches() tests
# --------------------------------------------------------------------------


class TestMatches:
    def test_daily_at_14(self) -> None:
        sched = parse_cron("0 14 * * *")
        assert matches(sched, datetime(2026, 4, 25, 14, 0, tzinfo=UTC))
        assert not matches(sched, datetime(2026, 4, 25, 13, 59, tzinfo=UTC))
        assert not matches(sched, datetime(2026, 4, 25, 14, 1, tzinfo=UTC))

    def test_every_5_minutes(self) -> None:
        sched = parse_cron("*/5 * * * *")
        assert matches(sched, datetime(2026, 4, 25, 14, 0, tzinfo=UTC))
        assert matches(sched, datetime(2026, 4, 25, 14, 5, tzinfo=UTC))
        assert not matches(sched, datetime(2026, 4, 25, 14, 7, tzinfo=UTC))

    def test_dow_sunday_zero(self) -> None:
        # Sunday-only schedule. 2026-04-26 is a Sunday.
        sched = parse_cron("0 0 * * 0")
        assert matches(sched, datetime(2026, 4, 26, 0, 0, tzinfo=UTC))
        # 2026-04-25 is a Saturday → no match.
        assert not matches(sched, datetime(2026, 4, 25, 0, 0, tzinfo=UTC))


# --------------------------------------------------------------------------
# should_fire_cron() tests
# --------------------------------------------------------------------------


class TestShouldFireCron:
    def test_first_fire_on_matching_minute(self) -> None:
        # last_triggered=None → "first fire ever". Anchor is now-1m so
        # a cron matching the current minute fires.
        now = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", None, now) is True

    def test_first_fire_no_match_in_24h(self) -> None:
        # An expression that can never match (Feb 30 does not exist) yields
        # False regardless of the NULL-anchor 24h lookback.
        now = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
        assert should_fire_cron("0 0 30 2 *", None, now) is False

    def test_no_fire_before_next_match(self) -> None:
        # Last fired at 14:00; "now" is 14:01 — same matching minute
        # has already fired, the next match is tomorrow at 14:00.
        last = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 1, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", last, now) is False

    def test_fire_after_next_match(self) -> None:
        # Last fired yesterday at 14:00; now is today at 14:00.
        last = datetime(2026, 4, 24, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", last, now) is True

    def test_supervisor_tick_lag_2_minutes(self) -> None:
        # The supervisor tick is every 2 minutes. If the cron matches at
        # 14:00 and the tick fires at 14:01, we still want to fire.
        last = datetime(2026, 4, 24, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 1, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", last, now) is True

    def test_every_5_minutes(self) -> None:
        # Schedule fires every 5 minutes.
        last = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 5, tzinfo=UTC)
        assert should_fire_cron("*/5 * * * *", last, now) is True

    def test_clock_skew_future_last_fired(self) -> None:
        # Defensive: last_triggered_at is somehow in the future
        # (clock skew, restored DB snapshot). Treat as up-to-date.
        last = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", last, now) is False

    def test_offline_for_days_caps_at_24h(self) -> None:
        # Daemon offline for 7 days — the walk caps at 24h, but a
        # daily cron is guaranteed to find its match within that window.
        last = datetime(2026, 4, 18, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", last, now) is True

    def test_malformed_expression_raises(self) -> None:
        last = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 1, tzinfo=UTC)
        with pytest.raises(CronParseError):
            should_fire_cron("not a cron", last, now)

    # ------------------------------------------------------------------
    # NULL-anchor 24h lookback (issue #3424)
    # ------------------------------------------------------------------

    def test_first_fire_finds_match_within_24h(self) -> None:
        """AC #1 + AC #4: last=None, now=13:00Z, expr 0 12 * * * → True.

        The cron's 12:00 slot already passed today (1 hour ago), but the
        24h lookback window reaches back to yesterday's 12:00, so the walk
        finds today's 12:00 and returns True.
        """
        now = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
        assert should_fire_cron("0 12 * * *", None, now) is True

    def test_consecutive_ticks_fire_once(self) -> None:
        """AC #5: two consecutive ticks; second must NOT re-fire.

        First tick: last=None, now=12:00:30Z → True (12:00 found in window).
        Second tick: last=12:00:30Z (set by caller after first fire),
        now=12:01:30Z → False (next 12:00 is tomorrow).
        """
        now_first = datetime(2026, 4, 25, 12, 0, 30, tzinfo=UTC)
        assert should_fire_cron("0 12 * * *", None, now_first) is True

        last_after_first_fire = datetime(2026, 4, 25, 12, 0, 30, tzinfo=UTC)
        now_second = datetime(2026, 4, 25, 12, 1, 30, tzinfo=UTC)
        assert (
            should_fire_cron("0 12 * * *", last_after_first_fire, now_second) is False
        )
