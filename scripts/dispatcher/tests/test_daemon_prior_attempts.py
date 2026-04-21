"""Unit tests for prior-attempt context in retry ralph spawns (#2984).

Covers:

* :meth:`daemon.DispatcherDaemon._fetch_prior_attempts` — DB-query helper:
  empty list when no rows, correct row shape when rows exist.
* :meth:`daemon.DispatcherDaemon._materialize_prior_attempts` — writes
  ``{worktree}/tmp/dispatcher-output/prior_attempts.md`` when count > 0
  (AC b.1), does NOT write the file when count == 0 (AC b.2).
* SQL filter: query uses ``phase NOT IN`` + the full
  ``_INFRA_PREEMPTION_CATEGORIES`` list so infra-preempted agents are
  excluded (AC b.2 variant).
* :meth:`daemon.DispatcherDaemon._run_ralph_phase` — ``daemon.ralph_spawn``
  log event carries ``prior_attempts_count`` (observability AC).
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Install a psycopg stub whose ``errors.UniqueViolation`` is a real
# Exception subclass — matches the pattern in test_daemon_plan_reuse.py.
if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_plan_reuse.py)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.fetch_queue:
            return None
        return self.fetch_queue.pop(0)

    def fetchall(self) -> list[Any]:
        if not self.fetchall_queue:
            return []
        return self.fetchall_queue.pop(0)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _CapturingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.prior_attempts.{id(tmp_path)}")
    logger.handlers = [handler]
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
    return d, conn, handler


_TS1 = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
_TS2 = datetime(2026, 4, 19, 8, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# _fetch_prior_attempts — unit tests
# --------------------------------------------------------------------------


class TestFetchPriorAttempts:
    def test_fetch_empty(self, tmp_path: Path) -> None:
        """Returns empty list when no failed agents exist for the issue."""
        d, conn, _handler = _make_daemon(tmp_path)
        # fetchall_queue is empty → fetchall returns []

        result = d._fetch_prior_attempts(42)

        assert result == []

    def test_fetch_two_rows(self, tmp_path: Path) -> None:
        """Returns two dicts when two rows exist."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append(
            [
                ("subprocess_crash", "ralph log one", "push log one", _TS1),
                ("stuck_timeout", None, "push log two", _TS2),
            ]
        )

        result = d._fetch_prior_attempts(42)

        assert len(result) == 2
        assert result[0]["failure_summary"] == "subprocess_crash"
        assert result[0]["ralph_log_text"] == "ralph log one"
        assert result[0]["push_log_text"] == "push log one"
        assert "2026-04-20" in result[0]["started_at"]
        assert result[1]["failure_summary"] == "stuck_timeout"
        assert result[1]["ralph_log_text"] is None

    def test_fetch_returns_empty_on_db_exception(self, tmp_path: Path) -> None:
        """DB errors are caught and return empty list (fail-open)."""
        d, conn, _handler = _make_daemon(tmp_path)

        def _raising_cursor() -> _FakeCursor:
            raise RuntimeError("DB is down")

        conn.cursor = _raising_cursor  # type: ignore[method-assign]

        result = d._fetch_prior_attempts(42)

        assert result == []

    def test_query_excludes_infra_preempted(self, tmp_path: Path) -> None:
        """SQL query uses ``phase NOT IN`` to exclude infra-preemption categories."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([])

        d._fetch_prior_attempts(42)

        executed_sqls = [sql for sql, _ in conn.cursor_instance.executed]
        assert any("phase NOT IN" in sql for sql in executed_sqls), (
            "SQL must filter with 'phase NOT IN' to exclude infra-preemption phases"
        )
        assert any(
            "a.status IN ('failed', 'crashed')" in sql for sql in executed_sqls
        ), "SQL must filter terminal agents"


# --------------------------------------------------------------------------
# _materialize_prior_attempts — writes the file
# --------------------------------------------------------------------------


class TestMaterializePriorAttempts:
    def test_materialize_writes_two_attempts(self, tmp_path: Path) -> None:
        """Writes prior_attempts.md with both attempts' content (AC b.1)."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        conn.cursor_instance.fetchall_queue.append(
            [
                (
                    "subprocess_crash",
                    "ralph log with ## Iteration feedback\n\nSome feedback text",
                    "push stderr tail here",
                    _TS1,
                ),
                (
                    "stuck_timeout",
                    None,
                    "another push log",
                    _TS2,
                ),
            ]
        )

        count = d._materialize_prior_attempts(worktree, 42)

        assert count == 2
        out_file = worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        assert out_file.exists(), "prior_attempts.md must be written when count > 0"
        content = out_file.read_text(encoding="utf-8")
        # Both attempts must appear.
        assert "Prior attempt 1" in content
        assert "Prior attempt 2" in content
        # Categories.
        assert "subprocess_crash" in content
        assert "stuck_timeout" in content
        # Ralph narrative extracted.
        assert "## Iteration feedback" in content
        assert "Some feedback text" in content
        # Push tail.
        assert "push stderr tail here" in content

    def test_materialize_first_attempt_no_file(self, tmp_path: Path) -> None:
        """Does NOT write file when count == 0 (AC b.2, first-attempt no-regression)."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # fetchall returns empty list → 0 attempts.

        count = d._materialize_prior_attempts(worktree, 42)

        assert count == 0
        out_file = worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        assert not out_file.exists(), (
            "prior_attempts.md must NOT be written when count == 0"
        )

    def test_materialize_excludes_infra_preempted(self, tmp_path: Path) -> None:
        """SQL filter excludes infra-preempted phases (implicit via _fetch_prior_attempts)."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # Return empty — simulates the filter excluding infra rows.

        count = d._materialize_prior_attempts(worktree, 42)

        executed_sqls = [sql for sql, _ in conn.cursor_instance.executed]
        assert any("phase NOT IN" in sql for sql in executed_sqls), (
            "SQL must use 'phase NOT IN' to exclude infra-preemption categories"
        )
        assert count == 0

    def test_materialize_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Creates the dispatcher-output/ directory if it doesn't exist."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "new-worktree"
        # Do NOT mkdir — _materialize_prior_attempts must create dirs.

        conn.cursor_instance.fetchall_queue.append(
            [
                ("subprocess_crash", None, None, _TS1),
            ]
        )

        count = d._materialize_prior_attempts(worktree, 42)

        assert count == 1
        out_file = worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        assert out_file.exists()

    def test_materialize_push_log_tail_truncated(self, tmp_path: Path) -> None:
        """Push log is truncated to the last ~2000 chars."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        long_push_log = "X" * 5000 + "LAST_2000"
        conn.cursor_instance.fetchall_queue.append(
            [("subprocess_crash", None, long_push_log, _TS1)]
        )

        d._materialize_prior_attempts(worktree, 42)

        content = (
            worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        ).read_text(encoding="utf-8")
        assert "LAST_2000" in content
        assert "X" * 5000 not in content  # The leading Xs were truncated.


# --------------------------------------------------------------------------
# _run_ralph_phase — observability: daemon.ralph_spawn event
# --------------------------------------------------------------------------


class TestRunRalphPhaseLogs:
    def test_run_ralph_phase_logs_prior_attempts_count(self, tmp_path: Path) -> None:
        """``daemon.ralph_spawn`` log event carries ``prior_attempts_count=2``."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Patch helpers so the test doesn't need real subprocesses or DB.
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(  # type: ignore[method-assign]
            return_value=2
        )
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]

        good_ralph_output = {
            "verdict": "SHIP",
            "iterations_used": 1,
            "block_reason": None,
            "changed_files": [],
            "summary": "done",
        }
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value=good_ralph_output
        )
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        ok = d._run_ralph_phase("agent-test", 42, worktree)

        assert ok is True

        spawn_events = handler.events("ralph_spawn")
        assert spawn_events, "daemon.ralph_spawn must be logged"
        record = spawn_events[0]
        assert getattr(record, "prior_attempts_count", None) == 2, (
            "prior_attempts_count must be 2 in the ralph_spawn event"
        )
        assert getattr(record, "agent_id", None) == "agent-test"
        assert getattr(record, "issue_number", None) == 42

    def test_run_ralph_phase_logs_zero_for_first_attempt(self, tmp_path: Path) -> None:
        """``daemon.ralph_spawn`` logs ``prior_attempts_count=0`` for first attempts."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(  # type: ignore[method-assign]
            return_value=0
        )
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]

        good_ralph_output = {
            "verdict": "SHIP",
            "iterations_used": 1,
            "block_reason": None,
            "changed_files": [],
            "summary": "done",
        }
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value=good_ralph_output
        )
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        ok = d._run_ralph_phase("agent-new", 42, worktree)

        assert ok is True
        spawn_events = handler.events("ralph_spawn")
        assert spawn_events
        assert getattr(spawn_events[0], "prior_attempts_count", None) == 0
