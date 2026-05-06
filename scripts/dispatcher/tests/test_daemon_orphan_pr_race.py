"""Unit tests for the orphan-PR resurrection race fix (#3508).

Covers:
- ``_mark_agent_resurrected_for_orphan_pr`` UniqueViolation handling
- ``_pick_candidate_issue`` deferral when orphan-PR recovery is pending
- ``_orphan_pr_recovery_pending`` helper logic

Acceptance criteria mapping:
- AC1/AC3: test_resurrection_blocked_by_active_unique_violation_logs_info_no_traceback
- AC2/AC4: test_pick_candidate_defers_when_failed_agent_has_mergeable_pr
- Additional: test_pick_candidate_proceeds_when_no_recoverable_orphan,
              test_pick_candidate_proceeds_when_pr_view_returns_none,
              test_orphan_pr_recovery_pending_uses_lookback_constant

The ``_FakeCursor`` / ``_FakeConnection`` / ``_CapturingLogHandler``
pattern mirrors ``test_daemon_orphan_pr_resurrection.py`` so this file
can evolve independently.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import psycopg  # noqa: E402  — stub installed by conftest.py

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirrors test_daemon_orphan_pr_resurrection.py
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
    logger = logging.getLogger(f"dispatcher.test.orphan_pr_race.{id(tmp_path)}")
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


def _pr_status_green() -> dict[str, Any]:
    """gh pr view payload that classifies as 'green'."""
    return {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
        ],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "head-sha",
        "mergeCommit": None,
    }


# --------------------------------------------------------------------------
# AC1/AC3 — UniqueViolation in _mark_agent_resurrected_for_orphan_pr
# --------------------------------------------------------------------------


class TestUniqueViolationHandling:
    def test_resurrection_blocked_by_active_unique_violation_logs_info_no_traceback(
        self, tmp_path: Path
    ) -> None:
        """AC1+AC3: simulate UniqueViolation from the INSERT.

        Assert:
        - No exception bubbles out.
        - daemon.orphan_pr_resurrect_blocked_by_active is logged at INFO.
        - daemon.orphan_pr_resurrect_failed is NOT logged (no traceback).
        - rollback is called.
        """
        d, conn, handler = _make_daemon(tmp_path)

        # Raise UniqueViolation on the second execute (the INSERT).
        # conftest.py installs psycopg.errors.UniqueViolation as a real
        # Exception subclass (_UniqueViolation) so the daemon's except
        # clause can catch it by type.
        call_count = 0

        def execute_side_effect(sql: str, params: Any = None) -> None:
            nonlocal call_count
            call_count += 1
            conn.cursor_instance.executed.append((sql, params))
            if call_count >= 2:
                raise psycopg.errors.UniqueViolation("duplicate key")

        conn.cursor_instance.execute = execute_side_effect  # type: ignore[method-assign]

        # Must not raise.
        result = d._mark_agent_resurrected_for_orphan_pr("agent-race-test")

        assert result is False, "expected False when UniqueViolation raised"
        # Exactly one rollback.
        assert conn.rollbacks == 1

        # Blocked event logged at INFO.
        blocked = handler.events("orphan_pr_resurrect_blocked_by_active")
        assert blocked, "expected orphan_pr_resurrect_blocked_by_active event"
        assert blocked[0].levelno == logging.INFO, "must be INFO, not ERROR"

        # No orphan_pr_resurrect_failed event — that would include a traceback.
        assert handler.events("orphan_pr_resurrect_failed") == [], (
            "orphan_pr_resurrect_failed must NOT be emitted for UniqueViolation"
        )

    def test_generic_exception_still_logs_resurrect_failed(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: non-UniqueViolation exceptions must still fall
        through to the generic except branch and log orphan_pr_resurrect_failed.
        """
        d, conn, handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            conn.cursor_instance.executed.append((sql, params))
            raise RuntimeError("connection lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]

        result = d._mark_agent_resurrected_for_orphan_pr("agent-generic-error")

        assert result is False
        assert handler.events("orphan_pr_resurrect_failed"), (
            "generic errors must log orphan_pr_resurrect_failed"
        )
        # Must NOT log the blocked event (wrong exception type).
        assert handler.events("orphan_pr_resurrect_blocked_by_active") == []


# --------------------------------------------------------------------------
# AC2/AC4 — _pick_candidate_issue defers when orphan PR is pending
# --------------------------------------------------------------------------


class TestPickCandidateOrphanPrDeferral:
    def test_pick_candidate_defers_when_failed_agent_has_mergeable_pr(
        self, tmp_path: Path
    ) -> None:
        """AC2+AC4: _pick_candidate_issue skips an issue when
        _orphan_pr_recovery_pending returns True.

        Assert:
        - The issue is NOT returned.
        - daemon.claim_deferred_orphan_pr is logged.
        - _issue_author_trusted is NOT called (no subprocess fork wasted).
        """
        d, conn, handler = _make_daemon(tmp_path)

        # Stub cheaply: _issue_already_attempted → False, _issue_in_cooldown → False.
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]

        # _orphan_pr_recovery_pending returns True for issue 3499.
        d._orphan_pr_recovery_pending = lambda n: n == 3499  # type: ignore[method-assign]

        # Sentinel: trust check must NOT be called for the deferred issue.
        trust_calls: list[int] = []

        def fake_trust(n: int) -> bool:
            trust_calls.append(n)
            return True

        d._issue_author_trusted = fake_trust  # type: ignore[method-assign]

        result = d._pick_candidate_issue([3499])

        assert result is None, "deferred issue must not be returned"

        # Deferral event must be logged.
        deferred = handler.events("claim_deferred_orphan_pr")
        assert deferred, "expected claim_deferred_orphan_pr event"
        assert deferred[0].issue_number == 3499

        # Trust check must NOT have been called for 3499.
        assert 3499 not in trust_calls, (
            "_issue_author_trusted must not be called for deferred issue"
        )

    def test_pick_candidate_proceeds_past_deferred_to_next(
        self, tmp_path: Path
    ) -> None:
        """When the first candidate is deferred, the second eligible candidate
        is returned.
        """
        d, conn, handler = _make_daemon(tmp_path)

        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        # Only 3499 is deferred; 3500 is not.
        d._orphan_pr_recovery_pending = lambda n: n == 3499  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]
        # #4211 — stub the shipped-zombie hook so this test doesn't
        # shell out to scripts/check-shipped-pr.sh on the test runner.
        d._issue_already_shipped = lambda n: None  # type: ignore[method-assign]

        result = d._pick_candidate_issue([3499, 3500])

        assert result == 3500


# --------------------------------------------------------------------------
# _orphan_pr_recovery_pending — proceed paths
# --------------------------------------------------------------------------


class TestOrphanPrRecoveryPendingProceedPaths:
    def test_pick_candidate_proceeds_when_no_recoverable_orphan(
        self, tmp_path: Path
    ) -> None:
        """_orphan_pr_recovery_pending returns False when there is no
        recent failed agent row → claim proceeds normally.
        """
        d, conn, handler = _make_daemon(tmp_path)

        # DB returns no row for the issue.
        conn.cursor_instance.fetch_queue.append(None)

        result = d._orphan_pr_recovery_pending(42)

        assert result is False

    def test_pick_candidate_proceeds_when_pr_view_returns_none(
        self, tmp_path: Path
    ) -> None:
        """_orphan_pr_recovery_pending returns False when _fetch_pr_status
        returns None (transient gh failure) — claim must not be blocked.
        """
        d, conn, handler = _make_daemon(tmp_path)

        # DB returns a failed agent row with a PR number.
        conn.cursor_instance.fetch_queue.append(("agent-orphan-x", 9999))
        # Count resurrections: 0 attempts.
        d._count_orphan_pr_resurrections = lambda agent_id: 0  # type: ignore[method-assign]
        # gh fails transiently.
        d._fetch_pr_status = lambda pr: None  # type: ignore[method-assign]

        result = d._orphan_pr_recovery_pending(42)

        assert result is False, "transient gh failure must not block the claim path"

    def test_pick_candidate_proceeds_when_budget_exhausted(
        self, tmp_path: Path
    ) -> None:
        """_orphan_pr_recovery_pending returns False when resurrection budget
        is exhausted — the sweep won't pick it up anyway, so let claim proceed.
        """
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetch_queue.append(("agent-budget-x", 8888))
        # At the cap.
        d._count_orphan_pr_resurrections = lambda agent_id: (  # type: ignore[method-assign]
            daemon.ORPHAN_PR_RESURRECTION_MAX_ATTEMPTS
        )

        # _fetch_pr_status must NOT be called when budget is exhausted.
        pr_calls: list[int] = []

        def boom(pr: int) -> dict[str, Any]:
            pr_calls.append(pr)
            return _pr_status_green()

        d._fetch_pr_status = boom  # type: ignore[method-assign]

        result = d._orphan_pr_recovery_pending(42)

        assert result is False
        assert pr_calls == [], "_fetch_pr_status must not be called past budget"

    def test_orphan_pr_recovery_pending_returns_true_when_green(
        self, tmp_path: Path
    ) -> None:
        """Happy path: recent failed agent + green PR → returns True."""
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetch_queue.append(("agent-green-y", 7777))
        d._count_orphan_pr_resurrections = lambda agent_id: 0  # type: ignore[method-assign]
        d._fetch_pr_status = lambda pr: _pr_status_green()  # type: ignore[method-assign]

        result = d._orphan_pr_recovery_pending(42)

        assert result is True


# --------------------------------------------------------------------------
# SQL shape — lookback constant passed as parameter
# --------------------------------------------------------------------------


class TestOrphanPrRecoveryPendingSqlShape:
    def test_orphan_pr_recovery_pending_uses_lookback_constant(
        self, tmp_path: Path
    ) -> None:
        """The SELECT in _orphan_pr_recovery_pending must pass
        ORPHAN_PR_RESURRECTION_LOOKBACK as a bound parameter (not inlined
        into the SQL string).
        """
        d, conn, handler = _make_daemon(tmp_path)

        # No row — we only care about the SQL shape.
        conn.cursor_instance.fetch_queue.append(None)

        d._orphan_pr_recovery_pending(99)

        # The last SELECT must include the lookback constant in params.
        selects = [
            e for e in conn.cursor_instance.executed if "ended_at > now()" in e[0]
        ]
        assert selects, "expected a SELECT with ended_at > now() - %s"
        sql, params = selects[-1]
        assert daemon.ORPHAN_PR_RESURRECTION_LOOKBACK in params, (
            "ORPHAN_PR_RESURRECTION_LOOKBACK must be passed as a parameter"
        )
