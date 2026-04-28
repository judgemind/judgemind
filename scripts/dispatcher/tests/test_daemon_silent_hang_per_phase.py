"""Regression tests for the silent-hang-vs-total-runtime threshold split (#3731).

Verifies that :meth:`~dispatcher.daemon.DispatcherDaemon._check_stuck_agents`
distinguishes two independent reap conditions:

1. **Silent-hang reap** — fires when ``now - max(phase_transitions.ts)``
   exceeds ``SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE[phase]`` (e.g. 90 min
   for ralph). Catches agents whose subprocess crashed / hung / OOM'd
   without writing any phase_transitions row.
2. **Total-runtime reap** — fires when ``now - started_at`` exceeds
   ``STUCK_TIMEOUT_SECONDS_BY_PHASE[phase]`` (e.g. 15 hr for ralph).
   Catches genuinely runaway agents that ARE making progress
   (phase_transitions ARE updating) but for too long.

Pre-#3731 the reaper used ``STUCK_TIMEOUT_SECONDS_BY_PHASE[phase]`` for
**both** decisions, comparing it against ``now -
COALESCE(pt.last_ts, started_at)``. That conflation meant a hung ralph
agent (no phase_transitions for hours) had to wait the full 15-hour
absolute-runtime cap before the reaper would notice. Concrete instance:
agent ``9943a31e`` on issue #3663 emitted its last phase_transitions
event at 04:33:54Z, then went silent for 12h while showing
``status=running, phase=ralph``. Operator manually force-stopped at
16:21:46Z. The cap slot was unusable for the full duration.

The fix: two thresholds per phase, evaluated independently. Reap on
EITHER (silent-hang OR total-runtime). The silent-hang threshold is
much tighter (90 min for ralph) because a healthy ralph emits
``ralph_iteration_*`` phase_transitions every few minutes — anything
silent for >90 min is a hung subprocess, not a slow one.

Test cases:

* **Silent-hang fires (silent > threshold, total < threshold)** — ralph
  agent with last phase_transitions.ts 100min ago and total runtime 2h.
  Silent-hang threshold 90min exceeded → reap. Total-runtime 15h NOT
  exceeded — without the split, this agent would NOT have been reaped.
* **Neither fires (silent < threshold, total < threshold)** — ralph
  agent iterating slowly: last ts 50min ago, total runtime 14h. Both
  thresholds under cap. Don't false-positive on slow-but-healthy ralph.
* **Total-runtime fires (total > threshold)** — ralph agent total
  runtime 16h, last ts 30min ago. Even if silent-hang threshold isn't
  exceeded (30min < 90min), total-runtime is. Preserve existing
  behavior — runaway agents still get reaped.
* **Helper returns the right thresholds** — ``_silent_hang_timeout_for_phase``
  returns expected per-phase values from ``SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE``
  and falls back to a global default for unknown phases.
* **Constant table values** — the proposed defaults (ralph=5400,
  plan/summary/verify/retro=1800, push_and_pr=900, operational=3600)
  match the issue body and the operator's dispatch.

Fixture style mirrors ``test_daemon_stuck_check_ecs_silent_hang.py``.
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
# Shared fakes — mirrors test_daemon_stuck_check_ecs_silent_hang.py
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
    logger = logging.getLogger(f"dispatcher.test.silent_hang_per_phase.{id(tmp_path)}")
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
        ecs_cluster_arn="arn:aws:ecs:us-west-2:155326049300:cluster/judgemind-dispatcher",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    d._ecs_client = MagicMock()
    return d, conn, handler


# --------------------------------------------------------------------------
# Constant + helper tests (cheap — no DB fixture)
# --------------------------------------------------------------------------


class TestSilentHangConstants:
    """The new ``SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE`` table values."""

    def test_table_exists(self) -> None:
        """The new constant exists at module level."""
        assert hasattr(daemon, "SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE")
        assert isinstance(daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE, dict)

    def test_ralph_threshold_is_90_min(self) -> None:
        """Ralph emits phase_transitions every few minutes when healthy.

        90 min is generous (10× the typical iteration cadence) but tight
        enough to reap a hung subprocess in <2h instead of waiting for
        the 15h total-runtime cap.
        """
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["ralph"] == 5400

    def test_short_llm_phases_are_30_min(self) -> None:
        """Plan / summary / verify / retro are single-pass LLM phases.

        They emit a phase_transitions row at start and again at end — no
        iteration in between. 30 min covers the longest observed
        single-pass runtime with a wide margin.
        """
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["plan"] == 1800
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["planning"] == 1800
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["summary"] == 1800
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["verify"] == 1800
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["retro"] == 1800

    def test_push_and_pr_is_15_min(self) -> None:
        """push_and_pr is git-only — no LLM. 15 min covers any TCP retry."""
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["push_and_pr"] == 900

    def test_operational_matches_total_runtime(self) -> None:
        """Operational phase has no phase_transitions sub-events.

        Silent-hang threshold equals total-runtime threshold (60 min)
        because there's only one phase_transitions row written (at end).
        """
        assert daemon.SILENT_HANG_TIMEOUT_SECONDS_BY_PHASE["operational"] == 3600

    def test_global_default_exists(self) -> None:
        """Unknown phases fall back to a global default constant."""
        assert hasattr(daemon, "SILENT_HANG_DEFAULT_SECONDS")
        assert isinstance(daemon.SILENT_HANG_DEFAULT_SECONDS, int)
        assert daemon.SILENT_HANG_DEFAULT_SECONDS == 1800


class TestSilentHangHelperLookup:
    """``_silent_hang_timeout_for_phase`` matches the table + global default."""

    def test_known_phase_returns_table_value(self, tmp_path: Path) -> None:
        d, _conn, _h = _make_daemon(tmp_path)
        assert d._silent_hang_timeout_for_phase("ralph") == 5400
        assert d._silent_hang_timeout_for_phase("push_and_pr") == 900

    def test_unknown_phase_falls_back_to_global(self, tmp_path: Path) -> None:
        d, _conn, _h = _make_daemon(tmp_path)
        assert (
            d._silent_hang_timeout_for_phase("nonexistent_phase_xyz")
            == daemon.SILENT_HANG_DEFAULT_SECONDS
        )

    def test_none_phase_falls_back_to_global(self, tmp_path: Path) -> None:
        d, _conn, _h = _make_daemon(tmp_path)
        assert (
            d._silent_hang_timeout_for_phase(None) == daemon.SILENT_HANG_DEFAULT_SECONDS
        )


# --------------------------------------------------------------------------
# Reaper-decision tests — the core regression cases from the issue body
# --------------------------------------------------------------------------


class TestSilentHangPerPhaseReaper:
    """End-to-end ``_check_stuck_agents`` behavior with the threshold split."""

    def test_silent_hang_fires_when_silent_exceeds_threshold(
        self, tmp_path: Path
    ) -> None:
        """Ralph agent silent 100min, total runtime 2h — REAP fires.

        Silent-hang threshold (90min) exceeded; total-runtime threshold
        (15h) NOT exceeded. Pre-#3731 this agent would have been left
        running for another 13h — the bug. The fix is the split: ANY of
        the two thresholds being exceeded triggers a reap.

        Concrete: this matches the ``9943a31e`` incident shape from the
        issue body (12h of silence, started_at well within total-cap).
        Without the split, the silent-hang reaper had to wait until
        elapsed ≥ 15h to fire.
        """
        d, conn, _h = _make_daemon(tmp_path)

        task_arn = "arn:aws:ecs:us-west-2:155326049300:task/cluster/silent-ralph"
        # Row schema (post-#3731): agent_id, issue_number, phase,
        # silent_seconds, total_runtime_seconds, execution_mode,
        # agent_task_arn.
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "9943a31e-022f-4874-84bf-3f6828de0b37",
                    3663,
                    "ralph",
                    6000.0,  # 100 min silent — exceeds 5400s (90min) silent-hang
                    7200.0,  # 2h total — under 54000s (15h) total-runtime
                    "ecs",
                    task_arn,
                ),
            ],
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override unset
            None,  # silent_hang_timeout_s_by_phase override unset
            (200,),  # failure_id from _write_failure RETURNING
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1, (
            "ralph agent silent 100min should be reaped via silent-hang threshold"
        )

        # Failure row written with the silent-hang category.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_AGENT_SILENT_HANG

    def test_neither_fires_when_silent_and_total_under_threshold(
        self, tmp_path: Path
    ) -> None:
        """Ralph agent iterating every 50min, total runtime 14h — NO reap.

        Slow ralph (legitimate 50min iterations on a hard issue) must
        not false-positive. Both thresholds are under cap:
        * silent: 50min < 90min
        * total: 14h < 15h

        Without the split, an aggressive silent-hang threshold could
        accidentally false-positive on slow-but-healthy ralph. The
        per-phase silent-hang threshold has to be high enough to
        accommodate the longest legitimate iteration cadence.
        """
        d, conn, _h = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-slow-ralph",
                    3700,
                    "ralph",
                    3000.0,  # 50 min silent — under 5400s (90min) threshold
                    50400.0,  # 14h total — under 54000s (15h) total-runtime
                    "subprocess",
                    None,
                ),
            ],
        ]
        # Override fetches for both threshold tables.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override unset
            None,  # silent_hang_timeout_s_by_phase override unset
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 0, (
            "slow-but-healthy ralph (50min silent, 14h total) must not be reaped"
        )

        # No failure row.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts == []

    def test_total_runtime_fires_independent_of_silent_hang(
        self, tmp_path: Path
    ) -> None:
        """Ralph agent total runtime 16h, last ts 30min ago — REAP fires.

        Existing behavior preserved: a runaway-but-active ralph (still
        emitting phase_transitions every few minutes) still hits the
        total-runtime cap. Even though silent-hang threshold (90min) is
        NOT exceeded (30min silent), total-runtime threshold (15h) IS
        exceeded (16h total). This is the original
        ``STUCK_TIMEOUT_SECONDS_BY_PHASE`` semantics, kept intact.
        """
        d, conn, _h = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-runaway-ralph",
                    3701,
                    "ralph",
                    1800.0,  # 30 min silent — under 5400s threshold
                    57600.0,  # 16h total — exceeds 54000s (15h) total-runtime
                    "subprocess",
                    None,
                ),
            ],
        ]
        # Subprocess path's fetch sequence (post-#3731 with the new
        # silent-hang override read added):
        # (1) stuck_timeout_s_by_phase override — None.
        # (2) silent_hang_timeout_s_by_phase override — None.
        # (3) failure_id RETURNING.
        # (4) _has_prior_stuck_timeout_in_window — None (first occurrence).
        # (5) prior retry-marker count.
        # (6) backoff schedule.
        conn.cursor_instance.fetch_queue = [
            None,
            None,
            (300,),
            None,
            (0,),
            ("[60,300,900]",),
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1, (
            "runaway ralph (16h total) must still hit total-runtime cap"
        )

        # The category should be stuck_timeout (total-runtime), not
        # silent-hang — because the silent-hang condition didn't trip.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
