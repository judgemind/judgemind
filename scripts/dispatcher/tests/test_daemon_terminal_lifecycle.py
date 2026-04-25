"""Lifecycle test for retry-recovery row state in dispatcher agents (issue #2933).

This test complements ``test_daemon_failure_summary.py``, which asserts SQL
*shape* (which clauses are present).  This file asserts the final simulated
*row state* — the values the admin cockpit actually renders — after the full
``running → crashed → retry_reset → running → succeeded`` sequence.

The mini-simulator ``_apply_executed_to_row`` walks ``cursor.executed`` in
order and applies each recognized UPDATE to an in-memory row dict, mirroring
the four SQL shapes in ``daemon.py``:

1. Terminal UPDATE w/ clear  (daemon.py:8071-8079)  — sets ended_at, clears failure_summary=NULL
2. Terminal UPDATE w/o clear (daemon.py:8081-8087)  — sets ended_at, leaves failure_summary alone
3. Non-terminal UPDATE       (daemon.py:8089-8095)  — updates status/phase only, no ended_at/failure_summary
4. Follow-up failure_summary write (daemon.py:7987) — SET failure_summary = %s

The retry-reset SQL shape (daemon.py:16062-16070) is also recognized so the
test can simulate what the marker-drain would do between the two terminal calls.
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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — same shape as test_daemon_failure_summary.py
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
    logger = logging.getLogger(f"dispatcher.test.terminal_lifecycle.{id(tmp_path)}")
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
# Mini-simulator: walk cursor.executed and apply SQL shapes to a row dict
# --------------------------------------------------------------------------

_SENTINEL = object()  # used to detect "no change" vs None


def _apply_executed_to_row(
    row: dict[str, Any], executed: list[tuple[str, Any]]
) -> None:
    """Walk ``cursor.executed`` in order and apply each recognized UPDATE shape.

    Mutates ``row`` in place.  Unrecognized SQL is silently ignored (e.g.
    ``INSERT INTO dispatcher.phase_transitions``, diagnoses UPDATEs, etc.).

    Recognized shapes (keyed on identifying substrings):
    - Terminal w/ clear   : ``ended_at = now()`` + ``failure_summary = NULL``
    - Terminal w/o clear  : ``ended_at = now()`` (no failure_summary clause)
    - Non-terminal        : ``SET status = %s, phase = %s`` without ``ended_at``
    - failure_summary write: ``SET failure_summary = %s``
    - Retry-reset          : ``SET status = 'retrying'``
    """
    for sql, params in executed:
        if "UPDATE dispatcher.agents" not in sql:
            continue

        if "ended_at = now()" in sql and "failure_summary = NULL" in sql:
            # Shape 1: terminal UPDATE w/ clear (succeeded / needs_review)
            # params: (status, phase, exit_code, pr_number, agent_id)
            status, phase, exit_code, pr_number, _agent_id = params
            row["status"] = status
            row["phase"] = phase
            row["ended_at"] = "now()"
            row["exit_code"] = exit_code
            if pr_number is not None:
                row["pr_number"] = pr_number
            row["failure_summary"] = None

        elif "ended_at = now()" in sql:
            # Shape 2: terminal UPDATE w/o clear (failed / crashed / plan_blocked)
            # params: (status, phase, exit_code, pr_number, agent_id)
            status, phase, exit_code, pr_number, _agent_id = params
            row["status"] = status
            row["phase"] = phase
            row["ended_at"] = "now()"
            row["exit_code"] = exit_code
            if pr_number is not None:
                row["pr_number"] = pr_number
            # failure_summary intentionally untouched here

        elif "SET status = %s, phase = %s" in sql and "ended_at" not in sql:
            # Shape 3: non-terminal UPDATE (running phase handoff)
            # params: (status, phase, pr_number, agent_id)
            status, phase, pr_number, _agent_id = params
            row["status"] = status
            row["phase"] = phase
            if pr_number is not None:
                row["pr_number"] = pr_number
            # Neither ended_at nor failure_summary touched

        elif "SET failure_summary = %s" in sql:
            # Shape 4: follow-up failure_summary write
            # params: (summary, agent_id)
            summary, _agent_id = params
            row["failure_summary"] = summary

        elif "SET status = 'retrying'" in sql:
            # Retry-reset shape (daemon.py:16062-16070)
            # params: (agent_id,)
            row["status"] = "retrying"
            row["phase"] = "claiming"
            row["exit_code"] = None
            row["ended_at"] = None


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestRetryRecoveryLifecycleFinalRowState:
    """Issue #2933 — lifecycle test asserting final row state after
    ``running → crashed → retry_reset → running → succeeded``.

    Columns ``_mark_agent_terminal`` may write, with expected post-recovery
    values after a successful retry:

        status          → 'succeeded'
        phase           → 'verify'
        ended_at        → NOT NULL  (set by terminal UPDATE for succeeded)
        exit_code       → 0         (set by the succeeded terminal call)
        pr_number       → 9999      (preserved via COALESCE through all phases)
        failure_summary → NULL      (cleared inline by the succeeded terminal UPDATE)
    """

    def test_retry_recovery_lifecycle_final_row_state(self, tmp_path: Path) -> None:
        """Drive the full ``running → crashed → retry_reset → running →
        succeeded`` sequence through ``_mark_agent_terminal`` and assert
        the final simulated row state matches what the admin cockpit would
        render.

        Steps:
        1. Seed an in-memory row at ``running`` / ``ralph-worker (2)``.
        2. Call crashed terminal → row gets failure_summary + ended_at.
        3. Apply retry-reset SQL → row reverts to retrying/claiming,
           exit_code/ended_at cleared.
        4. Call running non-terminal (Phase 3A) → pr_number set, no ended_at.
        5. Call succeeded terminal → failure_summary cleared, ended_at re-set.
        6. Assert all six columns have the expected final values.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        # ── Step 1: seed initial row ──────────────────────────────────────
        row: dict[str, Any] = {
            "status": "running",
            "phase": "ralph-worker (2)",
            "exit_code": None,
            "pr_number": None,
            "ended_at": None,
            "failure_summary": None,
            "phase_transitions": [],
        }

        # ── Step 2: crashed terminal ──────────────────────────────────────
        conn.cursor_instance.fetch_queue = [
            ("stuck_timeout", {"stuck_seconds": 5400}),
            (None,),
        ]
        d._mark_agent_terminal(
            "agent-lifecycle",
            status="crashed",
            phase="ralph-worker (2)",
            exit_code=None,
        )

        executed_before_reset = list(conn.cursor_instance.executed)
        _apply_executed_to_row(row, executed_before_reset)

        # Intermediate: row should be crashed with failure_summary populated
        assert row["status"] == "crashed", (
            f"Expected crashed after first terminal, got {row['status']}"
        )
        assert row["ended_at"] is not None, (
            "ended_at must be set after crashed terminal"
        )
        assert row["failure_summary"] is not None, (
            "failure_summary must be populated after crashed terminal"
        )

        # ── Step 3: retry-reset (simulate marker-drain path) ─────────────
        reset_sql = (
            "UPDATE dispatcher.agents "
            "SET status = 'retrying', "
            "    phase = 'claiming', "
            "    retries_used = retries_used + 1, "
            "    exit_code = NULL, "
            "    ended_at = NULL "
            "WHERE agent_id = %s"
        )
        # Capture position before reset executes so we can slice correctly
        pos_before_reset = len(conn.cursor_instance.executed)
        conn.cursor_instance.execute(reset_sql, ("agent-lifecycle",))
        _apply_executed_to_row(row, conn.cursor_instance.executed[pos_before_reset:])

        assert row["status"] == "retrying", (
            f"Expected retrying after reset, got {row['status']}"
        )
        assert row["exit_code"] is None, "exit_code must be cleared by retry-reset"
        assert row["ended_at"] is None, "ended_at must be cleared by retry-reset"
        # failure_summary is NOT cleared by retry-reset (that's the succeeded terminal's job)
        assert row["failure_summary"] is not None, (
            "failure_summary should still be set after retry-reset (cleared only on succeeded terminal)"
        )

        # ── Step 4: running non-terminal (Phase 3A post-PR hand-off) ─────
        pos_before_running = len(conn.cursor_instance.executed)
        d._mark_agent_terminal(
            "agent-lifecycle",
            status="running",
            phase="awaiting_ci",
            pr_number=9999,
        )
        _apply_executed_to_row(row, conn.cursor_instance.executed[pos_before_running:])

        assert row["status"] == "running", (
            f"Expected running after non-terminal, got {row['status']}"
        )
        assert row["phase"] == "awaiting_ci"
        assert row["pr_number"] == 9999, (
            "pr_number must be set via COALESCE on non-terminal"
        )
        assert row["ended_at"] is None, "Non-terminal must NOT set ended_at"
        # failure_summary must still be untouched by non-terminal UPDATE
        assert row["failure_summary"] is not None, (
            "Non-terminal UPDATE must not clear failure_summary"
        )

        # ── Step 5: succeeded terminal ────────────────────────────────────
        pos_before_succeeded = len(conn.cursor_instance.executed)
        d._mark_agent_terminal(
            "agent-lifecycle",
            status="succeeded",
            phase="verify",
            exit_code=0,
            pr_number=9999,
        )
        _apply_executed_to_row(
            row, conn.cursor_instance.executed[pos_before_succeeded:]
        )

        # ── Step 6: final row assertions (AC-1 verify-line) ──────────────
        assert row["status"] == "succeeded", (
            f"Final status must be 'succeeded'; got {row['status']!r}"
        )
        assert row["failure_summary"] is None, (
            f"failure_summary must be NULL after succeeded terminal; got {row['failure_summary']!r}"
        )
        assert row["exit_code"] == 0, (
            f"exit_code must be 0 after succeeded terminal; got {row['exit_code']!r}"
        )
        assert row["pr_number"] == 9999, (
            f"pr_number must be preserved as 9999 via COALESCE; got {row['pr_number']!r}"
        )
        assert row["ended_at"] is not None, (
            "ended_at must be set (non-NULL) after succeeded terminal"
        )
        assert row["phase"] == "verify", (
            f"phase must be 'verify' after succeeded terminal; got {row['phase']!r}"
        )
