"""Integration tests for scripts/cc-dual-run-diff.py.

Tests cover:
  - CLI argparse: date, json flag, exit-code semantics
  - JSON output shape
  - Exit-code 0 when no missing_portal rows, 1 when any exist
  - DB write: verify telemetry.data_quality_metrics insert (AC#5)

The script is imported via importlib (same pattern as
test_check_scraper_zero_record_streak.py) so the hyphenated filename works.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the hyphenated module
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

ccdiff = import_module("cc-dual-run-diff")

# Grab the public symbols we test
_parse_args = ccdiff._parse_args
main = ccdiff.main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_DATE = date(2026, 4, 15)
TARGET_DATE_STR = "2026-04-15"

# DB query returns: (scraper_id, s3_key, case_number, hearing_date, dept_from_db)
# For the retired scraper, dept_from_db is populated; s3_key is unused for dept.
# For the portal scraper, dept_from_db is None; dept is derived from s3_key.
_RETIRED_S3_KEY = "ca/contra_costa/superior/tentative/C24-01001.pdf"
_PORTAL_S3_KEY_DEPT16 = "/system/files/general/16_041526.pdf"
_PORTAL_S3_KEY_DEPT30 = "/system/files/general/30_041526.pdf"


def _row_retired(
    dept: str,
    case_number: str,
    hearing_date: date = TARGET_DATE,
    s3_key: str = _RETIRED_S3_KEY,
) -> tuple:
    """Build a DB row tuple for the retired scraper (dept from dept_from_db)."""
    return ("ca-cc-tentatives", s3_key, case_number, hearing_date, dept)


def _row_portal(
    s3_key: str,
    case_number: str,
    hearing_date: date = TARGET_DATE,
) -> tuple:
    """Build a DB row tuple for the portal scraper (dept from s3_key filename)."""
    return ("ca-cc-tentatives-portal", s3_key, case_number, hearing_date, None)


def _make_cursor(rows: list[tuple]) -> MagicMock:
    """Build a mock psycopg cursor that returns ``rows`` from fetchall()."""
    cur = MagicMock()
    cur.fetchall.return_value = list(rows)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _make_conn(rows: list[tuple]) -> MagicMock:
    """Build a mock psycopg connection backed by ``rows``."""
    cur = _make_cursor(rows)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# 1. CLI argparse
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_date_required(self) -> None:
        """--date is required; missing it raises SystemExit."""
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_date_parsed(self) -> None:
        args = _parse_args(["--date", TARGET_DATE_STR])
        assert args.date == TARGET_DATE

    def test_json_flag_default(self) -> None:
        args = _parse_args(["--date", TARGET_DATE_STR])
        # Default output is JSON
        assert not args.text

    def test_text_flag(self) -> None:
        args = _parse_args(["--date", TARGET_DATE_STR, "--text"])
        assert args.text is True

    def test_invalid_date_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--date", "not-a-date"])


# ---------------------------------------------------------------------------
# 2. JSON output shape
# ---------------------------------------------------------------------------


class TestJsonOutputShape:
    def test_output_is_valid_json_with_expected_keys(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main() emits valid JSON with expected top-level keys."""
        # DB returns no rows (no captures today) — diff is empty
        conn = _make_conn([])

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert "diff" in payload
        assert "healthy" in payload
        assert "date" in payload
        assert payload["date"] == TARGET_DATE_STR
        assert exit_code == 0

    def test_diff_rows_have_expected_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Each diff row in the JSON output has the correct keys."""
        # One retired-scraper capture in dept 16; portal has nothing
        rows = [
            _row_retired("16", "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert len(payload["diff"]) == 1
        row = payload["diff"][0]
        assert "department" in row
        assert "count_a" in row
        assert "count_b" in row
        assert "cases_only_a" in row
        assert "cases_only_b" in row
        assert "flag" in row
        assert exit_code == 1  # missing_portal → exit 1


# ---------------------------------------------------------------------------
# 3. Exit-code semantics
# ---------------------------------------------------------------------------


class TestExitCodeSemantics:
    def test_exit_0_when_no_breach(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Exit 0 when portal matches retired scraper for all departments."""
        rows = [
            _row_retired("16", "C24-01001"),
            _row_portal(_PORTAL_S3_KEY_DEPT16, "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        assert exit_code == 0

    def test_exit_1_when_missing_portal_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Exit 1 when at least one missing_portal row in the diff."""
        # Retired scraper has dept 16; portal has nothing
        rows = [
            _row_retired("16", "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        assert exit_code == 1

    def test_exit_2_when_no_database_url(self) -> None:
        """Exit 2 when DATABASE_URL is not set."""
        with patch.dict("os.environ", {}, clear=True):
            exit_code = main(["--date", TARGET_DATE_STR])
        assert exit_code == 2


# ---------------------------------------------------------------------------
# 4. DB write — AC#5
# ---------------------------------------------------------------------------


class TestWritesDataQualityMetricsRow:
    def test_inserts_one_row_to_data_quality_metrics(self) -> None:
        """main() inserts exactly one row into telemetry.data_quality_metrics.

        Verifies:
          - metric_name = 'cc_dual_run_diff'
          - county = 'contra_costa'
          - metadata JSONB contains the diff
        """
        rows = [
            _row_retired("16", "C24-01001"),
            _row_portal(_PORTAL_S3_KEY_DEPT16, "C24-01001"),
        ]
        cur = _make_cursor(rows)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            main(["--date", TARGET_DATE_STR])

        # Collect all execute() calls on the cursor
        execute_calls = [c for c in cur.execute.call_args_list]

        # At least one call should insert into data_quality_metrics
        insert_calls = [
            c
            for c in execute_calls
            if "data_quality_metrics" in str(c).lower() and "INSERT" in str(c).upper()
        ]
        assert len(insert_calls) >= 1, "Expected at least one INSERT into data_quality_metrics"

        # Verify the parameters of the insert call
        insert_call = insert_calls[0]
        params = (
            insert_call.args[1]
            if len(insert_call.args) > 1
            else insert_call.kwargs.get("params", ())
        )

        # county and metric_name should appear in the params
        param_strs = [str(p) for p in params]
        assert any("contra_costa" in s for s in param_strs), (
            f"Expected 'contra_costa' in insert params, got: {params}"
        )
        assert any("cc_dual_run_diff" in s for s in param_strs), (
            f"Expected 'cc_dual_run_diff' in insert params, got: {params}"
        )


# ---------------------------------------------------------------------------
# 5. Total-coverage signal — AC#1
# ---------------------------------------------------------------------------


class TestTotalCoverageSignal:
    def test_json_payload_carries_total_coverage_keys(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When portal captured nothing, the JSON payload includes the
        total-coverage signal keys with portal_total_zero=true and a headline.
        """
        # Retired scraper has dept 16; portal has nothing → total-coverage failure
        rows = [
            _row_retired("16", "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["portal_total_zero"] is True
        assert payload["retired_total"] == 1
        assert payload["portal_total"] == 0
        assert "total-coverage failure" in payload["total_coverage_headline"]
        # Exit-code semantics unchanged: missing_portal → exit 1
        assert exit_code == 1

    def test_json_payload_total_coverage_keys_false_on_partial(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Partial coverage — portal_total_zero is false, headline empty."""
        rows = [
            _row_retired("16", "C24-01001"),
            _row_retired("30", "N25-0001"),
            _row_portal(_PORTAL_S3_KEY_DEPT30, "N25-0001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            main(["--date", TARGET_DATE_STR])

        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["portal_total_zero"] is False
        assert payload["retired_total"] == 2
        assert payload["portal_total"] == 1
        assert payload["total_coverage_headline"] == ""

    def test_metrics_metadata_carries_total_coverage_keys(self) -> None:
        """The metrics-row metadata JSON carries the total-coverage signal keys."""
        rows = [
            _row_retired("16", "C24-01001"),
        ]
        cur = _make_cursor(rows)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            main(["--date", TARGET_DATE_STR])

        insert_calls = [
            c
            for c in cur.execute.call_args_list
            if "data_quality_metrics" in str(c).lower() and "INSERT" in str(c).upper()
        ]
        assert len(insert_calls) >= 1
        params = insert_calls[0].args[1]
        # metadata is the last param — JSON string
        metadata = json.loads(params[-1])
        assert metadata["portal_total_zero"] is True
        assert metadata["retired_total"] == 1
        assert metadata["portal_total"] == 0
        assert "total-coverage failure" in metadata["total_coverage_headline"]


# ---------------------------------------------------------------------------
# 6. agent_actionable classification — #4631
# ---------------------------------------------------------------------------


class TestAgentActionable:
    """A total-coverage failure (portal captured nothing) is NOT agent-actionable
    — it is the expected "portal not yet authoritative" Phase-2 signal, which no
    agent code change can fix. A per-department gap (portal live but missing a
    department) IS a genuine regression and stays agent-actionable. See #4631.
    """

    def test_total_coverage_failure_is_not_agent_actionable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Retired has dept 16; portal captured nothing → total-coverage failure.
        rows = [
            _row_retired("16", "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        payload = json.loads(capsys.readouterr().out)
        assert payload["portal_total_zero"] is True
        assert payload["agent_actionable"] is False
        # Exit-code semantics unchanged: a missing_portal row still exits 1.
        assert exit_code == 1

    def test_partial_department_gap_is_agent_actionable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Portal is live (captured dept 16) but missing dept 30 → real regression.
        rows = [
            _row_retired("16", "C24-01001"),
            _row_retired("30", "N25-0001"),
            _row_portal(_PORTAL_S3_KEY_DEPT16, "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        payload = json.loads(capsys.readouterr().out)
        assert payload["portal_total_zero"] is False
        assert payload["missing_portal_count"] == 1
        assert payload["agent_actionable"] is True
        assert exit_code == 1

    def test_healthy_is_not_agent_actionable(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Both scrapers agree → healthy, nothing to action.
        rows = [
            _row_retired("16", "C24-01001"),
            _row_portal(_PORTAL_S3_KEY_DEPT16, "C24-01001"),
        ]
        conn = _make_conn(rows)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            exit_code = main(["--date", TARGET_DATE_STR])

        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is True
        assert payload["agent_actionable"] is False
        assert exit_code == 0

    def test_metrics_metadata_carries_agent_actionable(self) -> None:
        rows = [
            _row_retired("16", "C24-01001"),
        ]
        cur = _make_cursor(rows)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake"}),
            patch("psycopg.connect", return_value=conn),
        ):
            main(["--date", TARGET_DATE_STR])

        insert_calls = [
            c
            for c in cur.execute.call_args_list
            if "data_quality_metrics" in str(c).lower() and "INSERT" in str(c).upper()
        ]
        assert len(insert_calls) >= 1
        metadata = json.loads(insert_calls[0].args[1][-1])
        assert metadata["agent_actionable"] is False
