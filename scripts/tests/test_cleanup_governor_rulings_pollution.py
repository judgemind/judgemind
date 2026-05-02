"""Tests for cleanup_governor_rulings_pollution script.

Tests cover:
- DELETE_RULINGS_SQL is defined with correct county/ILIKE predicates
- DELETE_CASES_SQL is NOT defined (regression guard — cases delete was removed in #3840)
- main() calls cur.execute() exactly once (rulings delete only)
- main() calls conn.commit() exactly once after the rulings delete

The script depends on psycopg, which may not be available in the lightweight
CI scripts-tests environment. We mock it in sys.modules before importing the
script under test, following the pattern in test_cleanup_legacy_date_partitioned_s3.py.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg at module level,
# which may not be installed in the CI scripts-tests environment.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "one_off"))

_mock_psycopg = MagicMock()

_saved_modules: dict[str, object] = {}
if "psycopg" in sys.modules:
    _saved_modules["psycopg"] = sys.modules["psycopg"]
sys.modules["psycopg"] = _mock_psycopg

import cleanup_governor_rulings_pollution  # noqa: E402

# Restore sys.modules so mock injection doesn't bleed into other test files.
for _mod_name in ("psycopg",):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


# ---------------------------------------------------------------------------
# Test 1: DELETE_RULINGS_SQL is defined with correct predicates
# ---------------------------------------------------------------------------


def test_rulings_sql_contains_statewide_county() -> None:
    """DELETE_RULINGS_SQL targets the 'Statewide' county."""
    assert "Statewide" in cleanup_governor_rulings_pollution.DELETE_RULINGS_SQL


def test_rulings_sql_contains_governor_ilike() -> None:
    """DELETE_RULINGS_SQL filters court_name with '%governor%' ILIKE predicate."""
    assert "%governor%" in cleanup_governor_rulings_pollution.DELETE_RULINGS_SQL


# ---------------------------------------------------------------------------
# Test 2: DELETE_CASES_SQL is NOT defined (regression guard for #3840 AC#2)
# ---------------------------------------------------------------------------


def test_cases_delete_constant_removed() -> None:
    """DELETE_CASES_SQL must not exist on the module (cases delete was removed in #3840)."""
    assert not hasattr(cleanup_governor_rulings_pollution, "DELETE_CASES_SQL")


# ---------------------------------------------------------------------------
# Test 3: main() calls cur.execute() exactly once (rulings delete only)
# ---------------------------------------------------------------------------


def test_main_executes_rulings_delete_only() -> None:
    """main() calls cur.execute() exactly once with DELETE_RULINGS_SQL."""
    mock_conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 178
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("cleanup_governor_rulings_pollution.psycopg") as mock_psycopg_mod:
        mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
            return_value=mock_conn
        )
        mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            cleanup_governor_rulings_pollution.main()

    assert cursor_mock.execute.call_count == 1
    executed_sql = cursor_mock.execute.call_args[0][0]
    assert "derived.rulings" in executed_sql
    assert "derived.cases" not in executed_sql


# ---------------------------------------------------------------------------
# Test 4: main() calls conn.commit() exactly once
# ---------------------------------------------------------------------------


def test_main_commits_exactly_once() -> None:
    """main() calls conn.commit() exactly once after the rulings delete."""
    mock_conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 178
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("cleanup_governor_rulings_pollution.psycopg") as mock_psycopg_mod:
        mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
            return_value=mock_conn
        )
        mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            cleanup_governor_rulings_pollution.main()

    assert mock_conn.commit.call_count == 1
