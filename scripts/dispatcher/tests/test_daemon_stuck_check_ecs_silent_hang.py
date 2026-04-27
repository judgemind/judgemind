"""Regression tests for the ECS-mode silent-hang reaper (#3656).

Verifies that :meth:`~dispatcher.daemon.DispatcherDaemon._check_stuck_agents`
now reaps ECS-mode agents whose phase has exceeded its threshold instead
of silently leaving them as zombie cap-slot pins.

Pre-#3656 the per-phase stuck-timeout SELECT excluded ``execution_mode =
'ecs'`` (issue #3158), under the assumption that a hung ECS task would
surface as a STOPPED transition reaped by ``_reap_completed_agent_tasks``.
That assumption is wrong when the agent-runner container's ``bash`` is
alive while a child ``git push`` blocks indefinitely on TCP retry —
observed 2026-04-27 on agent ``2ff6e282`` (#3608) for 16+ minutes
before manual ``aws ecs stop-task``.

Five test cases:

* **ECS positive — over threshold flagged as silent_hang** — an ECS
  agent at elapsed 1100s on phase ``push_and_pr`` (default 600s
  threshold) is reaped: ``_force_stop_ecs_task`` is called with the
  agent's ``agent_task_arn``, a ``dispatcher.failures`` row is written
  with ``category='agent_silent_hang'``, the agent row is flipped to
  ``status='failed'``, and the ``daemon.agent_silent_hang_reaped``
  event is emitted. NO retry marker is enqueued — the diagnoser owns
  that decision.
* **ECS negative — under threshold not flagged** — an ECS agent at
  elapsed 100s on the same phase is NOT flagged.
* **ECS no task_arn — still routes through failure path** — defensive
  behavior: a hung ECS row with ``agent_task_arn=NULL`` (rare —
  pre-launch crash) still gets a failure row written. The
  ``_force_stop_ecs_task`` call no-ops cleanly.
* **Subprocess regression — over-threshold subprocess still flagged
  with stuck_timeout** — confirm the existing subprocess path is
  unchanged. A retry marker is enqueued, category is still
  ``stuck_timeout``, and ``_force_stop_ecs_task`` is NOT called.
* **Mixed batch — both modes coexist in one tick** — one ECS + one
  subprocess agent both over threshold; both are flagged but with
  different categories and routing.

Fixture style mirrors ``test_daemon_stuck_check_operational.py``.
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


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirrors test_daemon_stuck_check_operational.py
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
    logger = logging.getLogger(f"dispatcher.test.stuck_ecs_hang.{id(tmp_path)}")
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
        # Provide a non-empty cluster ARN so _force_stop_ecs_task
        # actually attempts the stop_task call (else it short-circuits
        # and we can't assert the call happened).
        ecs_cluster_arn="arn:aws:ecs:us-west-2:155326049300:cluster/judgemind-dispatcher",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    # Stub the ECS client so _force_stop_ecs_task records a call without
    # hitting the network.
    d._ecs_client = MagicMock()
    return d, conn, handler


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestEcsSilentHangReaper:
    """Issue #3656 — ECS-mode agents are reaped on phase-stuck threshold."""

    def test_ecs_agent_over_threshold_reaped_as_silent_hang(
        self, tmp_path: Path
    ) -> None:
        """ECS agent at elapsed 2000s on push_and_pr (1800s default) is reaped.

        Asserts:
        * ``_force_stop_ecs_task`` issues an ``ecs:StopTask`` call
          against the recorded ``agent_task_arn``.
        * ``dispatcher.failures`` row is written with
          ``category='agent_silent_hang'``.
        * Agent row is flipped to ``status='failed'`` (via
          ``_handle_agent_failure`` → ``_mark_agent_terminal``).
        * ``daemon.agent_silent_hang_reaped`` log event is emitted.
        * NO retry marker is enqueued (diagnoser owns the decision).
        """
        d, conn, handler = _make_daemon(tmp_path)

        # Seeded row: ECS agent on push_and_pr, elapsed 2000s.
        # ``push_and_pr`` is not in STUCK_TIMEOUT_SECONDS_BY_PHASE so
        # the threshold is the global STUCK_TIMEOUT_SECONDS default
        # (1800s / 30 min). Mirrors the 2026-04-27 incident shape
        # (#3608 / agent 2ff6e282) where the actual hang lasted >75 min.
        task_arn = (
            "arn:aws:ecs:us-west-2:155326049300:task/"
            "judgemind-dispatcher/9d333e50bb3c4653b4ba8eb6506f9d13"
        )
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "2ff6e282-a431-493c-bc3e-b899d4c92ca1",
                    3608,
                    "push_and_pr",
                    2000.0,  # 2000s > 1800s default threshold
                    "ecs",
                    task_arn,
                ),
            ],
        ]
        # Order of fetchone calls inside _check_stuck_agents (ECS branch
        # via _handle_agent_failure → _write_failure → _mark_agent_terminal):
        # (1) stuck_timeout_s_by_phase override read — returns None.
        # (2) failure_id from _write_failure RETURNING.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override unset
            (200,),  # failure_id from _write_failure RETURNING
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # ASSERT 1: ecs:StopTask was issued for the recorded ARN.
        assert d._ecs_client is not None
        d._ecs_client.stop_task.assert_called_once()
        call_kwargs = d._ecs_client.stop_task.call_args.kwargs
        assert call_kwargs["task"] == task_arn
        assert call_kwargs["cluster"] == d._cfg.ecs_cluster_arn

        # ASSERT 2: dispatcher.failures row written with the new category.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        params = failure_inserts[0][1]
        assert params[0] == "2ff6e282-a431-493c-bc3e-b899d4c92ca1"
        assert params[1] == daemon.FAILURE_CATEGORY_AGENT_SILENT_HANG

        # ASSERT 3: agent flipped to 'failed' (via _handle_agent_failure
        # → _mark_agent_terminal which uses status='failed').
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert terminal_updates

        # ASSERT 4: daemon.agent_silent_hang_reaped event was emitted.
        reaped = handler.events("agent_silent_hang_reaped")
        assert reaped, "expected agent_silent_hang_reaped log event"
        assert reaped[0].agent_id == "2ff6e282-a431-493c-bc3e-b899d4c92ca1"  # type: ignore[attr-defined]
        assert reaped[0].agent_task_arn == task_arn  # type: ignore[attr-defined]
        assert reaped[0].phase == "push_and_pr"  # type: ignore[attr-defined]

        # ASSERT 5: NO retry marker for this ECS row — the diagnoser
        # owns the retry/escalate decision via the silent_hang category
        # being in TIER_2_FIRST_OCCURRENCE_CATEGORIES.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == [], (
            "ECS silent-hang must not enqueue a retry marker — diagnoser routes"
        )

        # ASSERT 6: agent_failure_routed event from _handle_agent_failure
        # (sanity that the unified routing path was hit).
        routed = handler.events("agent_failure_routed")
        assert routed
        assert routed[0].category == daemon.FAILURE_CATEGORY_AGENT_SILENT_HANG  # type: ignore[attr-defined]

    def test_ecs_agent_under_threshold_not_flagged(self, tmp_path: Path) -> None:
        """ECS agent at elapsed 600s on push_and_pr (1800s default) is NOT flagged.

        Confirms the threshold guard fires before the StopTask + failure
        write so an in-progress ECS agent is not preempted.
        """
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-ecs-fast",
                    3656,
                    "push_and_pr",
                    600.0,  # well below 1800s default threshold
                    "ecs",
                    "arn:aws:ecs:us-west-2:155326049300:task/cluster/abc",
                ),
            ],
        ]
        conn.cursor_instance.fetch_queue = [None]

        flagged = d._check_stuck_agents()
        assert flagged == 0

        # No StopTask call.
        assert d._ecs_client is not None
        d._ecs_client.stop_task.assert_not_called()

        # No failure row.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts == []

        # No silent-hang event.
        assert handler.events("agent_silent_hang_reaped") == []

    def test_ecs_agent_with_null_task_arn_still_routes_failure(
        self, tmp_path: Path
    ) -> None:
        """ECS row with agent_task_arn=NULL still gets a failure row.

        Defensive case: a hung ECS agent that crashed before
        ``_launch_agent_ecs_task`` could persist its ARN. We can't
        StopTask it, but we still want the row off the cap-slot count
        with a failure routed through the diagnoser.
        """
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-ecs-no-arn",
                    3656,
                    "push_and_pr",
                    2000.0,  # > 1800s default threshold
                    "ecs",
                    None,  # missing ARN
                ),
            ],
        ]
        conn.cursor_instance.fetch_queue = [
            None,
            (201,),  # failure_id RETURNING
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # _force_stop_ecs_task short-circuits on missing ARN; no
        # ``stop_task`` call fires.
        assert d._ecs_client is not None
        d._ecs_client.stop_task.assert_not_called()

        # But the failure row + log event + terminal flip still happen.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_AGENT_SILENT_HANG

        reaped = handler.events("agent_silent_hang_reaped")
        assert reaped
        assert reaped[0].agent_task_arn is None  # type: ignore[attr-defined]

    def test_subprocess_agent_over_threshold_still_uses_stuck_timeout(
        self, tmp_path: Path
    ) -> None:
        """Subprocess regression — the existing path is unchanged.

        Confirms that the #3656 SELECT change (dropping the
        ``<> 'ecs'`` filter and adding execution_mode + task_arn
        columns) does not regress the subprocess-mode behavior.
        """
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-subproc",
                    3656,
                    "push_and_pr",
                    2000.0,  # > 1800s default threshold
                    "subprocess",
                    None,
                ),
            ],
        ]
        # Subprocess path's fetch sequence (unchanged from the
        # pre-#3656 test fixtures):
        # (1) stuck_timeout_s_by_phase override — None.
        # (2) failure_id RETURNING.
        # (3) _has_prior_stuck_timeout_in_window — None (first occurrence).
        # (4) prior retry-marker count.
        # (5) backoff schedule.
        conn.cursor_instance.fetch_queue = [
            None,
            (300,),
            None,
            (0,),
            ("[60,300,900]",),
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # NO StopTask call for subprocess.
        assert d._ecs_client is not None
        d._ecs_client.stop_task.assert_not_called()

        # No silent-hang event for subprocess.
        assert handler.events("agent_silent_hang_reaped") == []

        # Subprocess hits the existing failure_detected event with
        # stuck_timeout category.
        detected = handler.events("failure_detected")
        assert detected
        assert detected[0].category == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT  # type: ignore[attr-defined]

        # And the existing retry marker IS enqueued.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        assert marker_inserts[0][1][1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT

    def test_mixed_batch_ecs_and_subprocess_routed_correctly(
        self, tmp_path: Path
    ) -> None:
        """One ECS + one subprocess in the same scan — distinct routing.

        The Python loop in ``_check_stuck_agents`` must dispatch each
        candidate to the right branch independently. A single bad ECS
        StopTask call must not stall the subprocess flag (and vice
        versa).
        """
        d, conn, handler = _make_daemon(tmp_path)

        ecs_arn = "arn:aws:ecs:us-west-2:155326049300:task/cluster/ecs1"
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-mixed-ecs",
                    3656,
                    "push_and_pr",
                    2000.0,  # > 1800s default
                    "ecs",
                    ecs_arn,
                ),
                (
                    "agent-mixed-sub",
                    3656,
                    "summary",  # 6000s threshold (#2885)
                    7000.0,  # > 6000s
                    "subprocess",
                    None,
                ),
            ],
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override
            # ECS agent failure write (first):
            (400,),
            # subprocess agent failure write:
            (401,),
            None,  # _has_prior_stuck_timeout_in_window
            (0,),  # prior retry markers
            ("[60,300,900]",),  # backoff schedule
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 2

        # ECS got StopTask, subprocess didn't.
        assert d._ecs_client is not None
        assert d._ecs_client.stop_task.call_count == 1
        d._ecs_client.stop_task.assert_called_with(
            cluster=d._cfg.ecs_cluster_arn,
            task=ecs_arn,
            reason="operator_force_stop",
        )

        # Two failure rows — one of each category.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 2
        categories = {row[1][1] for row in failure_inserts}
        assert categories == {
            daemon.FAILURE_CATEGORY_AGENT_SILENT_HANG,
            daemon.FAILURE_CATEGORY_STUCK_TIMEOUT,
        }

        # Exactly one retry marker (for the subprocess).
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        assert marker_inserts[0][1][0] == "agent-mixed-sub"

        # Both events fired exactly once.
        assert len(handler.events("agent_silent_hang_reaped")) == 1
        assert len(handler.events("failure_detected")) == 1

    def test_silent_hang_category_is_tier_2_first_occurrence(self) -> None:
        """The new category routes through the diagnoser on first hit.

        Sanity check that ``FAILURE_CATEGORY_AGENT_SILENT_HANG`` is in
        :data:`TIER_2_FIRST_OCCURRENCE_CATEGORIES` so
        ``_find_diagnoser_candidates`` picks it up immediately rather
        than waiting for a recurrence — the operator wants the
        diagnoser to inspect the last known phase + log tail before
        deciding retry vs. escalate.
        """
        assert (
            daemon.FAILURE_CATEGORY_AGENT_SILENT_HANG
            in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
