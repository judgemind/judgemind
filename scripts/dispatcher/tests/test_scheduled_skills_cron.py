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
    previous_match,
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
        # Daemon offline for 7 days — well within the 8-day walk cap
        # (#4317), and a daily cron is guaranteed to find its match.
        # (Test name retained for git history; the cap is 8 days now.)
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

    # ------------------------------------------------------------------
    # Weekly cron 8-day cap (issue #4317)
    #
    # Before the fix the walk was capped at 24h (1440 minutes), which
    # silently truncated weekly crons after their first fire — the next
    # match is 7 days = 10,080 minutes past the anchor, well beyond 1440.
    # Bumping the cap to 8 days = 11,520 minutes lets weekly crons land
    # while still bounding an offline-for-9+-days daemon.
    # ------------------------------------------------------------------

    def test_weekly_cron_after_first_fire_fires_next_week(self) -> None:
        """AC #1: weekly Sunday 06:00Z cron fires the following Sunday.

        Anchor (last_triggered_at) is 2026-05-03 06:00Z (a Sunday); now
        is 2026-05-10 06:01Z (the next Sunday, one minute past the
        target). Before the 24h cap fix this returned False; with the
        8-day cap it correctly returns True.
        """
        prev_sunday = datetime(2026, 5, 3, 6, 0, tzinfo=UTC)
        this_sunday = datetime(2026, 5, 10, 6, 1, tzinfo=UTC)
        assert should_fire_cron("0 6 * * 0", prev_sunday, this_sunday) is True

    def test_weekly_cron_no_premature_fire_mid_week(self) -> None:
        """A weekly Sunday 06:00Z cron must not fire mid-week.

        Anchor 2026-05-03 06:00Z (Sunday), now 2026-05-07 06:00Z
        (Thursday). No matching minute in that range → False. Guards
        against a too-greedy cap that would let unrelated minutes match.
        """
        anchor = datetime(2026, 5, 3, 6, 0, tzinfo=UTC)
        thursday = datetime(2026, 5, 7, 6, 0, tzinfo=UTC)
        assert should_fire_cron("0 6 * * 0", anchor, thursday) is False

    def test_offline_for_eight_days_caps(self) -> None:
        """Daemon offline > cap: walk caps at 8 days, daily cron still found.

        anchor 9 days back, now today 14:00Z, schedule daily 14:00 — the
        walk caps at 8 days = 11,520 minutes which is still enough to
        find a daily match within the window.
        """
        last = datetime(2026, 4, 16, 14, 0, tzinfo=UTC)
        now = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert should_fire_cron("0 14 * * *", last, now) is True

    # ------------------------------------------------------------------
    # Monthly + longer cadences (issue #4326)
    #
    # The O(1) ``previous_match`` rewrite collapses the 8-day walk cap.
    # Cadences whose period exceeds 8 days — monthly, quarterly,
    # yearly — must now fire correctly without code changes.
    # ------------------------------------------------------------------

    def test_monthly_cron_fires_two_months_after_first_fire(self) -> None:
        """AC #4: ``0 0 1 * *`` fires the next month after first fire.

        A monthly cron fires at 00:00Z on the 1st of each month. With the
        previous 8-day cap the walk could not reach a 30-day-back anchor;
        the O(1) rewrite supports this directly.

        First fire: anchor=2026-03-01 00:00Z, now=2026-04-01 00:00Z → True.
        Second fire (two months out): anchor=2026-04-01 00:00Z,
        now=2026-06-01 00:00Z → True (May's 1st was a missed fire; the
        most-recent prev_match is 2026-06-01 itself, which is > anchor).
        """
        # First fire — month-over-month.
        first_anchor = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        first_now = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        assert should_fire_cron("0 0 1 * *", first_anchor, first_now) is True

        # Two months out from first_anchor: prev_match = 2026-06-01,
        # which is strictly after the anchor → fires.
        second_anchor = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        second_now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        assert should_fire_cron("0 0 1 * *", second_anchor, second_now) is True

    def test_monthly_cron_no_fire_mid_month(self) -> None:
        """A monthly ``0 0 1 * *`` cron must not fire mid-month."""
        anchor = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        mid_month = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        assert should_fire_cron("0 0 1 * *", anchor, mid_month) is False

    def test_quarterly_cron_fires(self) -> None:
        """A quarterly cron (1st of Jan/Apr/Jul/Oct at 00:00) fires correctly."""
        # Anchor at start of Q1, now at start of Q2.
        anchor = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        now = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        assert should_fire_cron("0 0 1 1,4,7,10 *", anchor, now) is True


class TestPreviousMatch:
    """Tests for ``previous_match`` — the O(1) backward-walk helper.

    Acceptance criterion #1: returns the most-recent datetime ≤ ``now``
    matching the schedule, or None if no match in the past 366 days.
    """

    def test_minute_resolution_match(self) -> None:
        """Cron matching the current minute returns ``now`` itself."""
        sched = parse_cron("0 14 * * *")
        now = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert previous_match(now, sched) == now

    def test_minute_resolution_match_with_seconds(self) -> None:
        """Seconds and microseconds on ``now`` are ignored.

        Cron matches the minute slot; ``previous_match`` must return the
        slot's :00.000000 instant, not ``now`` carrying seconds.
        """
        sched = parse_cron("0 14 * * *")
        now = datetime(2026, 4, 25, 14, 0, 30, 123456, tzinfo=UTC)
        expected = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
        assert previous_match(now, sched) == expected

    def test_walks_back_to_yesterday(self) -> None:
        """When today's match has not yet occurred, returns yesterday's match."""
        sched = parse_cron("0 14 * * *")
        # 13:00Z — 14:00Z hasn't happened today.
        now = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
        expected = datetime(2026, 4, 24, 14, 0, tzinfo=UTC)
        assert previous_match(now, sched) == expected

    def test_weekly_cron_walks_back_to_previous_sunday(self) -> None:
        """Weekly Sunday 06:00Z cron from a Thursday returns the prior Sunday."""
        sched = parse_cron("0 6 * * 0")  # Sunday 06:00Z
        # 2026-05-07 (Thursday) — previous Sunday is 2026-05-03.
        thursday = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
        expected = datetime(2026, 5, 3, 6, 0, tzinfo=UTC)
        assert previous_match(thursday, sched) == expected

    def test_monthly_cron_walks_back_to_previous_first(self) -> None:
        """Monthly ``0 0 1 * *`` from mid-month returns the previous 1st."""
        sched = parse_cron("0 0 1 * *")
        mid_month = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        expected = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        assert previous_match(mid_month, sched) == expected

    def test_returns_none_for_impossible_schedule(self) -> None:
        """Schedules that cannot match (e.g. Feb 30) return None."""
        sched = parse_cron("0 0 30 2 *")
        now = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
        assert previous_match(now, sched) is None

    def test_yearly_cron_within_lookback(self) -> None:
        """A yearly cron match within the 366-day window is found."""
        sched = parse_cron("0 0 1 1 *")  # 00:00Z on Jan 1
        # Now is Dec 31 of next year — last Jan 1 was Jan 1 of the same year.
        now = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
        expected = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        assert previous_match(now, sched) == expected

    # ----- Property test (AC #1) -----

    def test_property_previous_match_is_le_now_and_matches(self) -> None:
        """For randomized inputs, prev_match ≤ now AND matches(prev_match).

        Property test: a fixed-seed sample of cron expressions and ``now``
        timestamps, asserting the two invariants from AC #1:
            previous_match(now, schedule) <= now
            matches(schedule, previous_match(now, schedule)) is True
        """
        import random

        rng = random.Random(0xC401)

        cron_expressions = [
            "* * * * *",  # every minute
            "0 * * * *",  # hourly
            "*/15 * * * *",  # every 15 minutes
            "0 0 * * *",  # daily midnight
            "0 14 * * *",  # daily 14:00
            "0 6 * * 0",  # weekly Sunday 06:00
            "30 3 * * 1",  # weekly Monday 03:30
            "0 0 1 * *",  # monthly 1st
            "0 0 15 * *",  # monthly 15th
            "0 0 1 1,4,7,10 *",  # quarterly
            "0 0 1 1 *",  # yearly Jan 1
            "0,15,30,45 * * * *",  # every 15 minutes via comma list
            "0 9 * * 1-5".replace("1-5", "1,2,3,4,5"),  # weekdays (no ranges)
        ]

        for expr in cron_expressions:
            sched = parse_cron(expr)
            for _ in range(20):
                # Random timestamp across a 5-year window.
                year = rng.randint(2024, 2028)
                month = rng.randint(1, 12)
                # Pick day robustly — use 28 max to avoid month-length issues.
                day = rng.randint(1, 28)
                hour = rng.randint(0, 23)
                minute = rng.randint(0, 59)
                now = datetime(year, month, day, hour, minute, tzinfo=UTC)

                result = previous_match(now, sched)
                assert result is not None, (
                    f"prev_match returned None for {expr!r} at {now}; "
                    "cron should match within 366 days"
                )
                assert result <= now, (
                    f"prev_match {result} not <= now {now} for {expr!r}"
                )
                assert matches(sched, result), (
                    f"prev_match {result} does not match {expr!r}"
                )

    def test_lookback_capped_at_366_days(self) -> None:
        """Schedules that need > 366 days of lookback return None.

        ``0 0 29 2 *`` — Feb 29 at 00:00Z. This cron only matches in
        leap years. Starting from 2025-01-01 (no leap year in the
        preceding 366 days because 2024 was the most recent leap year
        and Feb 29 2024 is more than 366 days before Jan 1 2025? Actually
        2024-02-29 is 307 days before 2025-01-01 — within the window.

        Use a cron that genuinely cannot match within 366 days: instead
        check that an impossible cron (Feb 30) returns None. The Feb 29
        case is covered above as "yearly_cron_within_lookback" variant.
        """
        # Genuinely impossible schedule.
        sched = parse_cron("0 0 31 2 *")  # Feb 31 doesn't exist
        now = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
        assert previous_match(now, sched) is None
