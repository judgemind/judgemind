"""Unit tests for the orphan-PR resurrection sweep (#3399).

Covers :meth:`DispatcherDaemon._resurrect_orphan_pr_agents` and its
helpers ``_list_orphan_pr_failed_agents``,
``_count_orphan_pr_resurrections``, and
``_mark_agent_resurrected_for_orphan_pr``.

The acceptance criteria from issue #3399:

1. **Happy path** — failed agent + open mergeable green PR → flipped
   to ``status='running' AND phase='awaiting_ci'``, audit row written
   to ``dispatcher.phase_transitions``, ``agent_resurrected_for_orphan_pr``
   log event fired.
2. **Closed/merged PR** (mergeable != 'MERGEABLE') → skipped with
   reason ``rollup_state_red`` (the classifier rejects non-MERGEABLE).
3. **Dirty PR** (mergeStateStatus != 'CLEAN') → skipped with reason
   ``rollup_state_red``.
4. **CI still pending** → skipped with reason ``rollup_state_pending``.
5. **Agent older than 24h** → not even fetched (excluded by SELECT).
6. **Already resurrected 3 times** → skipped with reason
   ``budget_exhausted``.
7. **Sweep wired into supervisor_tick** — orphan_pr_resurrected
   counter surfaces in the tick's return dict.
8. **Sweep skipped when rate-limit flag is set** — the supervisor
   skips the sweep alongside the advance pass.

All external calls (``gh pr view``, psycopg) are mocked — no real
GitHub / DB writes.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirror the test_daemon_phase3b / merge_auto_unstick
# fixtures so this test file can evolve independently.
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
    logger = logging.getLogger(f"dispatcher.test.orphan_pr.{id(tmp_path)}")
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


def _pr_status_dirty() -> dict[str, Any]:
    """gh pr view payload with mergeStateStatus=DIRTY (rebase needed)."""
    return {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
        ],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "DIRTY",
        "headRefOid": "head-sha",
        "mergeCommit": None,
    }


def _pr_status_pending() -> dict[str, Any]:
    """gh pr view payload with at least one in-progress check."""
    return {
        "statusCheckRollup": [
            {"status": "IN_PROGRESS", "conclusion": None, "name": "ci-passed"},
        ],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "UNSTABLE",
        "headRefOid": "head-sha",
        "mergeCommit": None,
    }


def _pr_status_closed() -> dict[str, Any]:
    """gh pr view payload for a closed/merged PR (mergeable != 'MERGEABLE')."""
    return {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
        ],
        # GitHub reports CONFLICTING / UNKNOWN for closed/merged PRs in
        # most cases; the classifier just needs anything other than
        # MERGEABLE to bail.
        "mergeable": "CONFLICTING",
        "mergeStateStatus": "DIRTY",
        "headRefOid": "head-sha",
        "mergeCommit": None,
    }


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


class TestConstants:
    def test_lookback_is_24_hours(self) -> None:
        assert daemon.ORPHAN_PR_RESURRECTION_LOOKBACK == timedelta(hours=24)

    def test_max_attempts_is_three(self) -> None:
        # Issue #3399 spec: "don't resurrect more than 2-3 times per
        # agent_id". 3 keeps us at the upper bound.
        assert daemon.ORPHAN_PR_RESURRECTION_MAX_ATTEMPTS == 3

    def test_phase_marker_constant(self) -> None:
        assert daemon.PHASE_RESURRECTED_FOR_ORPHAN_PR == "resurrected_for_orphan_pr"


# --------------------------------------------------------------------------
# _resurrect_orphan_pr_agents — happy path: green mergeable PR
# --------------------------------------------------------------------------


class TestResurrectionHappyPath:
    def test_failed_agent_with_mergeable_green_pr_is_resurrected(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Stub the SELECT and the per-agent count query through
        # fetchall_queue / fetch_queue. Both UPDATE+INSERT for the
        # resurrection commit then run on the same fake cursor.
        agent_id = "agent-7f656b9f"
        pr_number = 3391
        issue_number = 3375
        conn.cursor_instance.fetchall_queue.append(
            [(agent_id, pr_number, issue_number)]
        )
        # _count_orphan_pr_resurrections fetchone — 0 prior attempts.
        conn.cursor_instance.fetch_queue.append((0,))

        # Stub _fetch_pr_status_batch to return the canonical green
        # payload for every requested PR (#3407 batched-fetch). PR
        # #3391 is the live test case from the issue body.
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_green() for pr in prs
        }

        n = d._resurrect_orphan_pr_agents()

        assert n == 1, "expected one row resurrected"

        # The resurrection success event fired with the right shape.
        events = handler.events("agent_resurrected_for_orphan_pr")
        assert events, "expected agent_resurrected_for_orphan_pr event"
        assert events[0].agent_id == agent_id
        assert events[0].pr_number == pr_number
        assert events[0].issue_number == issue_number
        assert events[0].past_attempts == 0
        assert events[0].new_attempts == 1

        # The UPDATE flipped status='running' phase='awaiting_ci' and
        # forced execution_mode='subprocess' so the ECS short-circuit
        # in _advance_running_agents doesn't skip the row.
        update_calls = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert update_calls, "expected agents UPDATE"
        sql, params = update_calls[-1]
        assert "status = 'running'" in sql
        assert "phase = 'awaiting_ci'" in sql
        assert "execution_mode = 'subprocess'" in sql
        assert "ended_at = NULL" in sql
        assert params == (agent_id,)

        # The phase_transitions INSERT wrote the audit row with the
        # PHASE_RESURRECTED_FOR_ORPHAN_PR marker.
        insert_calls = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert insert_calls, "expected phase_transitions INSERT"
        _, insert_params = insert_calls[-1]
        assert insert_params == (agent_id, daemon.PHASE_RESURRECTED_FOR_ORPHAN_PR)


# --------------------------------------------------------------------------
# _resurrect_orphan_pr_agents — negative paths
# --------------------------------------------------------------------------


class TestResurrectionNegativePaths:
    def test_closed_or_unmergeable_pr_is_skipped(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([("agent-closed", 999, 100)])
        conn.cursor_instance.fetch_queue.append((0,))
        # Closed/declined PRs come back as mergeable != 'MERGEABLE'.
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_closed() for pr in prs
        }

        n = d._resurrect_orphan_pr_agents()

        assert n == 0
        # NO resurrection success event.
        assert handler.events("agent_resurrected_for_orphan_pr") == []
        # Skip event fired with rollup_state reason. The canonical
        # _ci_rollup_state treats mergeable=CONFLICTING as 'pending'
        # (transient — GitHub may recompute), so the skip reason is
        # rollup_state_pending. The important thing is we skip.
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped"
        assert skipped[0].reason.startswith("rollup_state_")
        # NO UPDATE.
        update_calls = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert update_calls == []

    def test_dirty_pr_is_skipped(self, tmp_path: Path) -> None:
        # #3431: mergeStateStatus=DIRTY is a true merge conflict, not a
        # transient recompute. The classifier now returns 'red' (routed to
        # fix_conflict), and the orphan sweep maps that to 'conflict' via
        # _reason_to_state, producing reason='rollup_state_conflict'.
        # The orphan is still skipped (operator-owned escalation path), but
        # with a distinct reason that surfaces in CloudWatch vs. pending.
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([("agent-dirty", 888, 200)])
        conn.cursor_instance.fetch_queue.append((0,))
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_dirty() for pr in prs
        }

        n = d._resurrect_orphan_pr_agents()

        assert n == 0
        assert handler.events("agent_resurrected_for_orphan_pr") == []
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped"
        assert skipped[0].reason == "rollup_state_conflict"

    def test_mergeable_unknown_classifies_as_pending_not_red(
        self, tmp_path: Path
    ) -> None:
        """Regression guard for the issue caught during PR #3399 verify
        on PR #3391. GitHub flips mergeable + mergeStateStatus to
        UNKNOWN intermittently while it recomputes (classic refresh
        window). The canonical _ci_rollup_state classifier handles
        this as 'pending' (so the next supervisor tick re-checks),
        not as 'red' (which the legacy classifier did, causing the
        sweep to mis-skip a row that was actually mergeable). The
        switch to transition_from_awaiting_ci closes that gap."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([("agent-unknown", 666, 600)])
        conn.cursor_instance.fetch_queue.append((0,))
        unknown_payload = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
            ],
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
            "headRefOid": "head-sha",
            "mergeCommit": None,
        }
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: unknown_payload for pr in prs
        }

        n = d._resurrect_orphan_pr_agents()

        assert n == 0, "UNKNOWN must NOT count as green"
        # NOT 'red' — that was the legacy bug. Pending means we'll
        # re-check next tick.
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped"
        assert skipped[0].reason == "rollup_state_pending"

    def test_pending_ci_is_skipped(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([("agent-pending", 777, 300)])
        conn.cursor_instance.fetch_queue.append((0,))
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_pending() for pr in prs
        }

        n = d._resurrect_orphan_pr_agents()

        assert n == 0
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped"
        assert skipped[0].reason == "rollup_state_pending"

    def test_pr_view_failure_is_skipped_no_db_write(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([("agent-pr-view-fail", 666, 400)])
        conn.cursor_instance.fetch_queue.append((0,))
        # _fetch_pr_status_batch returns ``{pr: None}`` on transient
        # gh / GraphQL failure (#3407).
        d._fetch_pr_status_batch = lambda prs: {pr: None for pr in prs}  # type: ignore[method-assign]

        n = d._resurrect_orphan_pr_agents()

        assert n == 0
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped"
        assert skipped[0].reason == "pr_view_failed"
        # NO UPDATE — we don't flip a row when we can't see PR state.
        update_calls = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert update_calls == []

    def test_resurrection_budget_exhausted_skipped(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append(
            [("agent-budget-exhausted", 555, 500)]
        )
        # 3 prior resurrections — at the budget cap.
        conn.cursor_instance.fetch_queue.append(
            (daemon.ORPHAN_PR_RESURRECTION_MAX_ATTEMPTS,)
        )

        # Sentinel: _fetch_pr_status_batch must NOT receive the
        # budget-exhausted PR (saves it from the GraphQL alias list).
        called: list[list[int]] = []

        def boom(prs: list[int]) -> dict[int, dict[str, Any] | None]:
            called.append(list(prs))
            return {pr: _pr_status_green() for pr in prs}

        d._fetch_pr_status_batch = boom  # type: ignore[method-assign]

        n = d._resurrect_orphan_pr_agents()

        assert n == 0
        # _fetch_pr_status_batch is either skipped entirely (no
        # eligible candidates) OR receives a list NOT containing
        # the budget-exhausted PR. Either is acceptable.
        for invocation in called:
            assert 555 not in invocation, (
                "budget-exhausted PR must NOT be in batched fetch"
            )
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped"
        assert skipped[0].reason == "budget_exhausted"
        assert skipped[0].past_attempts == daemon.ORPHAN_PR_RESURRECTION_MAX_ATTEMPTS
        assert skipped[0].max_attempts == daemon.ORPHAN_PR_RESURRECTION_MAX_ATTEMPTS

    def test_count_failed_skips_candidate_no_resurrection(self, tmp_path: Path) -> None:
        """When _count_orphan_pr_resurrections returns None (DB error),
        the candidate must be skipped without batch-fetching its PR
        status (no wasted GraphQL call) and without any resurrection
        DB write. A distinct orphan_pr_resurrection_skipped event with
        reason='count_failed' must be logged (#3427, #3407).
        """
        d, conn, handler = _make_daemon(tmp_path)

        # One candidate row in the list.
        conn.cursor_instance.fetchall_queue.append([("agent-count-fail", 444, 600)])

        # Force _count_orphan_pr_resurrections to return None.
        d._count_orphan_pr_resurrections = lambda agent_id: None  # type: ignore[method-assign]

        # Sentinel: _fetch_pr_status_batch must NOT include the
        # count-failed PR.
        pr_status_calls: list[list[int]] = []

        def fetch_pr_sentinel(prs: list[int]) -> dict[int, dict[str, Any] | None]:
            pr_status_calls.append(list(prs))
            return {pr: _pr_status_green() for pr in prs}

        d._fetch_pr_status_batch = fetch_pr_sentinel  # type: ignore[method-assign]

        n = d._resurrect_orphan_pr_agents()

        assert n == 0, "no resurrection expected"
        for invocation in pr_status_calls:
            assert 444 not in invocation, (
                "_fetch_pr_status_batch must NOT include count_failed PR"
            )
        # No resurrection event.
        assert handler.events("agent_resurrected_for_orphan_pr") == []
        # Distinct skip event with count_failed reason.
        skipped = handler.events("orphan_pr_resurrection_skipped")
        assert skipped, "expected orphan_pr_resurrection_skipped event"
        assert skipped[0].reason == "count_failed"
        # No UPDATE issued.
        update_calls = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert update_calls == []

    def test_no_candidates_returns_zero_no_log(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # SELECT returns no rows.
        conn.cursor_instance.fetchall_queue.append([])

        # Sentinel: _fetch_pr_status_batch must NOT be called when
        # there are no candidates.
        called: list[list[int]] = []

        def fetch_sentinel(prs: list[int]) -> dict[int, dict[str, Any] | None]:
            called.append(list(prs))
            return {}

        d._fetch_pr_status_batch = fetch_sentinel  # type: ignore[method-assign]

        n = d._resurrect_orphan_pr_agents()

        assert n == 0
        assert called == [], "_fetch_pr_status_batch must NOT be called"
        assert handler.events("agent_resurrected_for_orphan_pr") == []
        assert handler.events("orphan_pr_resurrection_skipped") == []


# --------------------------------------------------------------------------
# _list_orphan_pr_failed_agents — SELECT shape
# --------------------------------------------------------------------------


class TestListOrphanPrFailedAgents:
    def test_select_filters_status_failed_and_pr_not_null(self, tmp_path: Path) -> None:
        d, conn, _ = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([])

        d._list_orphan_pr_failed_agents()

        sql_executed = [e[0] for e in conn.cursor_instance.executed]
        assert sql_executed, "expected at least one SQL call"
        sql = sql_executed[-1]
        # The SELECT must filter on the three orphan-PR predicates.
        assert "status = 'failed'" in sql
        assert "pr_number IS NOT NULL" in sql
        assert "issue_number IS NOT NULL" in sql
        # Recency filter parameterised by the lookback constant.
        assert "ended_at > now() - %s" in sql
        # Bound by ended_at IS NOT NULL so we never trip on a half-
        # written terminal row.
        assert "ended_at IS NOT NULL" in sql

    def test_select_passes_lookback_constant(self, tmp_path: Path) -> None:
        d, conn, _ = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue.append([])

        d._list_orphan_pr_failed_agents()

        last = conn.cursor_instance.executed[-1]
        # The interval is bound through the parameter list, NOT inlined
        # into the SQL — so the constant is the single source of truth.
        assert last[1] == (daemon.ORPHAN_PR_RESURRECTION_LOOKBACK,)


# --------------------------------------------------------------------------
# Sweep wired into supervisor_tick
# --------------------------------------------------------------------------


class TestSupervisorTickWiring:
    def test_sweep_runs_in_supervisor_tick_and_surfaces_in_summary(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # ── Stubs to neutralize the rest of the tick. ──
        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._gh_rate_skip_active = lambda: False  # type: ignore[method-assign]
        d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._check_diagnoser_circuit_breaker = lambda: None  # type: ignore[method-assign]
        d._reap_diagnoser_subprocesses = lambda: 0  # type: ignore[method-assign]
        d._run_diagnoser_pass = lambda: 0  # type: ignore[method-assign]
        d._scheduled_skills_tick = lambda: {  # type: ignore[method-assign]
            "rows_scanned": 0,
            "fires_succeeded": 0,
            "fires_skipped_cap": 0,
            "fires_skipped_collision": 0,
            "fires_failed": 0,
        }
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        # The sweep itself: stub it directly so we don't have to
        # re-stage all the sub-fetches.
        sweep_calls: list[int] = []

        def fake_sweep() -> int:
            sweep_calls.append(1)
            return 2  # arbitrary positive number

        d._resurrect_orphan_pr_agents = fake_sweep  # type: ignore[method-assign]

        # The smoke-test SELECT in supervisor_tick wants a fetchone result.
        conn.cursor_instance.fetch_queue.append((0,))

        summary = d.supervisor_tick()

        # Sweep was invoked.
        assert sweep_calls == [1], "expected sweep called once per tick"
        # Counter surfaces in the return dict for cron/CloudWatch.
        assert summary["orphan_pr_resurrected"] == 2
        # Counter surfaces in the supervisor_tick log event.
        ticks = handler.events("supervisor_tick")
        assert ticks, "expected supervisor_tick log event"
        assert ticks[0].orphan_pr_resurrected == 2

    def test_sweep_skipped_when_rate_limit_active(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        # Rate-limit flag IS active — sweep + advance both skip.
        d._gh_rate_skip_active = lambda: True  # type: ignore[method-assign]
        d._gh_rate_skip_until = datetime.now(UTC)  # type: ignore[attr-defined]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._check_diagnoser_circuit_breaker = lambda: None  # type: ignore[method-assign]
        d._reap_diagnoser_subprocesses = lambda: 0  # type: ignore[method-assign]
        d._run_diagnoser_pass = lambda: 0  # type: ignore[method-assign]
        d._scheduled_skills_tick = lambda: {  # type: ignore[method-assign]
            "rows_scanned": 0,
            "fires_succeeded": 0,
            "fires_skipped_cap": 0,
            "fires_skipped_collision": 0,
            "fires_failed": 0,
        }
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        sweep_calls: list[int] = []

        def fake_sweep() -> int:
            sweep_calls.append(1)
            return 999

        d._resurrect_orphan_pr_agents = fake_sweep  # type: ignore[method-assign]

        conn.cursor_instance.fetch_queue.append((0,))

        summary = d.supervisor_tick()

        # Sweep NOT called — same skip-when-rate-limited policy as the
        # advance pass.
        assert sweep_calls == [], "sweep must be skipped when rate-limit flag is set"
        assert summary["orphan_pr_resurrected"] == 0


# --------------------------------------------------------------------------
# _count_orphan_pr_resurrections + _mark_agent_resurrected — DB error paths
# --------------------------------------------------------------------------


class TestAwaitingCiRedWorktreeMissingGuard:
    """The defensive guard in ``_advance_awaiting_ci`` that catches a
    resurrected agent whose worktree no longer exists, and whose PR
    has flipped from green-at-sweep to red-at-tick (#3399)."""

    def test_red_with_missing_worktree_marks_failed_no_fix_ci_spawn(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # PR fetch returns a 'red' rollup (mergeable but with a failed
        # check) — the classifier returns 'red'/'fix_ci'.
        red_payload = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "ci-passed"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
            "headRefOid": "head-sha",
            "mergeCommit": None,
        }
        d._fetch_pr_status = lambda pr: red_payload  # type: ignore[method-assign]

        # Sentinel: _run_fix_ci must NOT be called when the worktree
        # is missing — that's the defensive guard #3399 adds.
        fix_ci_calls: list[tuple[str, int]] = []

        def fake_fix_ci(agent: dict[str, Any], pr_status: dict[str, Any]) -> None:
            fix_ci_calls.append((agent["agent_id"], agent["pr_number"]))

        d._run_fix_ci = fake_fix_ci  # type: ignore[method-assign]

        # And _mark_agent_terminal must be called with status='failed'.
        terminal_calls: list[dict[str, Any]] = []

        def fake_terminal(agent_id: str, **kwargs: Any) -> None:
            terminal_calls.append({"agent_id": agent_id, **kwargs})

        d._mark_agent_terminal = fake_terminal  # type: ignore[method-assign]

        # Worktree path that does NOT exist on disk — the resurrected
        # agent's original worktree was cleaned up at terminal time.
        agent = {
            "agent_id": "agent-resurrected-then-red",
            "issue_number": 3399,
            "phase": "awaiting_ci",
            "pr_number": 3391,
            "worktree_path": str(tmp_path / "does-not-exist"),
            "retries_used": 0,
        }

        d._advance_awaiting_ci(agent)

        # Defensive event fired with the right shape.
        events = handler.events("awaiting_ci_red_worktree_missing")
        assert events, "expected awaiting_ci_red_worktree_missing event"
        assert events[0].agent_id == "agent-resurrected-then-red"
        assert events[0].pr_number == 3391

        # NO fix-ci spawn.
        assert fix_ci_calls == [], "fix-ci must not run when worktree is gone"

        # Marked failed cleanly.
        assert len(terminal_calls) == 1
        assert terminal_calls[0]["status"] == "failed"
        assert terminal_calls[0]["phase"] == "awaiting_ci"

    def test_red_with_existing_worktree_still_runs_fix_ci(self, tmp_path: Path) -> None:
        """Regression guard — the defensive check must NOT short-circuit
        normal red-CI handling for live agents whose worktree is intact.
        """
        d, conn, handler = _make_daemon(tmp_path)

        red_payload = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "ci-passed"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
            "headRefOid": "head-sha",
            "mergeCommit": None,
        }
        d._fetch_pr_status = lambda pr: red_payload  # type: ignore[method-assign]

        fix_ci_calls: list[tuple[str, int]] = []
        d._run_fix_ci = lambda agent, ps: fix_ci_calls.append(  # type: ignore[method-assign]
            (agent["agent_id"], agent["pr_number"])
        )

        # tmp_path EXISTS on disk — the worktree is intact.
        agent = {
            "agent_id": "agent-live-red",
            "issue_number": 100,
            "phase": "awaiting_ci",
            "pr_number": 200,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }

        d._advance_awaiting_ci(agent)

        # Defensive event must NOT fire.
        assert handler.events("awaiting_ci_red_worktree_missing") == []
        # fix-ci runs as normal.
        assert fix_ci_calls == [("agent-live-red", 200)]


class TestDbErrorPaths:
    def test_count_returns_none_on_db_error(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("connection lost")

        conn.cursor_instance.execute = boom  # type: ignore[assignment]

        n = d._count_orphan_pr_resurrections("agent-x")

        assert n is None
        # Failure event logged so CloudWatch captures the issue.
        assert handler.events("orphan_pr_count_failed")

    def test_mark_returns_false_on_db_error(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("write error")

        conn.cursor_instance.execute = boom  # type: ignore[assignment]

        ok = d._mark_agent_resurrected_for_orphan_pr("agent-x")

        assert ok is False
        assert handler.events("orphan_pr_resurrect_failed")

    def test_list_returns_empty_on_db_error(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("select fail")

        conn.cursor_instance.execute = boom  # type: ignore[assignment]

        rows = d._list_orphan_pr_failed_agents()

        assert rows == []
        assert handler.events("orphan_pr_list_failed")


# --------------------------------------------------------------------------
# Issue #3407 — batched gh pr view + candidate cap
# --------------------------------------------------------------------------


class TestBatchedFetchInvariants:
    """The orphan-PR resurrection sweep must issue ONE batched fetch
    per supervisor tick instead of N per-PR ``gh pr view`` calls. The
    pre-fix sweep took ~27s/tick at the 24h-lookback default with
    20-30 candidates; one batched GraphQL roundtrip is the surgical
    fix described in the issue body's "fix shape #1".
    """

    def test_constant_max_orphan_pr_batch_size(self) -> None:
        # Candidate cap must exist and be a sensible bound — small
        # enough to keep the GraphQL query under GitHub's 500-node
        # limit (one alias per PR), large enough to absorb the
        # observed 20-30-row 24h-lookback steady state.
        assert daemon.MAX_ORPHAN_PR_BATCH_SIZE >= 20
        assert daemon.MAX_ORPHAN_PR_BATCH_SIZE <= 100

    def test_batched_fetch_called_exactly_once_per_sweep(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        # 5 eligible candidates — the batched contract is "one
        # invocation regardless of N".
        rows = [(f"agent-{i}", 1000 + i, 2000 + i) for i in range(5)]
        conn.cursor_instance.fetchall_queue.append(rows)
        # 5 budget-count fetchones, all 0.
        for _ in range(5):
            conn.cursor_instance.fetch_queue.append((0,))

        invocations: list[list[int]] = []

        def batched_fetch(prs: list[int]) -> dict[int, dict[str, Any] | None]:
            invocations.append(list(prs))
            # All green — every candidate resurrects.
            return {pr: _pr_status_green() for pr in prs}

        d._fetch_pr_status_batch = batched_fetch  # type: ignore[method-assign]

        n = d._resurrect_orphan_pr_agents()

        assert n == 5, "all five candidates should resurrect"
        assert len(invocations) == 1, (
            "batched fetch must be called once per sweep, not N times"
        )
        assert sorted(invocations[0]) == [1000, 1001, 1002, 1003, 1004]

    def test_per_call_fetch_pr_status_not_invoked_in_sweep(
        self, tmp_path: Path
    ) -> None:
        """Sentinel — :meth:`_fetch_pr_status` (the per-call helper)
        must not be reachable from the resurrection sweep after the
        #3407 batched-fetch refactor. Calling the per-call helper
        would spawn one ``gh pr view`` subprocess per candidate, which
        is exactly the failure mode the issue describes."""
        d, conn, _handler = _make_daemon(tmp_path)

        rows = [(f"agent-{i}", 5000 + i, 6000 + i) for i in range(3)]
        conn.cursor_instance.fetchall_queue.append(rows)
        for _ in range(3):
            conn.cursor_instance.fetch_queue.append((0,))

        per_call_invocations: list[int] = []

        def boom_per_call(pr_number: int) -> dict[str, Any] | None:
            per_call_invocations.append(pr_number)
            return _pr_status_green()

        d._fetch_pr_status = boom_per_call  # type: ignore[method-assign]
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_green() for pr in prs
        }

        d._resurrect_orphan_pr_agents()

        assert per_call_invocations == [], (
            "_fetch_pr_status MUST NOT be called from the sweep — "
            "use _fetch_pr_status_batch instead"
        )

    def test_30_candidate_sweep_completes_under_5s(self, tmp_path: Path) -> None:
        """Acceptance criterion #3 from issue #3407: simulate 30
        candidate orphan-PR rows; sweep completes within 5s with
        mocked gh calls.

        The pre-fix sweep was ~27s for ~25 rows (one ``gh pr view``
        per row at ~1s wall-clock). After the refactor a single
        mocked batched fetch returns instantly, so the realistic
        expectation is sub-100ms — but the AC's 5s threshold is the
        operator-visible bound the supervisor tick must stay under.
        """
        import time as _time  # local import to keep test isolation

        d, conn, _handler = _make_daemon(tmp_path)

        n_candidates = 30
        rows = [(f"agent-{i}", 7000 + i, 8000 + i) for i in range(n_candidates)]
        conn.cursor_instance.fetchall_queue.append(rows)
        for _ in range(n_candidates):
            conn.cursor_instance.fetch_queue.append((0,))

        # All-green batched response. The real call would issue one
        # GraphQL HTTP request — about 200-400ms in production. The
        # test mocks it as instant.
        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_green() for pr in prs
        }

        start = _time.monotonic()
        n = d._resurrect_orphan_pr_agents()
        elapsed = _time.monotonic() - start

        assert n == n_candidates, "all 30 candidates should resurrect"
        # 5s threshold from the issue's AC. The actual measured time
        # should be well under 1s; the threshold provides headroom for
        # CI-runner noise without being so loose it permits a regression.
        assert elapsed < 5.0, f"30-candidate sweep took {elapsed:.2f}s — exceeds 5s AC"


class TestBatchCapEnforcement:
    """When the candidate set is larger than
    :data:`MAX_ORPHAN_PR_BATCH_SIZE`, the sweep must drop the excess
    rows (with an audit log line per drop) rather than letting the
    GraphQL alias list grow unboundedly. The SELECT order
    (``ended_at DESC``) keeps the most-recent failures in scope."""

    def test_excess_candidates_emit_batch_cap_exceeded_skip(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # 5 over the cap — these should be skipped with the new reason.
        n_candidates = daemon.MAX_ORPHAN_PR_BATCH_SIZE + 5
        rows = [(f"agent-{i}", 9000 + i, 10000 + i) for i in range(n_candidates)]
        conn.cursor_instance.fetchall_queue.append(rows)
        for _ in range(n_candidates):
            conn.cursor_instance.fetch_queue.append((0,))

        d._fetch_pr_status_batch = lambda prs: {  # type: ignore[method-assign]
            pr: _pr_status_green() for pr in prs
        }

        n = d._resurrect_orphan_pr_agents()

        # Only the first MAX_ORPHAN_PR_BATCH_SIZE rows resurrected.
        assert n == daemon.MAX_ORPHAN_PR_BATCH_SIZE
        # The 5 excess rows each get a skip-event with the new reason.
        skipped = handler.events("orphan_pr_resurrection_skipped")
        cap_skips = [
            s for s in skipped if getattr(s, "reason", None) == "batch_cap_exceeded"
        ]
        assert len(cap_skips) == 5, (
            f"expected 5 batch_cap_exceeded skips, got {len(cap_skips)}"
        )
        # The CloudWatch event carries max_batch_size for operator
        # readability so they can check the cap from the log alone.
        assert cap_skips[0].max_batch_size == daemon.MAX_ORPHAN_PR_BATCH_SIZE

    def test_batched_fetch_receives_at_most_max_batch_size_prs(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        n_candidates = daemon.MAX_ORPHAN_PR_BATCH_SIZE + 10
        rows = [(f"agent-{i}", 11000 + i, 12000 + i) for i in range(n_candidates)]
        conn.cursor_instance.fetchall_queue.append(rows)
        for _ in range(n_candidates):
            conn.cursor_instance.fetch_queue.append((0,))

        invocations: list[list[int]] = []

        def batched_fetch(prs: list[int]) -> dict[int, dict[str, Any] | None]:
            invocations.append(list(prs))
            return {pr: _pr_status_green() for pr in prs}

        d._fetch_pr_status_batch = batched_fetch  # type: ignore[method-assign]

        d._resurrect_orphan_pr_agents()

        assert len(invocations) == 1
        # The batched fetch sees only the first cap-many PRs.
        assert len(invocations[0]) == daemon.MAX_ORPHAN_PR_BATCH_SIZE


# --------------------------------------------------------------------------
# _fetch_pr_status_batch — single subprocess, GraphQL shape, normalization
# --------------------------------------------------------------------------


class TestFetchPrStatusBatch:
    def test_empty_input_returns_empty_dict_no_subprocess(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # Sentinel: no subprocess wrapper invoked when input is empty.
        d._subprocess_with_retry = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("subprocess must NOT be called for empty input")
        )

        result = d._fetch_pr_status_batch([])

        assert result == {}

    def test_single_subprocess_for_many_prs(self, tmp_path: Path) -> None:
        """The whole point of #3407 — one subprocess call regardless
        of PR count."""
        d, _conn, _handler = _make_daemon(tmp_path)

        invocations: list[list[str]] = []

        # Build a synthetic GraphQL response for 25 PRs.
        pr_numbers = list(range(100, 125))

        def fake_run(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            invocations.append(cmd)
            assert event_name == "pr_view_batch", "uses dedicated event name"
            # Build a fake gh api graphql response.
            repo_data = {}
            for n in pr_numbers:
                repo_data[f"pr{n}"] = {
                    "number": n,
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "mergedAt": None,
                    "headRefOid": f"sha-{n}",
                    "mergeCommit": None,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "nodes": [
                                                {
                                                    "__typename": "CheckRun",
                                                    "name": "ci-passed",
                                                    "status": "COMPLETED",
                                                    "conclusion": "SUCCESS",
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    },
                }
            payload = {"data": {"repository": repo_data}}

            class _Result:
                stdout = json.dumps(payload)

            return {"ok": True, "result": _Result(), "attempts": 1}

        d._subprocess_with_retry = fake_run  # type: ignore[method-assign]

        result = d._fetch_pr_status_batch(pr_numbers)

        # ONE subprocess invocation regardless of N.
        assert len(invocations) == 1
        # The cmd is a `gh api graphql` invocation with a query
        # containing one alias per PR.
        cmd = invocations[0]
        assert cmd[:3] == ["gh", "api", "graphql"]
        assert cmd[3] == "-f"
        query = cmd[4].removeprefix("query=")
        for n in pr_numbers:
            assert f"pr{n}: pullRequest(number: {n})" in query

        # Per-PR shape mirrors `_fetch_pr_status` so the existing
        # classifier (transition_from_awaiting_ci) consumes it
        # without changes.
        assert set(result.keys()) == set(pr_numbers)
        for n in pr_numbers:
            payload = result[n]
            assert payload is not None
            assert payload["state"] == "OPEN"
            assert payload["mergeable"] == "MERGEABLE"
            assert payload["mergeStateStatus"] == "CLEAN"
            # CheckRun translated to gh-pr-view shape.
            assert payload["statusCheckRollup"] == [
                {"name": "ci-passed", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ]

    def test_subprocess_failure_returns_all_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # _subprocess_with_retry returns ok=False on exhausted retries.
        d._subprocess_with_retry = lambda *_a, **_k: {  # type: ignore[method-assign]
            "ok": False,
            "reason": "timeout",
            "stderr_tail": "",
            "exit_code": None,
            "attempts": 3,
        }

        result = d._fetch_pr_status_batch([100, 200, 300])

        assert result == {100: None, 200: None, 300: None}

    def test_unparseable_json_returns_all_none(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        class _Result:
            stdout = "{not json"

        d._subprocess_with_retry = lambda *_a, **_k: {  # type: ignore[method-assign]
            "ok": True,
            "result": _Result(),
            "attempts": 1,
        }

        result = d._fetch_pr_status_batch([100, 200])

        assert result == {100: None, 200: None}
        assert handler.events("pr_view_batch_invalid_json")

    def test_missing_repository_data_returns_all_none(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        class _Result:
            # Valid JSON but no data.repository — treated as transient
            # like in _fetch_blocker_titles.
            stdout = json.dumps({"data": {}})

        d._subprocess_with_retry = lambda *_a, **_k: {  # type: ignore[method-assign]
            "ok": True,
            "result": _Result(),
            "attempts": 1,
        }

        result = d._fetch_pr_status_batch([100, 200])

        assert result == {100: None, 200: None}
        assert handler.events("pr_view_batch_missing_data")

    def test_alias_null_for_one_pr_returns_none_for_only_that_pr(
        self, tmp_path: Path
    ) -> None:
        """A specific PR may have been deleted; the alias resolves to
        null while the rest of the batch returns valid data. The
        deleted PR's slot must be ``None``; the others must carry
        their normalized payloads."""
        d, _conn, _handler = _make_daemon(tmp_path)

        class _Result:
            stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "pr100": None,  # deleted
                            "pr200": {
                                "number": 200,
                                "state": "OPEN",
                                "mergeable": "MERGEABLE",
                                "mergeStateStatus": "CLEAN",
                                "mergedAt": None,
                                "headRefOid": "sha-200",
                                "mergeCommit": None,
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "statusCheckRollup": {
                                                    "contexts": {
                                                        "nodes": [
                                                            {
                                                                "__typename": "CheckRun",
                                                                "name": "ci-passed",
                                                                "status": "COMPLETED",
                                                                "conclusion": "SUCCESS",
                                                            }
                                                        ]
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                },
                            },
                        }
                    }
                }
            )

        d._subprocess_with_retry = lambda *_a, **_k: {  # type: ignore[method-assign]
            "ok": True,
            "result": _Result(),
            "attempts": 1,
        }

        result = d._fetch_pr_status_batch([100, 200])

        assert result[100] is None
        assert result[200] is not None
        assert result[200]["state"] == "OPEN"


class TestNormalizeGraphqlPrPayload:
    """Direct tests for the GraphQL-to-gh-pr-view shape normalizer.
    Both CheckRun and StatusContext discriminator paths must round-
    trip through the same gh-pr-view-shaped output the existing
    classifier consumes."""

    def test_check_run_node_normalized(self) -> None:
        node = {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "mergedAt": None,
            "headRefOid": "head-sha",
            "mergeCommit": None,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "CheckRun",
                                            "name": "ci-passed",
                                            "status": "COMPLETED",
                                            "conclusion": "SUCCESS",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                ]
            },
        }
        out = daemon._normalize_graphql_pr_payload(node)
        assert out["state"] == "OPEN"
        assert out["mergeable"] == "MERGEABLE"
        assert out["mergeStateStatus"] == "CLEAN"
        assert out["mergeCommit"] is None
        assert out["statusCheckRollup"] == [
            {"name": "ci-passed", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ]

    def test_status_context_node_normalized(self) -> None:
        node = {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "mergedAt": None,
            "headRefOid": "head-sha",
            "mergeCommit": None,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "StatusContext",
                                            "context": "vercel/preview",
                                            "state": "SUCCESS",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                ]
            },
        }
        out = daemon._normalize_graphql_pr_payload(node)
        assert out["statusCheckRollup"] == [
            {"context": "vercel/preview", "state": "SUCCESS"}
        ]

    def test_merge_commit_oid_normalized(self) -> None:
        node = {
            "state": "MERGED",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "mergedAt": "2026-04-26T03:00:00Z",
            "headRefOid": "head-sha",
            "mergeCommit": {"oid": "merge-sha"},
            "commits": {"nodes": []},
        }
        out = daemon._normalize_graphql_pr_payload(node)
        assert out["mergeCommit"] == {"oid": "merge-sha"}
        assert out["state"] == "MERGED"
        assert out["mergedAt"] == "2026-04-26T03:00:00Z"
        # Empty commits.nodes → empty rollup, NOT a crash.
        assert out["statusCheckRollup"] == []

    def test_missing_fields_default_to_none_or_empty(self) -> None:
        # Defensive — GraphQL may omit fields under transient errors.
        out = daemon._normalize_graphql_pr_payload({})
        assert out["state"] is None
        assert out["mergeable"] is None
        assert out["mergeStateStatus"] is None
        assert out["mergedAt"] is None
        assert out["headRefOid"] is None
        assert out["mergeCommit"] is None
        assert out["statusCheckRollup"] == []
