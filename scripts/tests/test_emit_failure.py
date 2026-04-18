"""Tests for scripts/dispatcher/emit_failure.py.

Follows the ``test_dev_db_query_runner.py`` pattern: ``psycopg`` is not
installed in the minimal ``scripts-tests`` CI environment, so we patch
it into ``sys.modules`` for ``TestEmitFailureWithInsert`` and call the
entry point with the mock in place.

The helper functions (``_parse_args``, ``_validated_table``,
``_parse_details``, ``_emit_timing``) are tested directly without any
psycopg involvement.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to path so we can import scripts.dispatcher.emit_failure.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from dispatcher.emit_failure import (  # noqa: E402
    _parse_args,
    _parse_details,
    _validated_table,
)


class TestParseArgs:
    """Argparse contract."""

    def test_minimal_required_args(self) -> None:
        ns = _parse_args(
            [
                "--category",
                "spike_test",
                "--agent-id",
                "agent-abc",
            ]
        )
        assert ns.category == "spike_test"
        assert ns.agent_id == "agent-abc"
        assert ns.details == "{}"
        assert ns.table == "dispatcher_spike.failures"

    def test_full_args(self) -> None:
        ns = _parse_args(
            [
                "--category",
                "cwd_drift",
                "--agent-id",
                "agent-xyz",
                "--details",
                '{"tool":"Read"}',
                "--table",
                "dispatcher.failures",
            ]
        )
        assert ns.category == "cwd_drift"
        assert ns.agent_id == "agent-xyz"
        assert ns.details == '{"tool":"Read"}'
        assert ns.table == "dispatcher.failures"

    def test_missing_category_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--agent-id", "a"])

    def test_missing_agent_id_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--category", "c"])


class TestValidatedTable:
    """Table-identifier SQL-injection guard."""

    def test_accepts_simple_schema_table(self) -> None:
        assert (
            _validated_table("dispatcher_spike.failures") == "dispatcher_spike.failures"
        )

    def test_accepts_underscores_and_digits(self) -> None:
        assert _validated_table("a_1.b_2") == "a_1.b_2"

    def test_rejects_missing_schema(self) -> None:
        with pytest.raises(ValueError):
            _validated_table("failures")

    def test_rejects_semicolon_injection(self) -> None:
        with pytest.raises(ValueError):
            _validated_table("dispatcher_spike.failures; DROP TABLE x")

    def test_rejects_quote_injection(self) -> None:
        with pytest.raises(ValueError):
            _validated_table('"dispatcher_spike"."failures"')

    def test_rejects_spaces(self) -> None:
        with pytest.raises(ValueError):
            _validated_table("dispatcher_spike. failures")

    def test_rejects_dots_in_name(self) -> None:
        with pytest.raises(ValueError):
            _validated_table("dispatcher.spike.failures")


class TestParseDetails:
    """--details JSON parsing."""

    def test_empty_returns_empty_dict(self) -> None:
        assert _parse_details("{}") == {}

    def test_valid_object(self) -> None:
        assert _parse_details('{"a":1,"b":"two"}') == {"a": 1, "b": "two"}

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_details("not json") == {}

    def test_array_not_accepted(self) -> None:
        # We store dicts only — arrays fall back to {}.
        assert _parse_details("[1,2,3]") == {}

    def test_null_not_accepted(self) -> None:
        assert _parse_details("null") == {}


class TestEmitFailureWithInsert:
    """End-to-end entry-point tests with psycopg mocked in sys.modules."""

    def _make_mock_psycopg(self) -> MagicMock:
        """Construct a mock psycopg module with connect() returning a context."""
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        # mirror the real psycopg.Error for the except clause
        mock_psycopg.Error = Exception

        return mock_psycopg

    def test_happy_path_inserts_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_psycopg = self._make_mock_psycopg()
        mock_cursor = mock_psycopg.connect.return_value.cursor.return_value

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                from dispatcher.emit_failure import emit_failure

                rc = emit_failure(
                    [
                        "--category",
                        "spike_test",
                        "--agent-id",
                        "harness-1",
                        "--details",
                        '{"tool":"Read"}',
                        "--table",
                        "dispatcher_spike.failures",
                    ]
                )

        assert rc == 0
        # One execute call with an INSERT into the correct table.
        assert mock_cursor.execute.call_count == 1
        sql, params = mock_cursor.execute.call_args[0]
        assert "INSERT INTO dispatcher_spike.failures" in sql
        assert params[1] == "spike_test"  # category
        assert params[2] == "harness-1"  # agent_id
        # params[3] is details JSON-encoded.
        assert json.loads(params[3]) == {"tool": "Read"}

        # Timing emission goes to stderr.
        captured = capsys.readouterr()
        assert "EMIT_FAILURE_TIMING" in captured.err
        # Extract the JSON tail after the prefix to verify the shape.
        line = next(
            line
            for line in captured.err.splitlines()
            if line.startswith("EMIT_FAILURE_TIMING")
        )
        payload = json.loads(line.split(" ", 1)[1])
        assert payload["status"] == "ok"
        assert payload["failure_id"]  # non-empty uuid string
        assert payload["elapsed_ms"] >= 0.0

    def test_missing_database_url_swallows(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_psycopg = self._make_mock_psycopg()

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict(os.environ, {}, clear=True):
                from dispatcher.emit_failure import emit_failure

                rc = emit_failure(
                    [
                        "--category",
                        "spike_test",
                        "--agent-id",
                        "harness-1",
                    ]
                )

        assert rc == 0
        # No DB calls attempted.
        mock_psycopg.connect.assert_not_called()
        captured = capsys.readouterr()
        assert "DATABASE_URL not set" in captured.err
        # Timing payload still emitted with status=no_db_url.
        line = next(
            line
            for line in captured.err.splitlines()
            if line.startswith("EMIT_FAILURE_TIMING")
        )
        payload = json.loads(line.split(" ", 1)[1])
        assert payload["status"] == "no_db_url"

    def test_insert_failure_is_swallowed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A psycopg error MUST NOT propagate — the hook must return 0."""
        mock_psycopg = MagicMock()
        mock_psycopg.connect.side_effect = RuntimeError("host unreachable")
        mock_psycopg.Error = Exception

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict(
                os.environ,
                {"DATABASE_URL": "postgresql://bogus-host-that-does-not-exist:5432/x"},
            ):
                from dispatcher.emit_failure import emit_failure

                rc = emit_failure(
                    [
                        "--category",
                        "spike_test",
                        "--agent-id",
                        "harness-1",
                    ]
                )

        assert rc == 0
        captured = capsys.readouterr()
        assert "insert failed" in captured.err
        # Status in timing payload should be 'error'.
        line = next(
            line
            for line in captured.err.splitlines()
            if line.startswith("EMIT_FAILURE_TIMING")
        )
        payload = json.loads(line.split(" ", 1)[1])
        assert payload["status"] == "error"

    def test_unsafe_table_identifier_swallowed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unsafe --table value must not reach psycopg, but the hook still returns 0."""
        mock_psycopg = self._make_mock_psycopg()

        with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                from dispatcher.emit_failure import emit_failure

                rc = emit_failure(
                    [
                        "--category",
                        "spike_test",
                        "--agent-id",
                        "harness-1",
                        "--table",
                        "bad; DROP TABLE x",
                    ]
                )

        assert rc == 0
        mock_psycopg.connect.assert_not_called()
        captured = capsys.readouterr()
        assert "refusing unsafe table identifier" in captured.err


class TestMainEntryPoint:
    """argparse SystemExit is caught inside emit_failure() (nothing to assert via main)."""

    def test_bad_args_still_returns_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Missing both required args. emit_failure() catches SystemExit and returns 0.
        from dispatcher.emit_failure import emit_failure

        rc = emit_failure([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "bad arguments" in captured.err
