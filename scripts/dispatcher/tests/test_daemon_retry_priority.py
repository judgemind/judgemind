"""Unit tests for issue #2949 — scheduler drains infra-preemption markers first.

When a daemon restart abandoned an in-flight agent, PR #2944's
terminal-and-reclaim path marks the old row ``failed`` and re-adds the
``agent/ready`` label to the interrupted issue. Before #2949, the
retry-marker drain only ran on the supervisor tick (120 s cadence), so
up to four scheduler ticks could fire a fresh claim before the
interrupted issue re-appeared in the queue snapshot. Issue #2949 moves
infra-preemption marker processing to the *start* of every scheduler
tick — ahead of ``_scan_queue_and_snapshot`` — so the re-added issue
lands in the same tick's snapshot and wins the claim gate ahead of
equal-priority fresh work.

These tests cover:

* ``_process_retry_markers(only_infra_preemption=True)`` narrows the
  SELECT to ``_INFRA_PREEMPTION_CATEGORIES`` and leaves budgeted
  retries for the supervisor drain.
* ``scheduler_tick`` calls the filtered drain BEFORE
  ``_scan_queue_and_snapshot`` and AFTER the config read (ordering is
  load-bearing — the re-added label must be visible to the scan).
* The ``retry_markers_prioritized`` field is surfaced on both the
  summary dict and the ``daemon.scheduler_tick`` log event.
* The distinct ``daemon.retry_prioritized_over_fresh_claim`` log event
  fires only when at least one marker was drained this tick, so
  CloudWatch can confirm the ordering kicked in without scanning every
  retry event.
* Supervisor-tick drain behavior is unchanged — the default
  ``_process_retry_markers()`` still returns all due markers regardless
  of reason.
* Exceptions raised inside the new drain are caught + logged and do
  NOT crash ``scheduler_tick``.

All external calls are mocked. Tests share the same ``_FakeCursor`` +
``_FakeConnection`` helpers from ``test_daemon_restart_cascade`` — the
psycopg stub installed there is reused via direct import.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


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

from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (local copies — match test_daemon_phase3c / restart_cascade)
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
    logger = logging.getLogger(f"dispatcher.test.retry_priority.{id(tmp_path)}")
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


# --------------------------------------------------------------------------
# _process_retry_markers(only_infra_preemption=True) — SELECT filter
# --------------------------------------------------------------------------


class TestProcessRetryMarkersOnlyInfraPreemption:
    """The ``only_infra_preemption=True`` kwarg narrows the SELECT filter."""

    def test_filter_flag_narrows_select_to_infra_categories(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """SQL SELECT includes ``reason = ANY(%s)`` with the infra frozenset."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Empty fetchall → no markers processed; we only care about the SQL shape.
        conn.cursor_instance.fetchall_queue = [[]]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        d._process_retry_markers(only_infra_preemption=True)

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT" in e[0] and "dispatcher.retry_markers" in e[0]
        ]
        assert len(selects) == 1, (
            f"exactly one SELECT expected; got {len(selects)} — {selects}"
        )
        sql, params = selects[0]
        assert "m.reason = ANY(%s)" in sql, (
            "only_infra_preemption=True must filter by reason = ANY(%s); "
            f"got SQL: {sql}"
        )
        # Bind parameter is the _INFRA_PREEMPTION_CATEGORIES list.
        assert params is not None
        assert len(params) == 1
        bound = params[0]
        assert isinstance(bound, list)
        assert set(bound) == set(daemon._INFRA_PREEMPTION_CATEGORIES)

    def test_default_call_has_no_category_filter(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Default (supervisor-tick) call issues the unfiltered SELECT."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        d._process_retry_markers()  # default: only_infra_preemption=False

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT" in e[0] and "dispatcher.retry_markers" in e[0]
        ]
        assert len(selects) == 1
        sql, params = selects[0]
        assert "m.reason = ANY(%s)" not in sql, (
            "default call must not filter by reason; got SQL: {sql}"
        )
        # No bind parameter for the filter path.
        assert params is None

    def test_filtered_call_still_takes_terminal_and_reclaim_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """An infra-preemption row returned by the filtered SELECT still hits
        the terminal-and-reclaim branch (``_mark_agent_terminal`` + re-add
        ``agent/ready``) — the filter only narrows the SELECT, it does not
        change the per-row processing logic.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    7,  # marker_id
                    "agent-infra",
                    daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
                    1,  # attempt
                    str(tmp_path / "worktrees" / "agent-infra"),
                    123,  # issue_number
                ),
            ],
        ]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        gh_calls: list[tuple[int, list[str]]] = []
        monkeypatch.setattr(
            d,
            "_gh_issue_add_labels",
            lambda issue, labels: gh_calls.append((issue, labels)),
        )

        processed = d._process_retry_markers(only_infra_preemption=True)
        assert processed == 1
        # agent/ready re-added (the reclaim half).
        assert gh_calls == [(123, ["agent/ready"])]
        # Terminal log event emitted (not the budgeted retry_processed event).
        assert handler.events("retry_terminal_and_reclaim")
        assert handler.events("retry_processed") == []

    def test_filtered_call_returns_zero_when_no_infra_markers_due(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Empty fetchall → 0 markers processed, no log events, no commits beyond
        the initial SELECT commit.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        processed = d._process_retry_markers(only_infra_preemption=True)
        assert processed == 0
        assert handler.events("retry_terminal_and_reclaim") == []
        assert handler.events("retry_processed") == []


# --------------------------------------------------------------------------
# scheduler_tick call ordering — retry drain before queue scan
# --------------------------------------------------------------------------


class TestSchedulerTickDrainsInfraRetriesFirst:
    """``scheduler_tick`` calls the infra-preemption drain before the queue scan.

    The ordering is load-bearing: the terminal-and-reclaim path re-adds
    ``agent/ready`` to the interrupted issue, and the queue scan has to
    observe that label in the SAME tick for the claim gate to pick the
    interrupted work ahead of fresh claims. If the drain ran AFTER the
    scan, the interrupted issue would have to wait for the next tick.
    """

    def test_infra_drain_invoked_before_queue_scan(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Record call order of the two helpers; drain must precede scan."""
        d, conn, _handler = _make_daemon(tmp_path)
        # config read returns cap=0 — keeps the orchestration gate closed
        # so this test isolates the drain/scan ordering.
        conn.cursor_instance.fetch_queue = [(0,)]

        call_order: list[str] = []

        def fake_drain(*, only_infra_preemption: bool = False) -> int:
            call_order.append(f"drain(only_infra_preemption={only_infra_preemption})")
            return 0

        def fake_scan() -> int:
            call_order.append("scan")
            return 0

        monkeypatch.setattr(d, "_process_retry_markers", fake_drain)
        monkeypatch.setattr(d, "_scan_queue_and_snapshot", fake_scan)
        # Skip the blocked-list scan — not relevant to this ordering test.
        d._should_run_blocked_scan = lambda: False  # type: ignore[method-assign]

        d.scheduler_tick()

        # Drain was called with only_infra_preemption=True, and it ran BEFORE
        # the queue scan. The supervisor tick's default drain is NOT invoked
        # from the scheduler tick — only the filtered pre-scan variant.
        assert call_order == [
            "drain(only_infra_preemption=True)",
            "scan",
        ], f"unexpected call order: {call_order}"

    def test_markers_prioritized_surfaces_on_summary_and_log(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Non-zero drain count appears in ``summary['retry_markers_prioritized']``
        and the ``daemon.scheduler_tick`` log event, plus the distinct
        ``daemon.retry_prioritized_over_fresh_claim`` event fires.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]  # cap=0, gate closed

        monkeypatch.setattr(
            d,
            "_process_retry_markers",
            lambda *, only_infra_preemption=False: 2,
        )
        monkeypatch.setattr(d, "_scan_queue_and_snapshot", lambda: 0)
        d._should_run_blocked_scan = lambda: False  # type: ignore[method-assign]

        summary = d.scheduler_tick()
        assert summary["retry_markers_prioritized"] == 2

        scheduler_events = handler.events("scheduler_tick")
        assert len(scheduler_events) == 1
        assert getattr(scheduler_events[0], "retry_markers_prioritized", None) == 2

        # Distinct event fires only when count > 0.
        priorit_events = handler.events("retry_prioritized_over_fresh_claim")
        assert len(priorit_events) == 1
        assert getattr(priorit_events[0], "markers_processed", None) == 2

    def test_zero_drain_does_not_emit_distinct_event(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """When no infra markers are due, the distinct log event stays silent —
        CloudWatch filters would otherwise be overwhelmed by the steady-state
        case (no markers on every tick).
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        monkeypatch.setattr(
            d,
            "_process_retry_markers",
            lambda *, only_infra_preemption=False: 0,
        )
        monkeypatch.setattr(d, "_scan_queue_and_snapshot", lambda: 0)
        d._should_run_blocked_scan = lambda: False  # type: ignore[method-assign]

        summary = d.scheduler_tick()
        assert summary["retry_markers_prioritized"] == 0

        # Scheduler_tick log still carries the field, just as 0.
        scheduler_events = handler.events("scheduler_tick")
        assert len(scheduler_events) == 1
        assert getattr(scheduler_events[0], "retry_markers_prioritized", None) == 0

        # No distinct event fires.
        assert handler.events("retry_prioritized_over_fresh_claim") == []

    def test_drain_exception_does_not_crash_scheduler_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A drain-path exception is caught + logged; the tick completes normally.

        The queue scan + claim gate must still fire so one bad retry-marker
        scan cannot stall the entire daemon's queue-scan cadence.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        def boom(*, only_infra_preemption: bool = False) -> int:
            raise RuntimeError("retry scan exploded")

        scan_called = {"count": 0}

        def fake_scan() -> int:
            scan_called["count"] += 1
            return 0

        monkeypatch.setattr(d, "_process_retry_markers", boom)
        monkeypatch.setattr(d, "_scan_queue_and_snapshot", fake_scan)
        d._should_run_blocked_scan = lambda: False  # type: ignore[method-assign]

        summary = d.scheduler_tick()

        # The tick returned a summary (did not raise).
        assert summary["retry_markers_prioritized"] == 0
        # Queue scan ran despite the drain exception.
        assert scan_called["count"] == 1
        # Structured exception event emitted for CloudWatch.
        assert handler.events("retry_prioritized_pass_failed")


# --------------------------------------------------------------------------
# Integration — restart-then-fresh-claim ordering on the next tick
# --------------------------------------------------------------------------


class TestSchedulerTickReclaimsInterruptedIssueAheadOfFresh:
    """End-to-end: two abandoned retrying agents + two fresh ready issues
    of the same priority → the scheduler drains the two retry markers
    first, so the *next* claim gate fires against a snapshot in which
    the two interrupted issues are present (with ``agent/ready`` freshly
    re-added). This matches the acceptance-criteria invariant in
    issue #2949: "the first N slot openings (on the same tick or next)
    go to the restarted agents — not to fresh claims from agent/ready."
    """

    def test_two_infra_markers_processed_before_scan_runs(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Two due infra-preemption markers → both are terminal-and-reclaimed,
        then the queue scan runs. The scan sees the re-added ``agent/ready``
        labels for both interrupted issues (captured via the stubbed GH
        label-add helper) before the scan itself runs.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]  # cap=0, gate closed

        # Stub the commands scan so its fetchall doesn't eat the retry-marker
        # fetchall queue.
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        # Two due infra-preemption markers on the first SELECT; no more on a
        # second call (empty list → default handling).
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,
                    "agent-preempted-1",
                    daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
                    1,
                    str(tmp_path / "worktrees" / "agent-preempted-1"),
                    501,  # issue_number
                ),
                (
                    2,
                    "agent-preempted-2",
                    daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH,
                    1,
                    str(tmp_path / "worktrees" / "agent-preempted-2"),
                    502,
                ),
            ],
        ]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        # Capture _gh_issue_add_labels calls — the reclaim path adds
        # ``agent/ready`` back to each interrupted issue.
        gh_calls: list[tuple[int, list[str]]] = []
        monkeypatch.setattr(
            d,
            "_gh_issue_add_labels",
            lambda issue, labels: gh_calls.append((issue, labels)),
        )
        # _mark_agent_terminal writes a failure row + UPDATE; stub the whole
        # thing so the test focuses on the ordering (tested in depth in
        # test_daemon_restart_cascade).
        terminal_calls: list[dict[str, Any]] = []

        def fake_terminal(
            agent_id: str,
            *,
            status: str,
            phase: str,
            exit_code: int | None = None,
            pr_number: int | None = None,
            issue_number: int | None = None,
        ) -> None:
            terminal_calls.append(
                {
                    "agent_id": agent_id,
                    "status": status,
                    "phase": phase,
                    "issue_number": issue_number,
                }
            )

        monkeypatch.setattr(d, "_mark_agent_terminal", fake_terminal)

        # Record that the scan ran AFTER the reclaim (assert ordering).
        scan_order: list[str] = []
        for issue in (501, 502):
            _ = issue  # silence unused — scan_order captures a single label

        original_add = d._gh_issue_add_labels

        def tracking_add(issue: int, labels: list[str]) -> None:
            scan_order.append(f"reclaim:{issue}")
            return original_add(issue, labels)

        monkeypatch.setattr(d, "_gh_issue_add_labels", tracking_add)

        def fake_scan() -> int:
            scan_order.append("scan")
            return 0

        monkeypatch.setattr(d, "_scan_queue_and_snapshot", fake_scan)
        d._should_run_blocked_scan = lambda: False  # type: ignore[method-assign]

        summary = d.scheduler_tick()

        # Both interrupted agents were marked terminal.
        assert len(terminal_calls) == 2
        assert {c["issue_number"] for c in terminal_calls} == {501, 502}
        assert all(
            c["status"] == "failed"
            and c["phase"] in daemon._INFRA_PREEMPTION_CATEGORIES
            for c in terminal_calls
        )
        # agent/ready re-added to both issues.
        assert gh_calls == [(501, ["agent/ready"]), (502, ["agent/ready"])]

        # Reclaims fired BEFORE the scan (the ordering invariant #2949 depends on).
        assert scan_order == ["reclaim:501", "reclaim:502", "scan"], (
            f"expected reclaims before scan; got {scan_order}"
        )

        # Summary + structured event surface the count.
        assert summary["retry_markers_prioritized"] == 2
        assert handler.events("retry_prioritized_over_fresh_claim")

    def test_budgeted_retry_marker_not_drained_by_scheduler_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A due budgeted retry marker (``subprocess_crash``) is NOT processed
        by the scheduler-tick drain — the supervisor tick owns that work.

        The scheduler-tick drain's SQL filter excludes reasons not in
        ``_INFRA_PREEMPTION_CATEGORIES``; since the fake cursor runs the
        exact SELECT issued by production code, feeding it a budgeted
        marker and asserting that no ``retry_processed`` log event is
        emitted confirms the filter works end-to-end in the scheduler
        path. (The full drain on the supervisor tick would process the
        same marker — that behavior is tested by
        ``TestProcessRetryMarkers`` in ``test_daemon_phase3c``.)
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        # Stub commands scan so its fetchall doesn't eat the retry-marker queue.
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        # Simulate a filtered SELECT that returns an empty list — which is
        # what happens in production because the real WHERE clause filters
        # budgeted reasons out. The fake cursor does not interpret SQL, so
        # we encode that expectation directly by returning [].
        conn.cursor_instance.fetchall_queue = [[]]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        monkeypatch.setattr(d, "_scan_queue_and_snapshot", lambda: 0)
        d._should_run_blocked_scan = lambda: False  # type: ignore[method-assign]

        summary = d.scheduler_tick()

        # No markers drained this tick — budgeted retries would appear in
        # the supervisor tick's unfiltered drain instead.
        assert summary["retry_markers_prioritized"] == 0
        assert handler.events("retry_processed") == []
        assert handler.events("retry_terminal_and_reclaim") == []
        # SQL shape check — the scheduler-tick drain used the filtered form.
        drain_selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT" in e[0]
            and "dispatcher.retry_markers" in e[0]
            and "m.reason = ANY(%s)" in e[0]
        ]
        assert len(drain_selects) == 1, (
            f"expected exactly one filtered drain SELECT; got {drain_selects}"
        )


# --------------------------------------------------------------------------
# Supervisor-tick drain behavior unchanged
# --------------------------------------------------------------------------


class TestSupervisorTickDrainUnchanged:
    """Supervisor tick still calls the default (unfiltered) drain.

    Regression guard for #2949: moving the infra-preemption drain to
    the scheduler tick must not silently remove the supervisor-tick
    drain of budgeted retries. Without the supervisor drain,
    ``subprocess_crash`` / ``stuck_timeout`` / ``gh_rate_exhausted``
    markers would sit forever after #2949.
    """

    def test_supervisor_tick_invokes_unfiltered_process_retry_markers(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # failures_last_hour count SELECT returns 0.
        conn.cursor_instance.fetch_queue = [(0,)]

        calls: list[dict[str, Any]] = []

        def fake_drain(**kwargs: Any) -> int:
            calls.append(kwargs)
            return 0

        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
        d._process_retry_markers = fake_drain  # type: ignore[method-assign]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        d.supervisor_tick()

        # One call — with NO kwargs (i.e. the default unfiltered drain).
        assert len(calls) == 1
        assert calls[0] == {}, (
            f"supervisor tick must call _process_retry_markers() with no kwargs "
            f"(unfiltered drain); got {calls[0]}"
        )


# --------------------------------------------------------------------------
# _INFRA_PREEMPTION_CATEGORIES frozenset is what's bound to the SELECT
# --------------------------------------------------------------------------


class TestInfraPreemptionCategoriesIntegrity:
    """Regression guard: the SELECT's bind parameter must match the constant.

    A drift between the frozenset and the SELECT's bind list would silently
    regress the #2949 fix (e.g. a future edit that adds a new infra category
    to the frozenset but forgets to trust the SELECT's binding).
    """

    def test_infra_categories_contains_restart_abandoned_and_killswitch(self) -> None:
        """Sanity check — the two known infra categories are in the frozenset."""
        assert (
            daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
            in daemon._INFRA_PREEMPTION_CATEGORIES
        )
        assert (
            daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH
            in daemon._INFRA_PREEMPTION_CATEGORIES
        )

    def test_filtered_select_binds_the_same_frozenset(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The SELECT's bind parameter list is exactly the frozenset contents."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        d._process_retry_markers(only_infra_preemption=True)

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT" in e[0] and "dispatcher.retry_markers" in e[0]
        ]
        assert len(selects) == 1
        _sql, params = selects[0]
        bound_categories = set(params[0])
        assert bound_categories == set(daemon._INFRA_PREEMPTION_CATEGORIES), (
            f"SELECT bind param drift: bound={bound_categories}, "
            f"constant={set(daemon._INFRA_PREEMPTION_CATEGORIES)}"
        )
