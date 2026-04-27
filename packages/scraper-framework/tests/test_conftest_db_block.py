"""Regression tests for the ``_block_telemetry_db_writes`` autouse fixture.

These tests verify that the session-scoped fixture in ``conftest.py``
successfully prevents real DB writes during the test session.  They will
fail immediately if the fixture is removed, weakened, or mis-scoped —
making any regression visible before it reaches CI.

Two assertions are checked:
1. ``os.environ["DATABASE_URL"]`` is empty inside a test (the env patch
   is active).
2. ``framework.runner._connect_db()`` returns ``None`` (the function-level
   patch is active).

Note: the module-attribute lookup (``import framework.runner as runner_mod``
then ``runner_mod._connect_db()``) is intentional — it sees the patched
version installed by the autouse fixture rather than any import-time-bound
local reference.
"""

from __future__ import annotations

import os

import framework.runner as runner_mod


def test_database_url_is_empty_in_test_env() -> None:
    """DATABASE_URL must be empty (empty string or absent) during tests.

    The ``_block_telemetry_db_writes`` autouse fixture patches DATABASE_URL
    to ``""`` for the entire test session.  If it is non-empty here, the
    fixture has been dropped or weakened and real DB connections could be
    attempted from runner tests.
    """
    assert os.environ.get("DATABASE_URL", "") == "", (
        "DATABASE_URL is non-empty inside the test session — "
        "the _block_telemetry_db_writes autouse fixture may have been removed "
        "or weakened.  Restore it in conftest.py to prevent accidental writes "
        "to telemetry.scraper_runs."
    )


def test_connect_db_returns_none_in_test_env() -> None:
    """``framework.runner._connect_db()`` must return ``None`` during tests.

    The ``_block_telemetry_db_writes`` autouse fixture patches
    ``framework.runner._connect_db`` to return ``None`` for the entire test
    session.  If it returns anything else here, the fixture has been dropped
    or weakened and runner tests could open live DB connections.
    """
    result = runner_mod._connect_db()
    assert result is None, (
        f"_connect_db() returned {result!r} instead of None inside the test "
        "session — the _block_telemetry_db_writes autouse fixture may have "
        "been removed or weakened.  Restore it in conftest.py."
    )
