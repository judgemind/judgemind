"""Tests for the dev-db-query-runner module.

Tests cover the formatting and decoding helpers that can be unit-tested
without a real database connection.  The ``run_query`` function is tested
with psycopg mocked out.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the scripts directory to the path so we can import the runner module.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# We need to provide a mock psycopg module before importing the runner,
# because the runner uses a lazy import.  We import the module-level
# helpers directly — they don't touch psycopg.
from dev_db_query_runner import (  # noqa: E402
    decode_query,
    format_non_select_result,
    format_select_results,
)


class TestDecodeQuery:
    """Tests for decode_query()."""

    def test_decodes_simple_query(self) -> None:
        query = "SELECT COUNT(*) FROM rulings"
        encoded = base64.b64encode(query.encode()).decode()
        assert decode_query(encoded) == query

    def test_decodes_query_with_quotes(self) -> None:
        query = "SELECT * FROM courts WHERE state = 'CA'"
        encoded = base64.b64encode(query.encode()).decode()
        assert decode_query(encoded) == query

    def test_decodes_unicode_query(self) -> None:
        query = "SELECT * FROM rulings WHERE title LIKE '%José%'"
        encoded = base64.b64encode(query.encode()).decode()
        assert decode_query(encoded) == query

    def test_decodes_multiline_query(self) -> None:
        query = "SELECT *\nFROM rulings\nWHERE id = 1"
        encoded = base64.b64encode(query.encode()).decode()
        assert decode_query(encoded) == query


class TestFormatSelectResults:
    """Tests for format_select_results()."""

    def test_formats_single_row(self) -> None:
        description = [("id",), ("name",)]
        rows = [(1, "foo")]
        result = json.loads(format_select_results(description, rows))
        assert result == [{"id": 1, "name": "foo"}]

    def test_formats_multiple_rows(self) -> None:
        description = [("id",), ("name",)]
        rows = [(1, "foo"), (2, "bar")]
        result = json.loads(format_select_results(description, rows))
        assert result == [{"id": 1, "name": "foo"}, {"id": 2, "name": "bar"}]

    def test_formats_empty_result_set(self) -> None:
        description = [("id",), ("name",)]
        rows: list[tuple[object, ...]] = []
        result = json.loads(format_select_results(description, rows))
        assert result == []

    def test_uses_default_str_for_non_serializable(self) -> None:
        """Dates, decimals, etc. should be serialised via str()."""
        from datetime import date

        description = [("created",)]
        rows = [(date(2025, 1, 15),)]
        result = json.loads(format_select_results(description, rows))
        assert result == [{"created": "2025-01-15"}]

    def test_handles_none_values(self) -> None:
        description = [("id",), ("name",)]
        rows = [(1, None)]
        result = json.loads(format_select_results(description, rows))
        assert result == [{"id": 1, "name": None}]

    def test_output_is_indented(self) -> None:
        """The output should be pretty-printed with indent=2."""
        description = [("id",)]
        rows = [(1,)]
        raw = format_select_results(description, rows)
        assert "\n" in raw
        assert "  " in raw

    def test_handles_many_columns(self) -> None:
        description = [("a",), ("b",), ("c",), ("d",), ("e",)]
        rows = [(1, 2, 3, 4, 5)]
        result = json.loads(format_select_results(description, rows))
        assert result == [{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}]


class TestFormatNonSelectResult:
    """Tests for format_non_select_result()."""

    def test_formats_positive_rowcount(self) -> None:
        result = json.loads(format_non_select_result(3))
        assert result == {"rowcount": 3}

    def test_formats_zero_rowcount(self) -> None:
        result = json.loads(format_non_select_result(0))
        assert result == {"rowcount": 0}

    def test_formats_large_rowcount(self) -> None:
        result = json.loads(format_non_select_result(10000))
        assert result == {"rowcount": 10000}


class TestRunQuery:
    """Tests for run_query() with psycopg mocked."""

    def _make_mock_psycopg(
        self,
        *,
        description: list[tuple[str, ...]] | None = None,
        rows: list[tuple[object, ...]] | None = None,
        rowcount: int = 0,
    ) -> MagicMock:
        """Create a mock psycopg module with a mock connection and cursor."""
        mock_cursor = MagicMock()
        mock_cursor.description = description
        mock_cursor.rowcount = rowcount
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        if rows is not None:
            mock_cursor.fetchall.return_value = rows

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_psycopg.Error = Exception

        return mock_psycopg

    def test_select_query_prints_json_array(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        query = "SELECT id FROM rulings"
        query_b64 = base64.b64encode(query.encode()).decode()

        mock_psycopg = self._make_mock_psycopg(
            description=[("id",)],
            rows=[(1,), (2,)],
        )

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
                from dev_db_query_runner import run_query

                run_query(query_b64, "0")

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == [{"id": 1}, {"id": 2}]

    def test_non_select_query_prints_rowcount(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        query = "UPDATE rulings SET status = 'active'"
        query_b64 = base64.b64encode(query.encode()).decode()

        mock_psycopg = self._make_mock_psycopg(
            description=None,
            rowcount=5,
        )

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
                from dev_db_query_runner import run_query

                run_query(query_b64, "0")

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == {"rowcount": 5}

    def test_readonly_flag_sets_transaction_readonly(self) -> None:
        query = "SELECT 1"
        query_b64 = base64.b64encode(query.encode()).decode()

        mock_psycopg = self._make_mock_psycopg(
            description=[("?column?",)],
            rows=[(1,)],
        )

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
                from dev_db_query_runner import run_query

                run_query(query_b64, "1")

        mock_cursor = mock_psycopg.connect.return_value.cursor.return_value
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("SET default_transaction_read_only = on" in c for c in calls)

    def test_missing_database_url_exits(self) -> None:
        query_b64 = base64.b64encode(b"SELECT 1").decode()

        mock_psycopg = self._make_mock_psycopg()

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict("os.environ", {}, clear=True):
                with pytest.raises(SystemExit) as exc_info:
                    from dev_db_query_runner import run_query

                    run_query(query_b64, "1")

                assert exc_info.value.code == 1

    def test_db_error_exits_with_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        query_b64 = base64.b64encode(b"SELECT 1").decode()

        mock_psycopg = MagicMock()
        mock_psycopg.Error = Exception
        mock_psycopg.connect.side_effect = Exception("connection refused")

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
                with pytest.raises(SystemExit) as exc_info:
                    from dev_db_query_runner import run_query

                    run_query(query_b64, "0")

                assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Database error" in captured.err


class TestMain:
    """Tests for the main() entry point."""

    def test_wrong_arg_count_exits(self) -> None:
        with patch("sys.argv", ["dev-db-query-runner.py"]):
            with pytest.raises(SystemExit) as exc_info:
                from dev_db_query_runner import main

                main()
            assert exc_info.value.code == 1

    def test_invalid_readonly_flag_exits(self) -> None:
        query_b64 = base64.b64encode(b"SELECT 1").decode()
        with patch("sys.argv", ["dev-db-query-runner.py", query_b64, "invalid"]):
            with pytest.raises(SystemExit) as exc_info:
                from dev_db_query_runner import main

                main()
            assert exc_info.value.code == 1
