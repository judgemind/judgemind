"""Launcher-level wiring for the v3 breaker + Telegram (issue #3883).

The pure-breaker tests live in ``test_breaker.py``. This file checks
that the launcher actually calls into the breaker / Telegram seam at
the right places:

1. ``_mark_agent_terminal`` → calls ``BreakerEvaluator.record_and_evaluate``
   after the agent-row UPDATE + label release (best-effort: a breaker
   exception does NOT propagate).
2. ``_mark_agent_needs_review`` (the C5 path) → fires a Telegram alert
   after writing ``status='needs_review'``. This replaces the prior
   ``# TODO(#3883 — C6)`` marker.
3. ``tick`` → calls ``BreakerEvaluator.maybe_auto_close`` once per
   tick, BEFORE the claim step (so a freshly-closed breaker restores
   the cap on the same tick).
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

# Inject the same psycopg fake as test_launcher.py (CI runner has no
# psycopg wheel installed — see ``.github/workflows/ci.yml``).
if "psycopg" not in sys.modules:
    fake_psycopg = types.ModuleType("psycopg")
    fake_errors = types.ModuleType("psycopg.errors")

    class _FakeUniqueViolation(Exception):
        pass

    fake_errors.UniqueViolation = _FakeUniqueViolation
    fake_psycopg.errors = fake_errors
    sys.modules["psycopg"] = fake_psycopg
    sys.modules["psycopg.errors"] = fake_errors


from dispatcher_v3.launcher import (  # noqa: E402
    DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX,
    Launcher,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.conn.executed.append((sql, params or ()))
        for predicate, handler in self.conn.handlers:
            if predicate in sql:
                handler(self, sql, params or ())
                return
        self._next_fetchone = None
        self._next_fetchall: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        return getattr(self, "_next_fetchone", None)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return getattr(self, "_next_fetchall", []) or []


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.handlers: list[tuple[str, Any]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def install_handler(self, predicate: str, handler: Any) -> None:
        self.handlers.append((predicate, handler))


class RecordingBreaker:
    """Records calls to the BreakerEvaluator surface used by the launcher."""

    def __init__(self) -> None:
        self.record_calls: list[tuple[str, str]] = []
        self.auto_close_calls: int = 0
        self.auto_close_returns: bool = False
        self.record_raises: Exception | None = None

    def record_and_evaluate(self, *, agent_id: str, status: str) -> bool:
        self.record_calls.append((agent_id, status))
        if self.record_raises is not None:
            raise self.record_raises
        return False

    def maybe_auto_close(self) -> bool:
        self.auto_close_calls += 1
        return self.auto_close_returns


def _make_launcher(
    *,
    conn: FakeConn,
    breaker: Any,
    telegram_alerter: Any | None = None,
    ecs_client: MagicMock | None = None,
) -> Launcher:
    telegram_calls: list[dict[str, Any]] = []
    if telegram_alerter is None:

        def telegram_alerter(message: str, *, trigger: str, run_id: str) -> None:
            telegram_calls.append(
                {
                    "message": message,
                    "trigger": trigger,
                    "run_id": run_id,
                }
            )

    return Launcher(
        run_id="run-test-uuid",
        github_repo="judgemind/judgemind",
        ecs_cluster_arn="arn:aws:ecs:us-west-2:0:cluster/jm",
        task_runner_task_definition="judgemind-task-runner:7",
        diagnoser_task_definition="judgemind-dispatcher-v3-diagnoser",
        agent_runner_subnet_ids=["subnet-a", "subnet-b"],
        agent_runner_security_group_id="sg-aaa",
        sessions_bucket="judgemind-sessions-dev",
        task_runner_log_group="",
        task_runner_log_stream_prefix=DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX,
        conn=conn,
        ecs_client=ecs_client,
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
        breaker=breaker,
        telegram_alerter=telegram_alerter,
    )


# ---------------------------------------------------------------------------
# _mark_agent_terminal → breaker
# ---------------------------------------------------------------------------


def test_mark_agent_terminal_calls_breaker() -> None:
    """After the UPDATE + label release, the breaker is invoked."""
    conn = FakeConn()
    breaker = RecordingBreaker()
    launcher = _make_launcher(conn=conn, breaker=breaker)

    launcher._mark_agent_terminal(
        agent_id="ag-1",
        issue_number=42,
        status="failed",
        exit_code=1,
        exit_reason="boom",
    )
    assert breaker.record_calls == [("ag-1", "failed")]


def test_mark_agent_terminal_breaker_failure_does_not_propagate() -> None:
    """A breaker exception is logged but does not block the transition."""
    conn = FakeConn()
    breaker = RecordingBreaker()
    breaker.record_raises = RuntimeError("breaker exploded")
    launcher = _make_launcher(conn=conn, breaker=breaker)

    # Should NOT raise.
    launcher._mark_agent_terminal(
        agent_id="ag-1",
        issue_number=42,
        status="succeeded",
        exit_code=0,
        exit_reason="",
    )
    # Agent-row UPDATE still ran.
    assert any(
        sql.startswith("UPDATE dispatcher.agents")
        and params
        and params[0] == "succeeded"
        for sql, params in conn.executed
    )


def test_mark_agent_terminal_breaker_called_with_status_string() -> None:
    """Status passed verbatim — the breaker classifies, not the launcher."""
    conn = FakeConn()
    breaker = RecordingBreaker()
    launcher = _make_launcher(conn=conn, breaker=breaker)
    launcher._mark_agent_terminal(
        agent_id="ag-2",
        issue_number=99,
        status="succeeded",
        exit_code=0,
        exit_reason="",
    )
    assert breaker.record_calls == [("ag-2", "succeeded")]


# ---------------------------------------------------------------------------
# _mark_agent_needs_review → Telegram (replaces TODO(#3883 — C6))
# ---------------------------------------------------------------------------


def test_mark_agent_needs_review_fires_telegram() -> None:
    """Diagnoser-failure path now sends a Telegram alert (#3883)."""
    conn = FakeConn()
    # issue_number lookup returns 5555.
    conn.install_handler(
        "SELECT issue_number FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (5555,)),
    )
    breaker = RecordingBreaker()
    telegram_calls: list[dict[str, Any]] = []

    def telegram_alerter(message: str, *, trigger: str, run_id: str) -> None:
        telegram_calls.append(
            {
                "message": message,
                "trigger": trigger,
                "run_id": run_id,
            }
        )

    launcher = _make_launcher(
        conn=conn,
        breaker=breaker,
        telegram_alerter=telegram_alerter,
    )
    launcher._mark_agent_needs_review(
        agent_id="ag-1",
        diagnoser_exit_code=137,
        diagnoser_exit_reason="OOMKilled",
    )

    # Status flipped + outcome_summary written.
    assert any(
        sql.startswith("UPDATE dispatcher.agents") and "needs_review" in sql
        for sql, _ in conn.executed
    )
    # Telegram called once with the right trigger.
    assert len(telegram_calls) == 1
    call = telegram_calls[0]
    assert call["trigger"] == "needs_review"
    assert call["run_id"] == "run-test-uuid"
    # Message references the issue + exit code.
    assert "5555" in call["message"]
    assert "137" in call["message"]
    assert "OOMKilled" in call["message"]


def test_mark_agent_needs_review_telegram_failure_does_not_propagate() -> None:
    """Telegram exception is logged; needs_review write still happens."""
    conn = FakeConn()
    conn.install_handler(
        "SELECT issue_number FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (1,)),
    )
    breaker = RecordingBreaker()

    def boom_alerter(message: str, *, trigger: str, run_id: str) -> None:
        raise RuntimeError("telegram down")

    launcher = _make_launcher(
        conn=conn,
        breaker=breaker,
        telegram_alerter=boom_alerter,
    )
    # Should NOT raise.
    launcher._mark_agent_needs_review(
        agent_id="ag-1",
        diagnoser_exit_code=1,
        diagnoser_exit_reason="x",
    )
    assert any(
        "needs_review" in sql
        for sql, _ in conn.executed
        if sql.startswith("UPDATE dispatcher.agents")
    )


# ---------------------------------------------------------------------------
# tick → maybe_auto_close
# ---------------------------------------------------------------------------


def test_tick_calls_breaker_maybe_auto_close() -> None:
    """Each tick invokes the breaker's auto-close hop exactly once."""
    conn = FakeConn()
    # cap_v3 = 0 so the claim step is a no-op.
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("0",)),
    )
    breaker = RecordingBreaker()
    breaker.auto_close_returns = False
    launcher = _make_launcher(conn=conn, breaker=breaker)
    summary = launcher.tick()
    assert breaker.auto_close_calls == 1
    assert summary["breaker_auto_closed"] is False


def test_tick_records_auto_close_in_summary_when_breaker_closes() -> None:
    """When auto-close fires, the tick summary reports it."""
    conn = FakeConn()
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("1",)),
    )
    breaker = RecordingBreaker()
    breaker.auto_close_returns = True
    launcher = _make_launcher(conn=conn, breaker=breaker)
    summary = launcher.tick()
    assert summary["breaker_auto_closed"] is True


def test_tick_breaker_exception_does_not_crash_loop() -> None:
    """A breaker exception is caught at the launcher level."""
    conn = FakeConn()
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("0",)),
    )

    class BoomBreaker:
        def maybe_auto_close(self) -> bool:
            raise RuntimeError("breaker exploded")

        def record_and_evaluate(self, **kwargs: Any) -> bool:
            return False

    launcher = _make_launcher(conn=conn, breaker=BoomBreaker())
    # Should not raise — caught, summary["breaker_auto_closed"] = False.
    summary = launcher.tick()
    assert summary["breaker_auto_closed"] is False


# ---------------------------------------------------------------------------
# Lookup issue_number — defensive
# ---------------------------------------------------------------------------


def test_lookup_issue_number_handles_missing_row() -> None:
    """Missing agent row → 0 sentinel (not a crash, not a None)."""
    conn = FakeConn()
    conn.install_handler(
        "SELECT issue_number FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", None),
    )
    launcher = _make_launcher(conn=conn, breaker=RecordingBreaker())
    assert launcher._lookup_issue_number("ag-x") == 0


def test_lookup_issue_number_handles_db_error() -> None:
    """DB exception → 0 sentinel (caller still fires Telegram with #0)."""
    conn = FakeConn()

    def raise_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        raise RuntimeError("db down")

    conn.install_handler(
        "SELECT issue_number FROM dispatcher.agents",
        raise_handler,
    )
    launcher = _make_launcher(conn=conn, breaker=RecordingBreaker())
    assert launcher._lookup_issue_number("ag-x") == 0
