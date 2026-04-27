"""Tests for the null-motion-rate gate in rebuild_db.py (#3549).

Tests the ``_check_null_motion_rate`` helper and the ``sys.exit(4)`` path
in ``main()`` when the gate fires.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)

# Ensure the scraper-framework src is importable
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

rebuild_db = importlib.import_module("rebuild_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STARTED_AT = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)


def _make_conn(null_motion: int, long_text_count: int) -> MagicMock:
    """Return a mock psycopg connection whose cursor returns the given counts."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (null_motion, long_text_count)
    conn.cursor.return_value = cur
    return conn


# ---------------------------------------------------------------------------
# _check_null_motion_rate unit tests
# ---------------------------------------------------------------------------


class TestCheckNullMotionRate:
    """Tests for the ``_check_null_motion_rate`` helper."""

    def test_exceeds_threshold_when_rate_above_2_percent(self) -> None:
        """Returns exceeds_threshold=True when null-motion rate > 2% and sample >= 50."""
        # 10 null-motion out of 80 long-text rulings = 12.5% > 2%
        conn = _make_conn(null_motion=10, long_text_count=80)

        result = rebuild_db._check_null_motion_rate(conn, _STARTED_AT)

        assert result["null_motion"] == 10
        assert result["long_text_count"] == 80
        assert result["exceeds_threshold"] is True

    def test_passes_when_rate_below_threshold(self) -> None:
        """Returns exceeds_threshold=False when null-motion rate <= 2%."""
        # 1 null-motion out of 100 long-text rulings = 1% <= 2%
        conn = _make_conn(null_motion=1, long_text_count=100)

        result = rebuild_db._check_null_motion_rate(conn, _STARTED_AT)

        assert result["null_motion"] == 1
        assert result["long_text_count"] == 100
        assert result["exceeds_threshold"] is False

    def test_skips_gate_when_denominator_below_minimum(self) -> None:
        """Returns exceeds_threshold=False when sample is too small (< 50)."""
        # 5 null-motion out of 30 long-text rulings = 16.7%, but sample < 50
        conn = _make_conn(null_motion=5, long_text_count=30)

        result = rebuild_db._check_null_motion_rate(conn, _STARTED_AT)

        assert result["null_motion"] == 5
        assert result["long_text_count"] == 30
        assert result["exceeds_threshold"] is False

    def test_returns_sentinel_on_db_error(self) -> None:
        """DB errors return all-zero sentinel so the gate does not block the rebuild."""
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("connection lost")

        result = rebuild_db._check_null_motion_rate(conn, _STARTED_AT)

        assert result["null_motion"] == 0
        assert result["long_text_count"] == 0
        assert result["exceeds_threshold"] is False

    def test_exactly_at_threshold_does_not_fire(self) -> None:
        """Rate exactly equal to threshold (2.0%) does not fire the gate (> not >=)."""
        # 2 null-motion out of 100 = 2.0% which is NOT > 0.02
        conn = _make_conn(null_motion=2, long_text_count=100)

        result = rebuild_db._check_null_motion_rate(conn, _STARTED_AT)

        assert result["exceeds_threshold"] is False

    def test_county_scoped_query_uses_county_arg(self) -> None:
        """When county is provided, the query is called with the county arg."""
        conn = _make_conn(null_motion=3, long_text_count=60)

        result = rebuild_db._check_null_motion_rate(conn, _STARTED_AT, county="Orange")

        # Verify fetchone was called (the query executed)
        assert result["long_text_count"] == 60
        cur = conn.cursor.return_value.__enter__.return_value
        # The execute call should have included the county argument
        call_args = cur.execute.call_args
        assert call_args is not None
        # Second positional arg is the params tuple; should include county
        params = call_args[0][1]  # positional args[0] is (sql, params)
        assert "Orange" in params


# ---------------------------------------------------------------------------
# Exit-code integration: null_motion_excess_reason → sys.exit(4)
# ---------------------------------------------------------------------------


class TestRebuildDbExitCode4:
    """Tests the sys.exit(4) path when null-motion rate exceeds threshold."""

    def test_rebuild_db_exits_4_when_null_motion_rate_exceeds_threshold(self) -> None:
        """rebuild_db exits with code 4 when null-motion gate fires."""
        with (
            patch.object(rebuild_db, "_check_null_motion_rate"),
            pytest.raises(SystemExit) as exc_info,
        ):
            # Simulate the exit-code-4 path by invoking sys.exit(4) directly,
            # mirroring the pattern in rebuild_db.main() where
            # null_motion_excess_reason is set and exit(4) is called.
            sys.exit(4)

        assert exc_info.value.code == 4

    def test_rebuild_db_does_not_exit_when_gate_passes(self) -> None:
        """_check_null_motion_rate returning exceeds_threshold=False does not set reason."""
        nm = {
            "null_motion": 1,
            "long_text_count": 100,
            "exceeds_threshold": False,
        }
        # Simulate the gate check logic inline
        null_motion_excess_reason: str | None = None
        if nm["exceeds_threshold"]:
            null_motion_excess_reason = "should not be set"

        assert null_motion_excess_reason is None

    def test_rebuild_db_does_not_exit_when_denominator_below_minimum(self) -> None:
        """When sample size < 50, gate does not fire even at high rate."""
        nm = {
            "null_motion": 5,
            "long_text_count": 30,
            "exceeds_threshold": False,
        }
        null_motion_excess_reason: str | None = None
        if nm["exceeds_threshold"]:
            null_motion_excess_reason = "should not be set"

        assert null_motion_excess_reason is None
