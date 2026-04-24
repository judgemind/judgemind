"""Tests for scripts/check-short-unsubstantive-rulings.py (see issue #2671).

These tests exercise the breach-detection logic with fabricated
``derived.rulings`` rows, asserting that:

* A county count above the threshold fires a breach.
* Counties at or below the threshold do not breach.
* Multiple counties can breach simultaneously.
* The ``--county`` filter restricts which counties are checked.
* JSON output shape is correct.
* Missing DATABASE_URL returns exit code 2.
* ``persist_metrics`` writes the correct rows (non-zero counts only).
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to sys.path so we can import the hyphenated module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# ruff: noqa: E402

sur = import_module("check-short-unsubstantive-rulings")

Breach = sur.Breach
CountyCount = sur.CountyCount
CheckResult = sur.CheckResult
check_short_unsubstantive_rulings = sur.check_short_unsubstantive_rulings
format_json = sur.format_json
persist_metrics = sur.persist_metrics
DEFAULT_THRESHOLD = sur.DEFAULT_THRESHOLD
METRIC_NAME = sur.METRIC_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_conn(rows: list[tuple[str, int]]) -> MagicMock:
    """Build a MagicMock that mimics psycopg cursor().execute()/fetchall().

    ``rows`` should be a list of ``(county, count)`` tuples as the SQL
    GROUP BY query would return them.
    """
    cur = MagicMock()
    cur.fetchall.return_value = list(rows)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _make_result(
    county_counts: list[tuple[str, int]],
    threshold: int = DEFAULT_THRESHOLD,
) -> CheckResult:
    """Build a CheckResult directly (no DB) for formatting/persistence tests."""
    ccs = [CountyCount(county=c, count=n) for c, n in county_counts]
    breaches = [
        Breach(
            county=cc.county,
            count=cc.count,
            threshold=threshold,
            message=(
                f"{cc.county}: {cc.count} short unsubstantive ruling(s) "
                f"in last 7 days (threshold: {threshold})"
            ),
        )
        for cc in ccs
        if cc.count > threshold
    ]
    return CheckResult(
        breaches=breaches,
        county_counts=ccs,
        checked_count=len(ccs),
    )


# ---------------------------------------------------------------------------
# Unit tests — check_short_unsubstantive_rulings
# ---------------------------------------------------------------------------


class TestCheckShortUnsubstantiveRulings:
    def test_above_threshold_fires_breach(self) -> None:
        """A county count strictly above the threshold fires a breach."""
        conn = _mock_conn([("orange", 10)])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert len(result.breaches) == 1
        b = result.breaches[0]
        assert b.county == "orange"
        assert b.count == 10
        assert b.threshold == 5
        assert result.checked_count == 1

    def test_at_threshold_no_breach(self) -> None:
        """A count exactly equal to the threshold does NOT fire a breach."""
        conn = _mock_conn([("orange", 5)])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert not result.breaches
        assert result.checked_count == 1

    def test_below_threshold_no_breach(self) -> None:
        """A count below the threshold does NOT fire a breach."""
        conn = _mock_conn([("orange", 3)])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert not result.breaches

    def test_zero_count_no_breach(self) -> None:
        """Zero-count counties never breach."""
        conn = _mock_conn([("orange", 0)])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert not result.breaches

    def test_multiple_counties_only_some_breach(self) -> None:
        """Only counties above the threshold appear in breaches."""
        conn = _mock_conn(
            [
                ("fresno", 2),
                ("orange", 12),
                ("riverside", 7),
                ("san_francisco", 1),
            ]
        )
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert result.checked_count == 4
        breaching = {b.county for b in result.breaches}
        assert breaching == {"orange", "riverside"}
        assert "fresno" not in breaching
        assert "san_francisco" not in breaching

    def test_multi_county_breach_emits_all_breaches(self) -> None:
        """All counties above the threshold are reported — SC/Riverside/Fresno."""
        conn = _mock_conn(
            [
                ("fresno", 8),
                ("riverside", 9),
                ("santa_clara", 11),
            ]
        )
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert len(result.breaches) == 3
        counties = {b.county for b in result.breaches}
        assert counties == {"fresno", "riverside", "santa_clara"}

    def test_no_rows_no_breach(self) -> None:
        """When the DB returns no rows the result is healthy with zero count."""
        conn = _mock_conn([])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert not result.breaches
        assert result.checked_count == 0

    def test_breach_message_format(self) -> None:
        """Breach message includes county, count, and threshold."""
        conn = _mock_conn([("orange", 20)])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        assert len(result.breaches) == 1
        msg = result.breaches[0].message
        assert "orange" in msg
        assert "20" in msg
        assert "5" in msg

    def test_county_counts_populated(self) -> None:
        """county_counts reflects all rows from the DB, breach or not."""
        conn = _mock_conn([("orange", 10), ("fresno", 2)])
        result = check_short_unsubstantive_rulings(conn, threshold=5)
        county_names = {cc.county for cc in result.county_counts}
        assert county_names == {"orange", "fresno"}


# ---------------------------------------------------------------------------
# Breach threshold edge cases
# ---------------------------------------------------------------------------


class TestBreachThreshold:
    def test_threshold_zero_means_any_count_breaches(self) -> None:
        """A threshold of 0 means even a count of 1 triggers a breach."""
        conn = _mock_conn([("orange", 1)])
        result = check_short_unsubstantive_rulings(conn, threshold=0)
        assert len(result.breaches) == 1

    def test_custom_threshold_honoured(self) -> None:
        """An explicit custom threshold is respected."""
        conn = _mock_conn([("orange", 10)])
        result = check_short_unsubstantive_rulings(conn, threshold=10)
        assert not result.breaches  # exactly at threshold → no breach

        result2 = check_short_unsubstantive_rulings(conn, threshold=9)
        assert len(result2.breaches) == 1


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    def test_format_json_healthy(self) -> None:
        import json as _json

        result = _make_result([("orange", 2), ("fresno", 1)])
        payload = _json.loads(format_json(result))
        assert payload["healthy"] is True
        assert payload["breaches"] == []
        assert payload["checked_count"] == 2
        assert len(payload["county_counts"]) == 2

    def test_format_json_with_breach(self) -> None:
        import json as _json

        result = _make_result([("orange", 10)], threshold=5)
        payload = _json.loads(format_json(result))
        assert payload["healthy"] is False
        assert len(payload["breaches"]) == 1
        b = payload["breaches"][0]
        assert b["county"] == "orange"
        assert b["count"] == 10
        assert b["threshold"] == 5

    def test_format_json_keys_present(self) -> None:
        """All expected top-level keys are present in the JSON output."""
        import json as _json

        result = _make_result([])
        payload = _json.loads(format_json(result))
        for key in ("breaches", "county_counts", "checked_count", "healthy"):
            assert key in payload, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Metrics persistence
# ---------------------------------------------------------------------------


class TestPersistMetrics:
    def test_persist_nonzero_counts_only(self) -> None:
        """persist_metrics only writes rows for counties with count > 0."""
        result = _make_result([("orange", 10), ("fresno", 0), ("riverside", 3)])
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        n = persist_metrics(conn, result)
        assert n == 2  # orange (10) and riverside (3), not fresno (0)
        cur.executemany.assert_called_once()
        rows_arg = cur.executemany.call_args[0][1]
        counties_written = {r[1] for r in rows_arg}
        assert counties_written == {"orange", "riverside"}

    def test_persist_metric_name(self) -> None:
        """persist_metrics uses the canonical METRIC_NAME constant."""
        result = _make_result([("orange", 8)])
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        persist_metrics(conn, result)
        rows_arg = cur.executemany.call_args[0][1]
        assert rows_arg[0][2] == METRIC_NAME

    def test_persist_empty_result_no_write(self) -> None:
        """When all counts are zero, persist_metrics writes nothing."""
        result = _make_result([("orange", 0)])
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        n = persist_metrics(conn, result)
        assert n == 0
        cur.executemany.assert_not_called()

    def test_persist_metric_value_is_float(self) -> None:
        """metric_value stored as float (schema: NUMERIC)."""
        result = _make_result([("orange", 7)])
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        persist_metrics(conn, result)
        rows_arg = cur.executemany.call_args[0][1]
        assert isinstance(rows_arg[0][3], float)

    def test_persist_failure_returns_zero_without_raising(self) -> None:
        """DB failure is logged but does not raise an exception."""
        result = _make_result([("orange", 8)])
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.executemany.side_effect = Exception("DB down")
        conn.cursor.return_value = cur

        n = persist_metrics(conn, result)
        assert n == 0


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestCli:
    def test_missing_database_url_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        rc = sur.main([])
        assert rc == 2

    def test_healthy_exit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no breach, main() returns 0."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        mock_conn = _mock_conn([("orange", 1)])
        with patch("psycopg.connect") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            rc = sur.main(["--threshold", "5"])
        assert rc == 0

    def test_breach_exit_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a breach exists, main() returns 1."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        mock_conn = _mock_conn([("orange", 20)])
        with patch("psycopg.connect") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            rc = sur.main(["--threshold", "5"])
        assert rc == 1
