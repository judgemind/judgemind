"""Tests for cleanup_oc_fused_titles_2522 script.

Tests cover:
- baseline zero skips rebuild (already_clean)
- dry-run skips rebuild (no --apply)
- --apply invokes rebuild and verifies post-count
- --apply raises RuntimeError when post-count is nonzero
- spot-check queries all three content_hash prefixes
- count SQL contains the exact regex acceptance criterion

The script depends on psycopg and structlog, which are not available in the
lightweight CI scripts-tests environment. We mock them in sys.modules before
importing the script under test.
"""

from __future__ import annotations

import inspect
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg and structlog at module
# level, which are not installed in the CI scripts-tests environment.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_mock_psycopg = MagicMock()
_mock_structlog = MagicMock()
_mock_framework_logging = MagicMock()

_modules_to_mock = {
    "psycopg": _mock_psycopg,
    "structlog": _mock_structlog,
    "framework": MagicMock(),
    "framework.logging": _mock_framework_logging,
}

_saved_modules: dict[str, object] = {}
for _mod_name, _mock_mod in _modules_to_mock.items():
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]
    sys.modules[_mod_name] = _mock_mod

import cleanup_oc_fused_titles_2522  # noqa: E402

# Restore sys.modules so mock injection doesn't pollute other test files.
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUSED_COUNT_SQL_FRAGMENT = r"\sv[s]?\.?\s.+\sv[s]?\.?\s"
_SPOT_CHECK_HASHES = ("1578e425", "37ef41f8", "bec66550")


def _make_cursor_mock(side_effects: list) -> MagicMock:
    """Return a cursor mock whose fetchone() returns items from side_effects."""
    cur = MagicMock()
    cur.fetchone.side_effect = side_effects
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _make_conn_mock(cursor_mock: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor_mock
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Test 1: baseline zero — skips rebuild, exits 0
# ---------------------------------------------------------------------------


def test_baseline_zero_skips_rebuild() -> None:
    """When baseline count is 0, exits 0 and never calls subprocess.run."""
    # fetchone() returns count=0 for the fused-title query
    cur = _make_cursor_mock([(0,)])
    conn = _make_conn_mock(cur)

    with patch("cleanup_oc_fused_titles_2522.psycopg") as mock_psycopg:
        mock_psycopg.connect.return_value = conn
        with patch("cleanup_oc_fused_titles_2522.subprocess") as mock_subprocess:
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch("sys.argv", ["cleanup_oc_fused_titles_2522.py"]):
                    result = cleanup_oc_fused_titles_2522.main()

    assert result == 0
    mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: dry-run (no --apply) — skips rebuild, exits 0
# ---------------------------------------------------------------------------


def test_dry_run_skips_rebuild() -> None:
    """Without --apply, even with baseline > 0, subprocess.run is not called."""
    cur = _make_cursor_mock([(42,)])
    conn = _make_conn_mock(cur)

    with patch("cleanup_oc_fused_titles_2522.psycopg") as mock_psycopg:
        mock_psycopg.connect.return_value = conn
        with patch("cleanup_oc_fused_titles_2522.subprocess") as mock_subprocess:
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch("sys.argv", ["cleanup_oc_fused_titles_2522.py"]):
                    result = cleanup_oc_fused_titles_2522.main()

    assert result == 0
    mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: --apply invokes rebuild with correct args and verifies post-count 0
# ---------------------------------------------------------------------------


def test_apply_invokes_rebuild_and_verifies() -> None:
    """With --apply, subprocess.run is called with --county Orange --reset, exits 0."""
    # First fetchone: baseline count=5; subsequent fetchones for re-count + spot-check
    spot_check_returns = [(2,), (3,), (2,)]  # three hashes, all >= 2
    cur = _make_cursor_mock(
        [(5,), (0,)] + spot_check_returns  # baseline=5, post-rebuild=0
    )
    conn = _make_conn_mock(cur)

    with patch("cleanup_oc_fused_titles_2522.psycopg") as mock_psycopg:
        mock_psycopg.connect.return_value = conn
        with patch("cleanup_oc_fused_titles_2522.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch(
                    "sys.argv",
                    ["cleanup_oc_fused_titles_2522.py", "--apply"],
                ):
                    result = cleanup_oc_fused_titles_2522.main()

    assert result == 0
    # Verify subprocess.run was called with --county Orange --reset
    assert mock_subprocess.run.call_count == 1
    call_args = mock_subprocess.run.call_args[0][0]  # positional list arg
    assert "--county" in call_args
    assert "Orange" in call_args
    assert "--reset" in call_args


# ---------------------------------------------------------------------------
# Test 4: --apply raises RuntimeError when post-rebuild count is nonzero
# ---------------------------------------------------------------------------


def test_apply_raises_when_post_count_nonzero() -> None:
    """RuntimeError raised when post-rebuild fused count is still > 0."""
    cur = _make_cursor_mock([(10,), (3,)])  # baseline=10, post-rebuild=3 (still dirty)
    conn = _make_conn_mock(cur)

    with patch("cleanup_oc_fused_titles_2522.psycopg") as mock_psycopg:
        mock_psycopg.connect.return_value = conn
        with patch("cleanup_oc_fused_titles_2522.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch(
                    "sys.argv",
                    ["cleanup_oc_fused_titles_2522.py", "--apply"],
                ):
                    with pytest.raises(RuntimeError):
                        cleanup_oc_fused_titles_2522.main()


# ---------------------------------------------------------------------------
# Test 5: spot-check queries all three content_hash prefixes
# ---------------------------------------------------------------------------


def test_spot_check_queries_three_hashes() -> None:
    """After rebuild, the script queries each of the three content_hash prefixes."""
    spot_check_returns = [(2,), (2,), (2,)]
    cur = _make_cursor_mock([(5,), (0,)] + spot_check_returns)
    conn = _make_conn_mock(cur)

    with patch("cleanup_oc_fused_titles_2522.psycopg") as mock_psycopg:
        mock_psycopg.connect.return_value = conn
        with patch("cleanup_oc_fused_titles_2522.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch(
                    "sys.argv",
                    ["cleanup_oc_fused_titles_2522.py", "--apply"],
                ):
                    cleanup_oc_fused_titles_2522.main()

    # There should have been exactly 5 fetchone calls:
    # 1 baseline, 1 post-rebuild count, 3 spot-check hashes
    assert cur.fetchone.call_count == 5

    # Verify each hash prefix appeared in execute() calls
    execute_calls = cur.execute.call_args_list
    executed_sql_and_params = [
        (str(c[0][0]) if c[0] else "", c[0][1] if len(c[0]) > 1 else ())
        for c in execute_calls
    ]
    for prefix in _SPOT_CHECK_HASHES:
        found = any(
            prefix in str(param)
            for _, params in executed_sql_and_params
            for param in (params if isinstance(params, (list, tuple)) else [params])
        )
        assert found, f"Hash prefix {prefix!r} not found in any DB execute call"


# ---------------------------------------------------------------------------
# Test 6: count SQL contains the exact regex from AC#2
# ---------------------------------------------------------------------------


def test_count_sql_matches_acceptance_criterion() -> None:
    r"""The SQL used to count fused titles contains the exact regex pattern.

    The acceptance criterion requires the regex: \sv[s]?\.?\s.+\sv[s]?\.?\s
    """
    source = inspect.getsource(cleanup_oc_fused_titles_2522)
    assert _FUSED_COUNT_SQL_FRAGMENT in source, (
        f"Expected SQL fragment {_FUSED_COUNT_SQL_FRAGMENT!r} not found in source. "
        "The count SQL must match the acceptance criterion exactly."
    )
