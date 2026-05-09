"""Tests for scripts/check-scraper-zero-record-streak.py (see issue #2666).

These tests exercise the streak-counting logic with fabricated
``scraper_runs`` rows, asserting that:

* N consecutive zero-record runs fires a breach.
* Scrapers with no historical nonzero run are reported as
  insufficient-history, NOT as breaches (brand-new scraper false-positive
  guard).
* A recent nonzero run breaks the streak even if older runs are zero.
* Excluded scraper IDs are skipped entirely.
* The daily-vs-frequent threshold is applied per scraper.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add scripts/ to sys.path so we can import the hyphenated module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# ruff: noqa: E402

zrs = import_module("check-scraper-zero-record-streak")

Breach = zrs.Breach
InsufficientHistory = zrs.InsufficientHistory
CheckResult = zrs.CheckResult
compute_streak = zrs.compute_streak
has_historical_nonzero = zrs.has_historical_nonzero
check_zero_record_streaks = zrs.check_zero_record_streaks
format_json = zrs.format_json
format_text = zrs.format_text
DEFAULT_STREAK_THRESHOLD = zrs.DEFAULT_STREAK_THRESHOLD
DEFAULT_DAILY_STREAK_THRESHOLD = zrs.DEFAULT_DAILY_STREAK_THRESHOLD

NOW = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Unit tests — compute_streak / has_historical_nonzero
# ---------------------------------------------------------------------------


def _mk_run(hours_ago: int, records: int, status: str = "success") -> tuple[datetime, int, str]:
    """Helper: make a (started_at, records, status) tuple ``hours_ago`` behind NOW."""
    return (NOW - timedelta(hours=hours_ago), records, status)


class TestComputeStreak:
    def test_all_zero_runs_full_streak(self) -> None:
        """Every run has zero records — streak equals row count."""
        runs = [_mk_run(1, 0), _mk_run(25, 0), _mk_run(49, 0)]
        streak, first, last, last_nonzero = compute_streak(runs)
        assert streak == 3
        assert first == runs[-1][0]
        assert last == runs[0][0]
        assert last_nonzero is None

    def test_leading_zero_then_nonzero(self) -> None:
        """Streak is only the leading consecutive-zero prefix."""
        runs = [_mk_run(1, 0), _mk_run(25, 0), _mk_run(49, 5), _mk_run(73, 0)]
        streak, first, last, last_nonzero = compute_streak(runs)
        assert streak == 2
        # last_nonzero is the first (newest) nonzero row after the streak
        assert last_nonzero == runs[2][0]

    def test_most_recent_nonzero_no_streak(self) -> None:
        """Newest run has records — streak is 0, even with old zeros."""
        runs = [_mk_run(1, 7), _mk_run(25, 0), _mk_run(49, 0)]
        streak, first, last, last_nonzero = compute_streak(runs)
        assert streak == 0
        assert first is None
        assert last is None
        assert last_nonzero == runs[0][0]

    def test_empty_runs(self) -> None:
        streak, first, last, last_nonzero = compute_streak([])
        assert streak == 0
        assert first is None and last is None and last_nonzero is None

    def test_single_zero_run(self) -> None:
        runs = [_mk_run(1, 0)]
        streak, first, last, last_nonzero = compute_streak(runs)
        assert streak == 1
        assert first == runs[0][0]
        assert last == runs[0][0]
        assert last_nonzero is None


class TestHasHistoricalNonzero:
    def test_true_when_any_nonzero_present(self) -> None:
        runs = [_mk_run(1, 0), _mk_run(25, 3)]
        assert has_historical_nonzero(runs) is True

    def test_false_when_all_zero(self) -> None:
        runs = [_mk_run(1, 0), _mk_run(25, 0)]
        assert has_historical_nonzero(runs) is False

    def test_false_on_empty(self) -> None:
        assert has_historical_nonzero([]) is False


# ---------------------------------------------------------------------------
# Integration tests — check_zero_record_streaks against a fake DB
# ---------------------------------------------------------------------------


def _mock_conn(rows: list[tuple[str, datetime, int, str]]) -> MagicMock:
    """Build a MagicMock that mimics psycopg's cursor().execute()/fetchall().

    ``rows`` should be a flat list of ``(scraper_id, started_at, records_captured, status)``
    tuples in the order the SQL query would return them (grouped by scraper_id,
    started_at DESC within each group).
    """
    cur = MagicMock()
    cur.fetchall.return_value = list(rows)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


class TestCheckZeroRecordStreaks:
    def test_three_consecutive_zeros_frequent_scraper_breaches(self) -> None:
        """3 zero-record runs for a frequent scraper with history = breach."""
        rows = [
            ("ca-sf-tentatives-civil", NOW - timedelta(hours=1), 0, "success"),
            ("ca-sf-tentatives-civil", NOW - timedelta(hours=7), 0, "success"),
            ("ca-sf-tentatives-civil", NOW - timedelta(hours=13), 0, "success"),
            ("ca-sf-tentatives-civil", NOW - timedelta(hours=24), 4, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids={"ca-sf-tentatives-civil"},
            exclusions=set(),
        )
        assert len(result.breaches) == 1
        b = result.breaches[0]
        assert b.scraper_id == "ca-sf-tentatives-civil"
        assert b.streak == 3
        assert b.threshold == 3
        assert b.last_nonzero_at is not None
        assert result.checked_count == 1
        assert result.excluded_count == 0
        assert not result.insufficient_history

    def test_two_consecutive_zeros_daily_scraper_breaches(self) -> None:
        """Daily scraper breaches at 2 consecutive zero-record runs."""
        rows = [
            ("ca-oc-tentatives", NOW - timedelta(hours=2), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=26), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=50), 18, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
        )
        assert len(result.breaches) == 1
        assert result.breaches[0].threshold == 2

    def test_brand_new_scraper_with_zeros_is_insufficient_history(self) -> None:
        """No historical nonzero run = insufficient_history, NOT breach.

        Pass ``known_scraper_ids=None`` to disable the unknown-scraper
        partition so a fictional scraper ID can exercise the
        insufficient-history path without being filtered out first.
        """
        rows = [
            ("ca-new-scraper", NOW - timedelta(hours=1), 0, "success"),
            ("ca-new-scraper", NOW - timedelta(hours=25), 0, "success"),
            ("ca-new-scraper", NOW - timedelta(hours=49), 0, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
            known_scraper_ids=None,  # disable unknown-ID check for this unit test
        )
        assert not result.breaches
        assert len(result.insufficient_history) == 1
        ih = result.insufficient_history[0]
        assert ih.scraper_id == "ca-new-scraper"
        assert ih.zero_run_count == 3

    def test_recent_nonzero_run_prevents_breach(self) -> None:
        """Even with older zeros, a fresh nonzero breaks the streak."""
        rows = [
            ("ca-la-tentatives-civil", NOW - timedelta(hours=1), 42, "success"),
            ("ca-la-tentatives-civil", NOW - timedelta(hours=25), 0, "success"),
            ("ca-la-tentatives-civil", NOW - timedelta(hours=49), 0, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
        )
        assert not result.breaches
        assert not result.insufficient_history
        assert result.checked_count == 1

    def test_excluded_scrapers_are_skipped(self) -> None:
        """Scrapers in the exclusion set are not checked at all."""
        rows = [
            ("ca-oc-dept-judges", NOW - timedelta(hours=1), 0, "success"),
            ("ca-oc-dept-judges", NOW - timedelta(hours=25), 0, "success"),
            ("ca-oc-dept-judges", NOW - timedelta(hours=49), 0, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions={"ca-oc-dept-judges"},
        )
        assert result.excluded_count == 1
        assert result.checked_count == 0
        assert not result.breaches
        assert not result.insufficient_history

    def test_multiple_scrapers_only_some_breach(self) -> None:
        """Healthy scrapers do not inflate the breach list."""
        rows = [
            # Healthy daily scraper
            ("ca-la-tentatives-civil", NOW - timedelta(hours=2), 42, "success"),
            ("ca-la-tentatives-civil", NOW - timedelta(hours=26), 38, "success"),
            # Breaching daily scraper
            ("ca-oc-tentatives", NOW - timedelta(hours=3), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=27), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=51), 18, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
        )
        assert result.checked_count == 2
        assert len(result.breaches) == 1
        assert result.breaches[0].scraper_id == "ca-oc-tentatives"

    def test_single_zero_run_no_breach_daily(self) -> None:
        """A single zero-record run does not breach even a daily scraper."""
        rows = [
            ("ca-oc-tentatives", NOW - timedelta(hours=2), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=26), 18, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
        )
        assert not result.breaches


# ---------------------------------------------------------------------------
# Module-level EXCLUSIONS regression tests
# ---------------------------------------------------------------------------


class TestModuleExclusions:
    """Lock the contents of the module-level ``EXCLUSIONS`` set.

    Each entry in ``EXCLUSIONS`` permanently silences the silent-outage
    guard for one scraper. Adding an entry without a tracked root-cause
    follow-up is the failure mode this test class guards against — the
    streak alert was originally added precisely so silently-zero scrapers
    could not ride along undetected. See #4531 / #4539.
    """

    def test_excludes_dept_judges_rosters(self) -> None:
        """Court directory / roster scrapers are zero-by-design — see EXCLUSIONS docstring."""
        for sid in (
            "ca-oc-dept-judges",
            "ca-la-dept-judges",
            "ca-fresno-dept-judges",
            "ca-kern-dept-judges",
            "ca-sd-dept-judges",
            "ca-sb-dept-judges",
            "ca-ventura-dept-judges",
            "ca-sf-dept-judges",
            "ca-riverside-dept-judges",
        ):
            assert sid in zrs.EXCLUSIONS, f"{sid} should remain in EXCLUSIONS"

    def test_excludes_governor_appointments(self) -> None:
        """Quarterly cadence — most runs legitimately return zero."""
        assert "ca-governor-appointments" in zrs.EXCLUSIONS

    def test_does_not_exclude_ca_sd_calendar_after_4539(self) -> None:
        """ca-sd-calendar must NOT be in EXCLUSIONS now that #4539 has
        shipped. The structural fix flipped the ``day_numbers`` default
        from ``[2]`` to ``[1, 2, 3, 4, 5]`` so the Friday-clustered
        motion calendar is captured on every run day, restoring the
        silent-outage guard for this scraper. Re-adding ``ca-sd-calendar``
        to EXCLUSIONS without a fresh tracked root-cause follow-up is
        the failure mode this test guards against. See #4531 for the
        originating alert and #4539 for the structural fix.
        """
        assert "ca-sd-calendar" not in zrs.EXCLUSIONS, (
            "ca-sd-calendar must NOT be in EXCLUSIONS after #4539 — the "
            "structural day_numbers fix shipped and the silent-outage "
            "guard must remain active for this scraper"
        )


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    def test_format_json_healthy(self) -> None:
        result = CheckResult(
            breaches=[],
            insufficient_history=[],
            checked_count=5,
            excluded_count=2,
        )
        import json as _json

        payload = _json.loads(format_json(result))
        assert payload["healthy"] is True
        assert payload["breaches"] == []
        assert payload["checked_count"] == 5
        assert payload["excluded_count"] == 2

    def test_format_json_with_breach(self) -> None:
        result = CheckResult(
            breaches=[
                Breach(
                    scraper_id="ca-sf-tentatives-civil",
                    streak=3,
                    threshold=3,
                    first_zero_at="2026-04-16T12:00:00+00:00",
                    last_zero_at="2026-04-18T00:00:00+00:00",
                    last_nonzero_at="2026-04-15T12:00:00+00:00",
                    message="breach message",
                ),
            ],
            insufficient_history=[],
            checked_count=1,
            excluded_count=0,
        )
        import json as _json

        payload = _json.loads(format_json(result))
        assert payload["healthy"] is False
        assert len(payload["breaches"]) == 1
        assert payload["breaches"][0]["scraper_id"] == "ca-sf-tentatives-civil"

    def test_format_text_lists_breaches(self) -> None:
        result = CheckResult(
            breaches=[
                Breach(
                    scraper_id="ca-sf-tentatives-civil",
                    streak=3,
                    threshold=3,
                    first_zero_at="2026-04-16T12:00:00+00:00",
                    last_zero_at="2026-04-18T00:00:00+00:00",
                    last_nonzero_at=None,
                    message="ca-sf-tentatives-civil: 3 consecutive zero-record runs",
                ),
            ],
            insufficient_history=[
                InsufficientHistory(
                    scraper_id="ca-new-scraper",
                    zero_run_count=3,
                    message="ca-new-scraper: insufficient history",
                ),
            ],
            checked_count=2,
            excluded_count=1,
        )
        text = format_text(result)
        assert "ca-sf-tentatives-civil" in text
        assert "BREACHES" in text
        assert "INSUFFICIENT HISTORY" in text
        assert "(none)" in text


# ---------------------------------------------------------------------------
# Unknown scraper ID (fixture-pollution) warning tests
# ---------------------------------------------------------------------------


class TestUnknownScraperIdWarning:
    """Verify that scraper IDs absent from the registry are routed to the
    ``unknown_scrapers`` warning category and skipped in the streak check."""

    def test_unknown_id_appears_in_unknown_scrapers(self) -> None:
        """An ID not in ``known_scraper_ids`` must end up in
        ``CheckResult.unknown_scrapers`` and NOT in breaches or
        insufficient_history."""
        rows = [
            ("test-stub", NOW - timedelta(hours=1), 0, "success"),
            ("test-stub", NOW - timedelta(hours=25), 0, "success"),
            # No historical nonzero run — would be insufficient_history if
            # it were a known scraper; it should be unknown instead.
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
            known_scraper_ids={"ca-la-tentatives-civil"},  # test-stub is NOT here
        )
        assert result.unknown_scrapers == ["test-stub"]
        assert result.checked_count == 0
        assert result.excluded_count == 0
        assert not result.breaches
        assert not result.insufficient_history

    def test_unknown_ids_are_not_streak_checked(self) -> None:
        """Unknown scrapers with a breach-length streak must NOT appear in
        breaches — they are suppressed as potential test pollution."""
        rows = [
            # Unknown scraper with a long zero-record streak
            ("good", NOW - timedelta(hours=1), 0, "success"),
            ("good", NOW - timedelta(hours=25), 0, "success"),
            ("good", NOW - timedelta(hours=49), 5, "success"),
            # Known scraper, also with a streak — this one SHOULD breach
            ("ca-oc-tentatives", NOW - timedelta(hours=2), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=26), 0, "success"),
            ("ca-oc-tentatives", NOW - timedelta(hours=50), 18, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
            known_scraper_ids={"ca-oc-tentatives"},  # "good" is NOT here
        )
        assert "good" in result.unknown_scrapers
        # The known scraper breaches normally
        assert len(result.breaches) == 1
        assert result.breaches[0].scraper_id == "ca-oc-tentatives"
        assert result.checked_count == 1

    def test_multiple_unknown_ids_all_reported(self) -> None:
        """All unknown scraper IDs are collected in ``unknown_scrapers``, sorted."""
        rows = [
            ("failing", NOW - timedelta(hours=1), 0, "success"),
            ("run-raiser", NOW - timedelta(hours=2), 0, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
            known_scraper_ids=set(),  # no known scrapers → both are unknown
        )
        assert sorted(result.unknown_scrapers) == ["failing", "run-raiser"]
        assert result.checked_count == 0
        assert not result.breaches

    def test_none_known_scrapers_disables_partition(self) -> None:
        """Passing ``known_scraper_ids=None`` disables the unknown-ID check:
        all fetched IDs are treated as known and streak-checked normally."""
        rows = [
            ("test-stub", NOW - timedelta(hours=1), 0, "success"),
            ("test-stub", NOW - timedelta(hours=25), 0, "success"),
            ("test-stub", NOW - timedelta(hours=49), 3, "success"),
        ]
        conn = _mock_conn(rows)
        result = check_zero_record_streaks(
            conn,
            now=NOW,
            threshold=3,
            daily_threshold=2,
            frequent_scraper_ids=set(),
            exclusions=set(),
            known_scraper_ids=None,  # disabled — treat everything as known
        )
        # The scraper has streak=2 (daily threshold=2) → breach
        assert len(result.breaches) == 1
        assert result.breaches[0].scraper_id == "test-stub"
        assert result.unknown_scrapers == []

    def test_format_text_includes_unknown_warning(self) -> None:
        """``format_text`` lists unknown scrapers in a WARNING block."""
        result = CheckResult(
            breaches=[],
            insufficient_history=[],
            checked_count=0,
            excluded_count=0,
            unknown_scrapers=["test-stub", "good"],
        )
        text = format_text(result)
        assert "WARNING" in text
        assert "test-stub" in text
        assert "good" in text
        assert "test pollution" in text

    def test_format_json_includes_unknown_scrapers_key(self) -> None:
        """``format_json`` includes an ``unknown_scrapers`` array."""
        import json as _json

        result = CheckResult(
            breaches=[],
            insufficient_history=[],
            checked_count=0,
            excluded_count=0,
            unknown_scrapers=["test-stub"],
        )
        payload = _json.loads(format_json(result))
        assert "unknown_scrapers" in payload
        assert payload["unknown_scrapers"] == ["test-stub"]


# ---------------------------------------------------------------------------
# Sentinel typing regression tests
# ---------------------------------------------------------------------------


class TestSentinelTyping:
    """Regression lock — ensure _SENTINEL is a dedicated type, not a fake set."""

    def test_sentinel_is_not_a_set(self) -> None:
        """_SENTINEL must not be typed as a set; the dishonest annotation is gone."""
        assert not isinstance(zrs._SENTINEL, set)

    def test_sentinel_is_not_none(self) -> None:
        """_SENTINEL must not be None — preserves the three-way sentinel/None/set distinction."""
        assert zrs._SENTINEL is not None


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestCli:
    def test_missing_database_url_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        rc = zrs.main([])
        assert rc == 2
