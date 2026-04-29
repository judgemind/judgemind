"""Unit tests for PR merge-state verification before stamping merged_at (#3752).

Covers three surfaces:

1. ``_merge_pr_and_advance`` — after gh pr merge succeeds, the daemon
   re-fetches the PR state and verifies state==MERGED+mergedAt non-null
   before calling ``_write_merged_at`` and advancing phase.

2. ``_reap_finalize_ecs_success`` — before the merged_at UPDATE, the
   daemon fetches the PR state. Label-strip and ARN clear STILL run even
   when the PR state is unverified (independent invariants).

3. ``_reconcile_stale_merged_at`` — SELECT agents with merged_at set,
   fetch PR state, clear merged_at=NULL when state is not MERGED. Fail-
   closed on gh flake (None payload → no clear). Called from
   ``_housekeeping_tick`` inside a try/except.

All external calls (gh pr view, gh pr merge) and DB writes are mocked —
no real GitHub / DB interaction.

Tests are written to FAIL against the pre-#3752 code and PASS after the
fix — each test asserts on the verification logic that the fix introduces.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirrors test_daemon_merge_auto_unstick.py exactly
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
    logger = logging.getLogger(f"dispatcher.test.merge_verification.{id(tmp_path)}")
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


def _pr_status_green_open() -> dict[str, Any]:
    """Build a gh pr view payload that is green but NOT yet merged (state=OPEN)."""
    return {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
        ],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "head-sha",
        "mergeCommit": None,
        "state": "OPEN",
        "mergedAt": None,
    }


def _pr_status_merged() -> dict[str, Any]:
    """Build a gh pr view payload for a fully merged PR."""
    return {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
        ],
        "mergeable": "MERGED",
        "mergeStateStatus": "MERGED",
        "headRefOid": "head-sha",
        "mergeCommit": {"oid": "abc123merged"},
        "state": "MERGED",
        "mergedAt": "2026-04-29T12:00:00Z",
    }


# --------------------------------------------------------------------------
# _pr_observed_as_merged — unit tests for the static helper
# --------------------------------------------------------------------------


class TestPrObservedAsMerged:
    def test_none_payload_returns_false(self) -> None:
        assert daemon.DispatcherDaemon._pr_observed_as_merged(None) is False

    def test_state_open_returns_false(self) -> None:
        assert (
            daemon.DispatcherDaemon._pr_observed_as_merged(
                {"state": "OPEN", "mergedAt": None}
            )
            is False
        )

    def test_state_merged_no_merged_at_returns_false(self) -> None:
        assert (
            daemon.DispatcherDaemon._pr_observed_as_merged(
                {"state": "MERGED", "mergedAt": None}
            )
            is False
        )

    def test_state_merged_with_merged_at_returns_true(self) -> None:
        assert (
            daemon.DispatcherDaemon._pr_observed_as_merged(
                {"state": "MERGED", "mergedAt": "2026-04-29T12:00:00Z"}
            )
            is True
        )

    def test_empty_dict_returns_false(self) -> None:
        assert daemon.DispatcherDaemon._pr_observed_as_merged({}) is False


# --------------------------------------------------------------------------
# _merge_pr_and_advance — verification guard
# --------------------------------------------------------------------------


class TestMergeSkipsStampWhenPrStateOpen:
    """AC1 half 1: gh pr merge succeeds exit-0, but PR state is OPEN.

    Assert: merged_at NOT written, pr_merged event NOT fired,
    pr_merge_unverified event IS fired, phase NOT advanced.
    This test FAILS against pre-#3752 code (no verification guard).
    """

    def test_merge_skips_stamp_when_pr_state_open(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Stub _fetch_pr_status to return OPEN after merge
        fetch_calls: list[int] = []

        def fake_fetch(pr_number: int) -> dict[str, Any]:
            fetch_calls.append(pr_number)
            return _pr_status_green_open()

        d._fetch_pr_status = fake_fetch  # type: ignore[method-assign]

        # Stub subprocess so gh pr merge returns exit 0
        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Stub _write_merged_at to detect calls
        write_merged_at_calls: list[str] = []
        d._write_merged_at = lambda agent_id, **kw: write_merged_at_calls.append(  # type: ignore[method-assign]
            agent_id
        )

        # Stub _update_agent_phase to detect calls
        phase_advances: list[str] = []
        d._update_agent_phase = lambda agent_id, phase: phase_advances.append(phase)  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-verify-open",
            "issue_number": 3752,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "merge_unstick_attempts": 0,
        }

        d._merge_pr_and_advance(agent, _pr_status_green_open())

        # Verification fetch must have been called
        assert fetch_calls, "expected _fetch_pr_status call after merge"

        # merged_at must NOT be written
        assert write_merged_at_calls == [], (
            "merged_at must not be written when PR state is OPEN"
        )

        # Phase must NOT be advanced
        assert phase_advances == [], "phase must not be advanced when PR state is OPEN"

        # pr_merged event must NOT fire
        assert handler.events("pr_merged") == [], (
            "pr_merged must not fire when PR state is OPEN"
        )

        # pr_merge_unverified event MUST fire
        unverified = handler.events("pr_merge_unverified")
        assert unverified, "expected pr_merge_unverified event"
        assert unverified[0].pr_number == 101


class TestMergeStampsWhenPrStateMerged:
    """AC1 normal path: after a real merge, state==MERGED → stamp + advance.

    This test PASSES against both old and new code (it tests the happy path).
    Included to lock in that the verification guard does not regress green merges.
    """

    def test_merge_stamps_when_pr_state_merged(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_fetch(pr_number: int) -> dict[str, Any]:
            return _pr_status_merged()

        d._fetch_pr_status = fake_fetch  # type: ignore[method-assign]

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        write_merged_at_calls: list[str] = []
        d._write_merged_at = lambda agent_id, **kw: write_merged_at_calls.append(  # type: ignore[method-assign]
            agent_id
        )

        phase_advances: list[str] = []
        d._update_agent_phase = lambda agent_id, phase: phase_advances.append(phase)  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-verify-merged",
            "issue_number": 3752,
            "phase": "awaiting_ci",
            "pr_number": 202,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "merge_unstick_attempts": 0,
        }

        d._merge_pr_and_advance(agent, _pr_status_merged())

        # merged_at MUST be written
        assert write_merged_at_calls == ["agent-verify-merged"], (
            "merged_at must be written when PR state is MERGED"
        )

        # Phase MUST be advanced to awaiting_deploy
        assert "awaiting_deploy" in phase_advances

        # pr_merged event must fire
        merged = handler.events("pr_merged")
        assert merged, "expected pr_merged event on verified merge"
        assert merged[0].pr_number == 202

        # No unverified event
        assert handler.events("pr_merge_unverified") == []


# --------------------------------------------------------------------------
# _reap_finalize_ecs_success — verification guard
# --------------------------------------------------------------------------


class TestReapFinalizeSkipsStampWhenPrOpen:
    """AC1 half 2: _reap_finalize_ecs_success fetches PR state before stamp.

    When state==OPEN: merged_at UPDATE skipped, but label-strip and ARN
    clear still run (independent invariants).
    This test FAILS against pre-#3752 code (no verification in reap path).
    """

    def test_reap_finalize_skips_stamp_when_pr_open(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # _read_agent_pr_number must return a pr_number so verification runs
        d._read_agent_pr_number = lambda agent_id: 303  # type: ignore[method-assign]

        # _fetch_pr_status returns OPEN
        d._fetch_pr_status = lambda pr_number: _pr_status_green_open()  # type: ignore[method-assign]

        # Stub label-strip and ARN clear with call tracking
        label_strip_calls: list[int] = []
        d._gh_issue_remove_labels = lambda issue_number, labels: (
            label_strip_calls.append(  # type: ignore[method-assign]
                issue_number
            )
        )

        d._reap_finalize_ecs_success("agent-reap-open", issue_number=3752)

        # merged_at must NOT be written — look for the UPDATE in executed SQL
        merged_at_updates = [
            e for e in conn.cursor_instance.executed if "SET merged_at = now()" in e[0]
        ]
        assert merged_at_updates == [], (
            "merged_at must not be stamped when PR state is OPEN"
        )

        # Label strip MUST still run — independent invariant
        assert label_strip_calls == [3752], (
            "label strip must still run even when merged_at stamp is skipped"
        )

        # reap_finalize_unverified event MUST fire
        unverified = handler.events("reap_finalize_unverified")
        assert unverified, "expected reap_finalize_unverified event"
        assert unverified[0].pr_number == 303

        # ARN clear must also still run
        arn_clears = [
            e
            for e in conn.cursor_instance.executed
            if "SET agent_task_arn = NULL" in e[0]
        ]
        assert arn_clears, "ARN clear must still run even when stamp is skipped"


# --------------------------------------------------------------------------
# _reconcile_stale_merged_at — reconciliation housekeeping
# --------------------------------------------------------------------------


class TestReconcileClearsWhenPrOpen:
    """AC2: seed agent row with merged_at set, PR state OPEN → merged_at cleared.

    This test FAILS against pre-#3752 code (no reconcile method).
    """

    def test_reconcile_clears_when_pr_open(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Seed one row: agent-x with pr_number 999 (merged_at set)
        conn.cursor_instance.fetchall_queue.append([("agent-x", 999)])

        # _fetch_pr_status returns OPEN (non-None → clear is allowed)
        d._fetch_pr_status = lambda pr_number: _pr_status_green_open()  # type: ignore[method-assign]

        result = d._reconcile_stale_merged_at()

        assert result["checked"] == 1
        assert result["cleared"] == 1
        assert result["errors"] == 0

        # merged_at=NULL UPDATE must have been issued
        null_updates = [
            e for e in conn.cursor_instance.executed if "SET merged_at = NULL" in e[0]
        ]
        assert null_updates, "expected SET merged_at = NULL for stale row"
        assert null_updates[0][1] == ("agent-x",)

        # structured log event fired
        cleared_events = handler.events("merged_at_reconcile_cleared")
        assert cleared_events, "expected merged_at_reconcile_cleared event"
        assert cleared_events[0].agent_id == "agent-x"
        assert cleared_events[0].pr_number == 999


class TestReconcileNoOpWhenPrMerged:
    """AC2 pass-through: state==MERGED → merged_at stays, no UPDATE.

    This test PASSES on both old and new code, but verifies the guard doesn't
    over-clear legitimate rows.
    """

    def test_reconcile_no_op_when_pr_merged(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue.append([("agent-merged", 777)])

        d._fetch_pr_status = lambda pr_number: _pr_status_merged()  # type: ignore[method-assign]

        result = d._reconcile_stale_merged_at()

        assert result["checked"] == 1
        assert result["cleared"] == 0
        assert result["errors"] == 0

        null_updates = [
            e for e in conn.cursor_instance.executed if "SET merged_at = NULL" in e[0]
        ]
        assert null_updates == [], "must not clear merged_at for a genuinely MERGED PR"

        assert handler.events("merged_at_reconcile_cleared") == []


class TestReconcileNoOpOnGhFlake:
    """AC2 fail-closed: _fetch_pr_status returns None (gh flake) → no clear.

    This test FAILS against pre-#3752 code (no reconcile method).
    """

    def test_reconcile_no_op_on_gh_flake(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue.append([("agent-flake", 888)])

        # Simulate gh 502 — fetch returns None
        d._fetch_pr_status = lambda pr_number: None  # type: ignore[method-assign]

        result = d._reconcile_stale_merged_at()

        assert result["checked"] == 1
        assert result["cleared"] == 0  # fail-closed: flake → skip

        null_updates = [
            e for e in conn.cursor_instance.executed if "SET merged_at = NULL" in e[0]
        ]
        assert null_updates == [], "must not clear merged_at on gh flake"

        assert handler.events("merged_at_reconcile_cleared") == []


# --------------------------------------------------------------------------
# _housekeeping_tick — calls _reconcile_stale_merged_at
# --------------------------------------------------------------------------


class TestHousekeepingCallsReconcile:
    """AC2: housekeeping tick invokes _reconcile_stale_merged_at once per tick.

    This test FAILS against pre-#3752 code (no call in housekeeping).
    """

    def test_housekeeping_calls_reconcile(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Provide config fetch queue for each housekeeping target
        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count

        reconcile_calls: list[int] = []

        def fake_reconcile() -> dict[str, int]:
            reconcile_calls.append(1)
            return {"checked": 0, "cleared": 0, "errors": 0}

        d._reconcile_stale_merged_at = fake_reconcile  # type: ignore[method-assign]

        d._housekeeping_tick()

        assert len(reconcile_calls) == 1, (
            "_reconcile_stale_merged_at must be called exactly once per housekeeping tick"
        )

    def test_housekeeping_reconcile_failure_does_not_break_prune(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A reconcile crash must not stop the prune or increment logic."""
        d, conn, handler = _make_daemon(tmp_path)

        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count

        def failing_reconcile() -> dict[str, int]:
            raise RuntimeError("simulated reconcile crash")

        d._reconcile_stale_merged_at = failing_reconcile  # type: ignore[method-assign]

        # Must not raise
        result = d._housekeeping_tick()

        # Prune still returned results (even if all zero-rowcount)
        assert isinstance(result, dict)
        # Counter still advanced
        assert d._housekeeping_ticks == 1
