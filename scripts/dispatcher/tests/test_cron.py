"""Unit tests for ``dispatcher.cron.cron_matches``.

Issue #2864 — idle-hook triggers for /audit and /spotcheck.

Covers:
- Daily expression ``0 14 * * *`` matches at 14:00 UTC and not at 14:01.
- Unsupported ``*/5`` step syntax raises ``ValueError``.
- Comma-separated list ``0,30 * * * *`` matches at :00 and :30, not :15.
- Malformed (wrong field count) expression raises ``ValueError``.
- Range syntax ``1-5`` raises ``ValueError``.
- Named token raises ``ValueError``.
- Out-of-range value raises ``ValueError``.
- Wildcard ``*`` in every field matches.
- Day-of-week 0 (Sunday) == 7 (Sunday) normalisation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.cron import cron_matches  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Daily expression: 0 14 * * *
# ---------------------------------------------------------------------------


class TestDailyAt14:
    EXPR = "0 14 * * *"

    def test_matches_at_1400(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 6, 15, 14, 0)) is True

    def test_no_match_at_1401(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 6, 15, 14, 1)) is False

    def test_no_match_at_1359(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 6, 15, 13, 59)) is False

    def test_no_match_at_1500(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 6, 15, 15, 0)) is False

    def test_matches_on_different_days(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 12, 31, 14, 0)) is True


# ---------------------------------------------------------------------------
# Comma-separated list: 0,30 * * * *
# ---------------------------------------------------------------------------


class TestCommaList:
    EXPR = "0,30 * * * *"

    def test_matches_at_hour_zero(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 1, 1, 9, 0)) is True

    def test_matches_at_thirty(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 1, 1, 9, 30)) is True

    def test_no_match_at_fifteen(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 1, 1, 9, 15)) is False

    def test_no_match_at_one(self) -> None:
        assert cron_matches(self.EXPR, _utc(2024, 1, 1, 9, 1)) is False


# ---------------------------------------------------------------------------
# Step syntax raises ValueError
# ---------------------------------------------------------------------------


class TestStepSyntaxRaises:
    def test_star_slash_five(self) -> None:
        with pytest.raises(ValueError, match="step syntax"):
            cron_matches("*/5 * * * *", _utc(2024, 1, 1, 0, 0))

    def test_star_slash_in_hour(self) -> None:
        with pytest.raises(ValueError, match="step syntax"):
            cron_matches("0 */2 * * *", _utc(2024, 1, 1, 0, 0))

    def test_number_slash(self) -> None:
        with pytest.raises(ValueError, match="step syntax"):
            cron_matches("0/15 * * * *", _utc(2024, 1, 1, 0, 0))


# ---------------------------------------------------------------------------
# Range syntax raises ValueError
# ---------------------------------------------------------------------------


class TestRangeSyntaxRaises:
    def test_range_in_dow(self) -> None:
        with pytest.raises(ValueError, match="range syntax"):
            cron_matches("0 14 * * 1-5", _utc(2024, 1, 1, 14, 0))

    def test_range_in_minute(self) -> None:
        with pytest.raises(ValueError, match="range syntax"):
            cron_matches("0-5 14 * * *", _utc(2024, 1, 1, 14, 0))


# ---------------------------------------------------------------------------
# Malformed expression (wrong field count) raises ValueError
# ---------------------------------------------------------------------------


class TestMalformedExpression:
    def test_too_few_fields(self) -> None:
        with pytest.raises(ValueError, match="5 fields"):
            cron_matches("0 14 * *", _utc(2024, 1, 1, 14, 0))

    def test_too_many_fields(self) -> None:
        with pytest.raises(ValueError, match="5 fields"):
            cron_matches("0 14 * * * *", _utc(2024, 1, 1, 14, 0))

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="5 fields"):
            cron_matches("", _utc(2024, 1, 1, 14, 0))


# ---------------------------------------------------------------------------
# Named token raises ValueError
# ---------------------------------------------------------------------------


class TestNamedTokenRaises:
    def test_named_month(self) -> None:
        with pytest.raises(ValueError, match="named values"):
            cron_matches("0 14 * JAN *", _utc(2024, 1, 1, 14, 0))

    def test_named_dow(self) -> None:
        with pytest.raises(ValueError, match="named values"):
            cron_matches("0 14 * * MON", _utc(2024, 1, 1, 14, 0))


# ---------------------------------------------------------------------------
# Out-of-range value raises ValueError
# ---------------------------------------------------------------------------


class TestOutOfRange:
    def test_minute_60(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            cron_matches("60 14 * * *", _utc(2024, 1, 1, 14, 0))

    def test_hour_24(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            cron_matches("0 24 * * *", _utc(2024, 1, 1, 14, 0))

    def test_month_0(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            cron_matches("0 14 * 0 *", _utc(2024, 1, 1, 14, 0))

    def test_month_13(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            cron_matches("0 14 * 13 *", _utc(2024, 1, 1, 14, 0))


# ---------------------------------------------------------------------------
# Wildcard in every field
# ---------------------------------------------------------------------------


class TestAllWildcards:
    def test_all_wildcards_always_match(self) -> None:
        assert cron_matches("* * * * *", _utc(2024, 6, 15, 3, 27)) is True
        assert cron_matches("* * * * *", _utc(2024, 1, 1, 0, 0)) is True


# ---------------------------------------------------------------------------
# Day-of-week: Sunday can be 0 or 7
# ---------------------------------------------------------------------------


class TestDayOfWeek:
    def test_sunday_as_zero(self) -> None:
        # 2024-06-16 is a Sunday (isoweekday=7, cron dow=0).
        assert cron_matches("* * * * 0", _utc(2024, 6, 16, 0, 0)) is True

    def test_sunday_as_seven(self) -> None:
        # Both 0 and 7 should match Sunday.
        assert cron_matches("* * * * 7", _utc(2024, 6, 16, 0, 0)) is True

    def test_monday_as_one(self) -> None:
        # 2024-06-17 is a Monday (isoweekday=1, cron dow=1).
        assert cron_matches("* * * * 1", _utc(2024, 6, 17, 0, 0)) is True

    def test_monday_does_not_match_sunday(self) -> None:
        # Monday (2024-06-17) should NOT match dow=0 (Sunday).
        assert cron_matches("* * * * 0", _utc(2024, 6, 17, 0, 0)) is False

    def test_saturday_as_six(self) -> None:
        # 2024-06-15 is a Saturday (isoweekday=6, cron dow=6).
        assert cron_matches("* * * * 6", _utc(2024, 6, 15, 0, 0)) is True
