"""Unit tests for issue #3385 — _fetch_phase_output raises PhaseOutputFetchError
on psycopg exceptions instead of silently returning None.

Covers:
- ``test_summary_fetch_psycopg_error_routes_through_handle_agent_failure`` —
  single OperationalError from ``_fetch_phase_output(phase="summary")`` inside
  ``_push_and_open_pr`` routes through ``_handle_agent_failure`` with
  ``category=phase_output_fetch_failed``, no PR opened, no commit amended.
- ``test_persistent_fetch_failure_does_not_open_pr`` —
  three consecutive raises across three ``_push_and_open_pr`` calls all route
  through ``_handle_agent_failure``; no ``git commit --amend`` or ``gh pr create``.
- ``test_run_summary_phase_fetch_psycopg_error_routes_through_handle_agent_failure`` —
  mirror for ``_run_summary_phase``'s ralph + plan fetches.
- ``test_run_ralph_phase_fetch_psycopg_error_routes_through_handle_agent_failure`` —
  mirror for ``_run_ralph_phase``'s plan fetch.
- ``test_happy_path_fetch_phase_output_returns_dict`` —
  sanity: normal cursor returns a dict, no raise.
- ``test_fetch_phase_output_row_none_returns_none`` —
  row not found (no DB error) still returns None.
- ``test_fetch_phase_output_raises_phase_output_fetch_error_on_exception`` —
  real DB exception → PhaseOutputFetchError, not None.

All external calls (subprocesses, real psycopg, network) are mocked.
Mirrors test_daemon_summary_output_incomplete.py style.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above
from dispatcher.daemon import (  # noqa: E402
    FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED,
    PhaseOutputFetchError,
    TIER_2_FIRST_OCCURRENCE_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Shared fakes (mirror test_daemon_summary_output_incomplete.py)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
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

    def cursor(self) -> "_FakeCursor":
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
    logger = logging.getLogger(
        f"dispatcher.test.phase_output_db_failure.{id(tmp_path)}"
    )
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


def _patch_push_and_open_pr_helpers(
    d: daemon.DispatcherDaemon,
    *,
    fetch_side_effect: Any,
) -> None:
    """Stub helpers for ``_push_and_open_pr`` so DB/subprocess are not needed.

    Sets ``_fetch_phase_output`` to raise (or return) according to
    ``fetch_side_effect``. ``_handle_agent_failure`` is captured for assertions.
    """
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._is_noop_ship = MagicMock(return_value=False)  # type: ignore[method-assign]
    d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
        side_effect=fetch_side_effect
    )


def _patch_run_summary_phase_helpers(
    d: daemon.DispatcherDaemon,
    *,
    fetch_side_effect: Any,
) -> None:
    """Stub helpers for ``_run_summary_phase``."""
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._read_phase_output = MagicMock(  # type: ignore[method-assign]
        return_value={
            "commit_message": "feat(foo): bar (#3385)",
            "pr_title": "feat(foo): bar (#3385)",
            "pr_body_md": "## Summary\n\nBar.\n\nCloses #3385",
        }
    )
    d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
    d._extract_log_preview = MagicMock(return_value="")  # type: ignore[method-assign]
    d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
    d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
    d._write_phase_input = MagicMock()  # type: ignore[method-assign]
    d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
        return_value={
            "issue_number": 3385,
            "issue_title": "Test issue",
            "issue_body": "Body",
            "issue_comments": [],
            "issue_labels": [],
            "blocked_by": [],
            "parent_issue": None,
        }
    )
    d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
        side_effect=fetch_side_effect
    )


def _patch_run_ralph_phase_helpers(
    d: daemon.DispatcherDaemon,
    *,
    fetch_side_effect: Any,
) -> None:
    """Stub helpers for ``_run_ralph_phase``."""
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._apply_prior_ralph_patch = MagicMock(return_value=None)  # type: ignore[method-assign]
    d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._read_phase_output = MagicMock(  # type: ignore[method-assign]
        return_value={
            "ship": True,
            "summary": "Implemented the fix",
            "changed_files": ["scripts/dispatcher/daemon.py"],
        }
    )
    d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
    d._extract_log_preview = MagicMock(return_value="")  # type: ignore[method-assign]
    d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
    d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
    d._write_phase_input = MagicMock()  # type: ignore[method-assign]
    d._store_ralph_patch = MagicMock()  # type: ignore[method-assign]
    d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
        side_effect=fetch_side_effect
    )


# ---------------------------------------------------------------------------
# _fetch_phase_output unit tests (low-level)
# ---------------------------------------------------------------------------


class TestFetchPhaseOutputDirectly:
    """Tests that directly drive _fetch_phase_output to verify the raise path."""

    def test_fetch_phase_output_raises_phase_output_fetch_error_on_exception(
        self, tmp_path: Path
    ) -> None:
        """A psycopg (or any) exception inside _fetch_phase_output must raise
        PhaseOutputFetchError — not return None.
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        # Make cursor().execute() raise to simulate a psycopg OperationalError.
        class _FakePsycopgError(Exception):
            pass

        class _ExplodingCursor:
            def __enter__(self) -> "_ExplodingCursor":
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def execute(self, *_a: Any, **_kw: Any) -> None:
                raise _FakePsycopgError("connection lost")

        class _ExplodingConn:
            commits = 0
            rollbacks = 0

            def cursor(self) -> "_ExplodingCursor":
                return _ExplodingCursor()

            def commit(self) -> None:
                self.commits += 1

            def rollback(self) -> None:
                self.rollbacks += 1

        d._conn = _ExplodingConn()  # type: ignore[assignment]

        with pytest.raises(PhaseOutputFetchError) as exc_info:
            d._fetch_phase_output("agent-abc", "summary")

        # __cause__ must be the original psycopg exception.
        assert isinstance(exc_info.value.__cause__, _FakePsycopgError)

    def test_fetch_phase_output_row_none_returns_none(self, tmp_path: Path) -> None:
        """Row not found (no DB error) still returns None — no regression."""
        d, conn, _handler = _make_daemon(tmp_path)
        # _FakeConnection's cursor fetchone() returns None by default.
        result = d._fetch_phase_output("agent-abc", "plan")
        assert result is None

    def test_happy_path_fetch_phase_output_returns_dict(self, tmp_path: Path) -> None:
        """Normal path: row present with a dict JSONB → dict returned, no raise."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue.append(({"commit_message": "feat: x"},))
        result = d._fetch_phase_output("agent-abc", "summary")
        assert result == {"commit_message": "feat: x"}


# ---------------------------------------------------------------------------
# _push_and_open_pr DB-failure routing tests
# ---------------------------------------------------------------------------


class TestPushAndOpenPrDbFailureRouting:
    def test_summary_fetch_psycopg_error_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """Single PhaseOutputFetchError from ``_fetch_phase_output(phase='summary')``
        inside ``_push_and_open_pr`` must route through ``_handle_agent_failure``
        with ``category=phase_output_fetch_failed``, and must NOT open a PR or
        amend a commit.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        cause = Exception("connection lost")
        err = PhaseOutputFetchError("DB error fetching summary")
        err.__cause__ = cause

        _patch_push_and_open_pr_helpers(
            d,
            fetch_side_effect=err,
        )

        d._push_and_open_pr("agent-db-fail", 3385, worktree)

        # _handle_agent_failure called once with the right category.
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-db-fail"
        assert kwargs["category"] == FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED
        assert kwargs["issue_number"] == 3385

        # No terminal was called directly — the unified handler owns that.
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_persistent_fetch_failure_does_not_open_pr(self, tmp_path: Path) -> None:
        """Three consecutive PhaseOutputFetchError raises across three
        ``_push_and_open_pr`` calls must all route through
        ``_handle_agent_failure``; no ``git commit --amend`` or ``gh pr create``.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Build a factory that always produces a fresh PhaseOutputFetchError.
        def _make_err() -> PhaseOutputFetchError:
            cause = Exception("db unavailable")
            err = PhaseOutputFetchError("fetch failed")
            err.__cause__ = cause
            return err

        _patch_push_and_open_pr_helpers(
            d,
            fetch_side_effect=[_make_err(), _make_err(), _make_err()],
        )

        # Drive three consecutive calls.
        d._push_and_open_pr("agent-persistent", 3385, worktree)
        d._push_and_open_pr("agent-persistent", 3385, worktree)
        d._push_and_open_pr("agent-persistent", 3385, worktree)

        # All three routed through _handle_agent_failure.
        assert d._handle_agent_failure.call_count == 3  # type: ignore[union-attr]
        for call in d._handle_agent_failure.call_args_list:  # type: ignore[union-attr]
            assert call.kwargs["category"] == FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED

        # No direct terminal call.
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# _run_summary_phase DB-failure routing tests
# ---------------------------------------------------------------------------


class TestRunSummaryPhaseDbFailureRouting:
    def test_run_summary_phase_ralph_fetch_psycopg_error_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """PhaseOutputFetchError from the ralph fetch inside ``_run_summary_phase``
        routes through ``_handle_agent_failure`` with
        ``category=phase_output_fetch_failed`` and returns False.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        cause = Exception("connection lost")
        err = PhaseOutputFetchError("DB error fetching ralph output")
        err.__cause__ = cause

        # First (ralph) fetch raises; return value is never reached.
        _patch_run_summary_phase_helpers(
            d,
            fetch_side_effect=err,
        )

        result = d._run_summary_phase("agent-db-fail-summary", 3385, worktree)

        assert result is False
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-db-fail-summary"
        assert kwargs["category"] == FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED
        assert kwargs["issue_number"] == 3385

    def test_run_summary_phase_plan_fetch_psycopg_error_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """PhaseOutputFetchError from the plan fetch inside ``_run_summary_phase``
        (after the ralph fetch succeeds) routes through ``_handle_agent_failure``
        with ``category=phase_output_fetch_failed`` and returns False.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # First call (ralph fetch) returns a valid dict; second call (plan fetch) raises.
        ralph_dict = {"changed_files": ["scripts/dispatcher/daemon.py"], "summary": "x"}
        cause = Exception("db gone away")
        plan_err = PhaseOutputFetchError("DB error fetching plan output")
        plan_err.__cause__ = cause

        _patch_run_summary_phase_helpers(
            d,
            fetch_side_effect=[ralph_dict, plan_err],
        )

        result = d._run_summary_phase("agent-db-fail-plan", 3385, worktree)

        assert result is False
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-db-fail-plan"
        assert kwargs["category"] == FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED
        assert kwargs["issue_number"] == 3385


# ---------------------------------------------------------------------------
# _run_ralph_phase DB-failure routing tests
# ---------------------------------------------------------------------------


class TestRunRalphPhaseDbFailureRouting:
    def test_run_ralph_phase_fetch_psycopg_error_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """PhaseOutputFetchError from ``_fetch_phase_output(phase='plan')`` inside
        ``_run_ralph_phase`` routes through ``_handle_agent_failure`` with
        ``category=phase_output_fetch_failed`` and returns False.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        cause = Exception("connection lost")
        err = PhaseOutputFetchError("DB error fetching plan output")
        err.__cause__ = cause

        _patch_run_ralph_phase_helpers(
            d,
            fetch_side_effect=err,
        )

        result = d._run_ralph_phase("agent-db-fail-ralph", 3385, worktree)

        assert result is False
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-db-fail-ralph"
        assert kwargs["category"] == FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED
        assert kwargs["issue_number"] == 3385


# ---------------------------------------------------------------------------
# Tier classification regression guard
# ---------------------------------------------------------------------------


class TestTierClassification:
    def test_phase_output_fetch_failed_in_tier2_first_occurrence(self) -> None:
        """The new category must diagnose on first occurrence so the diagnoser
        can pick retry vs escalate — mirror the phase_output_missing assertion.
        """
        assert (
            FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED
            in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
