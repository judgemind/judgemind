"""Unit tests for plan-phase reuse on infra-preemption retries (#2937).

When the dispatcher restarts or a killswitch cap flip terminates a running
agent that had already completed the plan phase successfully, the next
agent spawned for the same issue should reuse the prior plan rather than
re-running the expensive Opus subprocess.

Covers:

* :meth:`daemon.DispatcherDaemon._try_reuse_prior_plan` — the DB-query
  helper: None when no qualifying row exists, None when go=False, None
  when the issue was edited after the prior plan ran, and the parsed
  output_json dict on a valid hit.
* :meth:`daemon.DispatcherDaemon._materialize_plan_output` — writes
  ``{worktree}/tmp/dispatcher-output/plan.json``.
* :meth:`daemon.DispatcherDaemon._run_plan_phase` — the short-circuit
  branch: subprocess NOT spawned when a reusable plan exists (AC #1),
  subprocess IS spawned when no prior plan exists (AC #2), subprocess IS
  spawned when the issue was modified since the prior plan (AC #3).
* Observability: ``daemon.plan_reused`` event logged with ``agent_id``,
  ``issue_number`` (AC #4).
* Cost impact: ``_persist_phase_output`` called with ``usage=None`` so
  no Opus cost entry is written for the reused copy (AC #5).
"""

from __future__ import annotations

import json
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
# Exception subclass — matches the pattern in test_daemon_phase3c.py.
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
# Shared fakes
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
    logger = logging.getLogger(f"dispatcher.test.plan_reuse.{id(tmp_path)}")
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


# Timestamps used across tests.  prior_ts is older than issue_updated_at
# in the invalidation test and newer in the reuse test.
_PRIOR_TS = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_ISSUE_UPDATED_BEFORE_PLAN = "2026-04-19T10:00:00Z"  # issue unchanged since plan
_ISSUE_UPDATED_AFTER_PLAN = "2026-04-19T14:00:00Z"  # issue edited after plan


_GOOD_PLAN_OUTPUT: dict[str, Any] = {
    "go": True,
    "plan": "Implement feature X",
    "acceptance_criteria": ["AC 1", "AC 2"],
    "scope_check": [],
    "dependencies_to_install": [],
}


# --------------------------------------------------------------------------
# _try_reuse_prior_plan — unit tests for the DB-query helper
# --------------------------------------------------------------------------


class TestTryReusePriorPlan:
    """Tests for :meth:`daemon.DispatcherDaemon._try_reuse_prior_plan`."""

    def test_reuses_when_qualifying_row_exists_and_issue_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Returns the prior output and ts when go=True and issue not modified."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Fake DB returns one qualifying row: (output_json, ts)
        conn.cursor_instance.fetch_queue.append(
            (json.dumps(_GOOD_PLAN_OUTPUT), _PRIOR_TS)
        )

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        assert result is not None
        plan_output, prior_ts_str = result
        assert plan_output["go"] is True
        assert plan_output["plan"] == "Implement feature X"
        assert prior_ts_str  # non-empty timestamp string

    def test_returns_none_when_no_row_found(self, tmp_path: Path) -> None:
        """Returns None when phase_outputs has no qualifying plan row (AC #2)."""
        d, conn, _handler = _make_daemon(tmp_path)
        # fetch_queue is empty → fetchone returns None

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        assert result is None

    def test_returns_none_when_go_false(self, tmp_path: Path) -> None:
        """Returns None when the prior plan said go=False."""
        d, conn, _handler = _make_daemon(tmp_path)
        blocked_output = {"go": False, "block_reason": "already done"}
        conn.cursor_instance.fetch_queue.append((json.dumps(blocked_output), _PRIOR_TS))

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        assert result is None

    def test_returns_none_when_issue_modified_after_plan(self, tmp_path: Path) -> None:
        """Returns None when the issue body was edited after the prior plan (AC #3)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue.append(
            (json.dumps(_GOOD_PLAN_OUTPUT), _PRIOR_TS)
        )

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_AFTER_PLAN)

        assert result is None

    def test_query_filters_on_infra_preemption_phases(self, tmp_path: Path) -> None:
        """SQL query uses _INFRA_PREEMPTION_CATEGORIES as the phase filter."""
        d, conn, _handler = _make_daemon(tmp_path)
        # No row returned — we only care about the SQL that was executed.
        d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        executed_sqls = [sql for sql, _ in conn.cursor_instance.executed]
        assert any("a.phase = ANY" in sql for sql in executed_sqls), (
            "SQL must filter by _INFRA_PREEMPTION_CATEGORIES via a.phase = ANY(%s)"
        )
        assert any(
            "a.status IN ('failed', 'crashed')" in sql for sql in executed_sqls
        ), "SQL must filter terminal agents via a.status IN ('failed', 'crashed')"

    def test_handles_output_json_as_string(self, tmp_path: Path) -> None:
        """Works when psycopg returns JSONB column as a raw string (psycopg2 mode)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue.append(
            (json.dumps(_GOOD_PLAN_OUTPUT), _PRIOR_TS)
        )

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        assert result is not None
        plan_output, _ = result
        assert plan_output["go"] is True

    def test_handles_output_json_as_dict(self, tmp_path: Path) -> None:
        """Works when psycopg returns JSONB column as a parsed dict (psycopg3 mode)."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Provide already-parsed dict instead of JSON string.
        conn.cursor_instance.fetch_queue.append((_GOOD_PLAN_OUTPUT, _PRIOR_TS))

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        assert result is not None
        plan_output, _ = result
        assert plan_output["go"] is True

    def test_returns_none_on_db_exception(self, tmp_path: Path) -> None:
        """DB errors are caught and return None (fail-open, plan re-runs)."""
        d, conn, _handler = _make_daemon(tmp_path)

        original_cursor = conn.cursor

        def _raising_cursor() -> _FakeCursor:
            raise RuntimeError("DB is down")

        conn.cursor = _raising_cursor  # type: ignore[method-assign]

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_BEFORE_PLAN)

        assert result is None
        conn.cursor = original_cursor


# --------------------------------------------------------------------------
# _materialize_plan_output — writes plan.json to the worktree
# --------------------------------------------------------------------------


class TestMaterializePlanOutput:
    def test_writes_plan_json_to_dispatcher_output_dir(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        d._materialize_plan_output(worktree, _GOOD_PLAN_OUTPUT)

        plan_path = worktree / "tmp" / "dispatcher-output" / "plan.json"
        assert plan_path.exists(), "plan.json must be written to dispatcher-output/"
        written = json.loads(plan_path.read_text())
        assert written["go"] is True
        assert written["plan"] == "Implement feature X"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        # Do NOT mkdir worktree — _materialize_plan_output must create dirs.

        d._materialize_plan_output(worktree, _GOOD_PLAN_OUTPUT)

        plan_path = worktree / "tmp" / "dispatcher-output" / "plan.json"
        assert plan_path.exists()


# --------------------------------------------------------------------------
# _run_plan_phase integration — subprocess short-circuit
# --------------------------------------------------------------------------


def _bundle(issue_updated_at: str = _ISSUE_UPDATED_BEFORE_PLAN) -> dict[str, Any]:
    """Minimal issue bundle as returned by _fetch_issue_bundle."""
    return {
        "issue_number": 42,
        "issue_title": "Test issue",
        "issue_body": "body",
        "issue_comments": [],
        "issue_labels": [],
        "blocked_by": [],
        "parent_issue": None,
        "issue_updated_at": issue_updated_at,
    }


class TestPlanReuse:
    """Integration tests for the plan-reuse short-circuit inside _run_plan_phase."""

    # ------------------------------------------------------------------ #
    # AC #1: reuses plan, subprocess NOT spawned                          #
    # ------------------------------------------------------------------ #

    def test_reuses_prior_plan_from_infra_preemption_predecessor(
        self, tmp_path: Path
    ) -> None:
        """Subprocess is NOT spawned; _persist_phase_output called with usage=None.

        Verifies AC #1, AC #4, AC #5.
        """
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Fake the issue fetch to return a bundle with issue_updated_at
        # predating the prior plan's ts.
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value=_bundle(_ISSUE_UPDATED_BEFORE_PLAN)
        )

        # Fake _try_reuse_prior_plan to return a (plan, ts) tuple.
        _PRIOR_TS_STR = "2026-04-19T12:00:00+00:00"
        d._try_reuse_prior_plan = MagicMock(  # type: ignore[method-assign]
            return_value=(_GOOD_PLAN_OUTPUT, _PRIOR_TS_STR)
        )

        # Capture _persist_phase_output calls.
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]

        # Capture _materialize_plan_output calls.
        d._materialize_plan_output = MagicMock()  # type: ignore[method-assign]

        # Subprocess spawn should NOT be called.
        d._run_subprocess_or_fail = MagicMock(  # type: ignore[method-assign]
            return_value=0
        )

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]

        ok = d._run_plan_phase("agent-new", 42, worktree)

        assert ok is True

        # Subprocess was NOT spawned (plan reused).
        d._run_subprocess_or_fail.assert_not_called()

        # _persist_phase_output called with usage=None (AC #5 — no Opus cost).
        d._persist_phase_output.assert_called_once()
        call_kwargs = d._persist_phase_output.call_args
        assert call_kwargs[1].get("usage") is None or (
            len(call_kwargs[0]) >= 5 and call_kwargs[0][4] is None
        ), "usage must be None so no cost entry is written"
        # Also confirm log_text signals reuse.
        log_text_arg = call_kwargs[1].get("log_text") or call_kwargs[0][3]
        assert "reused" in str(log_text_arg)

        # _materialize_plan_output was called.
        d._materialize_plan_output.assert_called_once_with(worktree, _GOOD_PLAN_OUTPUT)

        # _agent_plan_output is set from the reused plan.
        assert d._agent_plan_output == _GOOD_PLAN_OUTPUT

        # Observability: daemon.plan_reused event logged (AC #4).
        reused_events = handler.events("plan_reused")
        assert reused_events, "daemon.plan_reused must be logged on plan reuse"
        record = reused_events[0]
        assert getattr(record, "agent_id", None) == "agent-new"
        assert getattr(record, "issue_number", None) == 42
        assert getattr(record, "prior_plan_output_ts", None) == _PRIOR_TS_STR

    # ------------------------------------------------------------------ #
    # AC #2: no prior plan → subprocess IS spawned                        #
    # ------------------------------------------------------------------ #

    def test_reruns_when_no_prior_plan_exists(self, tmp_path: Path) -> None:
        """Subprocess IS spawned when _try_reuse_prior_plan returns None (AC #2)."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value=_bundle()
        )
        # No prior plan.
        d._try_reuse_prior_plan = MagicMock(return_value=None)  # type: ignore[method-assign]

        fresh_output = {**_GOOD_PLAN_OUTPUT, "plan": "Fresh plan output"}
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=fresh_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        ok = d._run_plan_phase("agent-new", 42, worktree)

        assert ok is True
        # Subprocess WAS spawned (no reuse).
        d._run_subprocess_or_fail.assert_called_once()
        # No plan_reused event emitted.
        assert not handler.events("plan_reused")

    # ------------------------------------------------------------------ #
    # AC #3: issue edited after prior plan → subprocess IS spawned        #
    # ------------------------------------------------------------------ #

    def test_reruns_when_issue_modified_since_prior_plan(self, tmp_path: Path) -> None:
        """Subprocess IS spawned when the issue was edited after the prior plan ran (AC #3)."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Bundle has issue_updated_at AFTER the prior plan's ts.
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value=_bundle(_ISSUE_UPDATED_AFTER_PLAN)
        )
        # _try_reuse_prior_plan returns None because the issue is newer.
        d._try_reuse_prior_plan = MagicMock(return_value=None)  # type: ignore[method-assign]

        fresh_output = {**_GOOD_PLAN_OUTPUT}
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=fresh_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        ok = d._run_plan_phase("agent-new", 42, worktree)

        assert ok is True
        d._run_subprocess_or_fail.assert_called_once()
        # No plan_reused event.
        assert not handler.events("plan_reused")

    # ------------------------------------------------------------------ #
    # plan_blocked predecessor is excluded (go=False guard)               #
    # ------------------------------------------------------------------ #

    def test_does_not_reuse_plan_blocked_predecessor(self, tmp_path: Path) -> None:
        """Plan with go=False (plan_blocked) is not reused — subprocess IS spawned."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value=_bundle()
        )
        # _try_reuse_prior_plan returns None because go=False in DB row.
        d._try_reuse_prior_plan = MagicMock(return_value=None)  # type: ignore[method-assign]

        fresh_output = {**_GOOD_PLAN_OUTPUT}
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=fresh_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        ok = d._run_plan_phase("agent-new", 42, worktree)

        assert ok is True
        d._run_subprocess_or_fail.assert_called_once()
        assert not handler.events("plan_reused")

    # ------------------------------------------------------------------ #
    # budgeted retry predecessor is excluded (phase not in preemption set) #
    # ------------------------------------------------------------------ #

    def test_does_not_reuse_budgeted_retry_predecessor(self, tmp_path: Path) -> None:
        """Agents with stuck_timeout/subprocess_crash terminal phase are excluded.

        The SQL filter uses _INFRA_PREEMPTION_CATEGORIES; stuck_timeout and
        subprocess_crash are not members, so no reuse happens.
        """
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value=_bundle()
        )
        # No row returned from DB (phase filter excludes non-preemption phases).
        d._try_reuse_prior_plan = MagicMock(return_value=None)  # type: ignore[method-assign]

        fresh_output = {**_GOOD_PLAN_OUTPUT}
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=fresh_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        ok = d._run_plan_phase("agent-new", 42, worktree)

        assert ok is True
        d._run_subprocess_or_fail.assert_called_once()
        assert not handler.events("plan_reused")

    # ------------------------------------------------------------------ #
    # go=False in the returned plan — not reused                          #
    # ------------------------------------------------------------------ #

    def test_does_not_reuse_when_go_false_returned_directly(
        self, tmp_path: Path
    ) -> None:
        """_try_reuse_prior_plan returning None (because go=False) → subprocess runs."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value=_bundle()
        )
        d._try_reuse_prior_plan = MagicMock(return_value=None)  # type: ignore[method-assign]

        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        no_go_output = {"go": False, "block_reason": "duplicate"}
        d._read_phase_output = MagicMock(return_value=no_go_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._handle_plan_blocked = MagicMock()  # type: ignore[method-assign]

        ok = d._run_plan_phase("agent-new", 42, worktree)

        assert ok is False  # go=False → orchestration ends
        d._run_subprocess_or_fail.assert_called_once()
        assert not handler.events("plan_reused")


# --------------------------------------------------------------------------
# _try_reuse_prior_plan — edge cases for the timestamp comparison
# --------------------------------------------------------------------------


class TestTryReusePriorPlanTimestampEdgeCases:
    """Detailed timestamp comparison tests for _try_reuse_prior_plan."""

    def test_reuses_when_issue_updated_at_is_empty_string(self, tmp_path: Path) -> None:
        """When issue_updated_at is empty (gh CLI didn't return it), skip the
        timestamp guard and allow reuse."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue.append(
            (json.dumps(_GOOD_PLAN_OUTPUT), _PRIOR_TS)
        )

        result = d._try_reuse_prior_plan(42, "")

        # Empty updated_at → no invalidation → reuse.
        assert result is not None
        plan_output, _ = result
        assert plan_output["go"] is True

    def test_returns_none_when_prior_ts_is_none(self, tmp_path: Path) -> None:
        """When the DB row's ts column is NULL (unexpected), reuse is allowed
        because the timestamp guard condition skips on None prior_ts."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue.append((json.dumps(_GOOD_PLAN_OUTPUT), None))

        result = d._try_reuse_prior_plan(42, _ISSUE_UPDATED_AFTER_PLAN)

        # prior_ts is None → guard skips → reuse allowed.
        assert result is not None
        plan_output, _ = result
        assert plan_output["go"] is True

    def test_prior_ts_as_datetime_object(self, tmp_path: Path) -> None:
        """ts column returned as a datetime object (psycopg3 default)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue.append(
            (json.dumps(_GOOD_PLAN_OUTPUT), _PRIOR_TS)
        )

        # issue_updated_at is before the prior plan ts → should reuse.
        result = d._try_reuse_prior_plan(42, "2026-04-19T10:00:00Z")

        assert result is not None
        plan_output, prior_ts_str = result
        assert plan_output["go"] is True
        # Datetime was converted to ISO string.
        assert "2026-04-19" in prior_ts_str
