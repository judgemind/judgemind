"""Unit tests for TERMINAL_AGENT_STATUSES constant usage in daemon.py.

Covers two contract properties introduced by issue #2863:

1. ``_mark_agent_terminal`` raises ``AssertionError`` when given a status
   that is neither ``'running'`` nor a member of ``TERMINAL_AGENT_STATUSES``.
   This guards against typos or the incremental-status-addition mistake where
   the daemon writes an unknown status to ``dispatcher.agents``.

2. Each valid status — ``TERMINAL_AGENT_STATUSES`` ∪ {``'running'``} — is
   accepted without raising.  Accepted means the method reaches the SQL
   execution path (i.e. the guard passes, and the DB cursor receives at
   least one ``execute`` call).

All external calls (psycopg) are mocked.  Uses the same ``_make_daemon``
fixture shape as ``test_daemon_phase3e.py``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — minimal subset needed for _mark_agent_terminal.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> list[Any]:
        return []


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection]:
    logger = logging.getLogger(f"dispatcher.test.terminal_statuses.{id(tmp_path)}")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_mark_agent_terminal_rejects_unknown_status(tmp_path: Path) -> None:
    """_mark_agent_terminal raises AssertionError for an unknown status."""
    d, conn = _make_daemon(tmp_path)
    with pytest.raises(AssertionError, match="unknown status"):
        d._mark_agent_terminal(
            agent_id="agent-bogus",
            status="bogus",
            phase="some_phase",
        )
    # No SQL must have been issued — the guard fires before any DB call.
    assert conn.cursor_instance.executed == []


def test_mark_agent_terminal_rejects_empty_status(tmp_path: Path) -> None:
    """_mark_agent_terminal raises AssertionError for an empty string status."""
    d, conn = _make_daemon(tmp_path)
    with pytest.raises(AssertionError, match="unknown status"):
        d._mark_agent_terminal(
            agent_id="agent-empty",
            status="",
            phase="some_phase",
        )
    assert conn.cursor_instance.executed == []


def test_mark_agent_terminal_accepts_all_valid_statuses(tmp_path: Path) -> None:
    """Every value in TERMINAL_AGENT_STATUSES ∪ {'running'} is accepted."""
    valid_statuses = daemon.TERMINAL_AGENT_STATUSES | {"running"}
    for status in sorted(valid_statuses):
        d, conn = _make_daemon(tmp_path)
        # Should not raise — the guard passes and the method attempts SQL.
        try:
            d._mark_agent_terminal(
                agent_id=f"agent-{status}",
                status=status,
                phase="some_phase",
            )
        except AssertionError as exc:
            pytest.fail(
                f"_mark_agent_terminal raised AssertionError for valid status={status!r}: {exc}"
            )
        # At least one SQL statement must have been issued.
        assert conn.cursor_instance.executed, (
            f"Expected at least one SQL call for status={status!r}, got none"
        )


def test_terminal_agent_statuses_constant_contents() -> None:
    """TERMINAL_AGENT_STATUSES contains exactly the expected five values."""
    expected = frozenset(
        {"succeeded", "failed", "crashed", "plan_blocked", "needs_review"}
    )
    assert daemon.TERMINAL_AGENT_STATUSES == expected


def test_correct_outcome_terminal_statuses_constant_contents() -> None:
    """CORRECT_OUTCOME_TERMINAL_STATUSES contains exactly the expected three values."""
    expected = frozenset({"succeeded", "plan_blocked", "needs_review"})
    assert daemon.CORRECT_OUTCOME_TERMINAL_STATUSES == expected


def test_correct_outcome_is_subset_of_terminal() -> None:
    """CORRECT_OUTCOME_TERMINAL_STATUSES must be a subset of TERMINAL_AGENT_STATUSES."""
    assert daemon.CORRECT_OUTCOME_TERMINAL_STATUSES <= daemon.TERMINAL_AGENT_STATUSES
