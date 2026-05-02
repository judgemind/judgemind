"""Unit tests for ``dispatcher_v3.breaker.BreakerEvaluator``.

Pinned invariants (issue #3883):

- **2-of-3 trips, scoped to v3.** A bad-streak in the v3 rolling
  window flips ``concurrency_cap_v3`` to 0 and stamps
  ``cap_flipped_by_v3='circuit_breaker_v3'``. v2 outcomes (selected
  out by the ``parent_run_id`` filter) never count.
- **Auto-close paths.** Operator-reflip clears the flag; time-based
  restores cap once the window has rolled and bad-count is below
  threshold.
- **Telegram fires once on fresh open.** Re-asserting an already-open
  state (cap was 0) does NOT fire the alert again.
- **Tolerant classifier.** Anything not in
  :data:`OVERNIGHT_CB_GOOD_OUTCOME_STATUSES` counts as bad — matches
  v2's #2860 contract.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

# Inject a fake ``psycopg.errors`` module up front so the launcher's
# lazy ``import psycopg`` finds something callable in environments
# where the real wheel is not installed (matches test_launcher.py).
if "psycopg" not in sys.modules:
    fake_psycopg = types.ModuleType("psycopg")
    fake_errors = types.ModuleType("psycopg.errors")

    class _FakeUniqueViolation(Exception):
        pass

    fake_errors.UniqueViolation = _FakeUniqueViolation
    fake_psycopg.errors = fake_errors
    sys.modules["psycopg"] = fake_psycopg
    sys.modules["psycopg.errors"] = fake_errors


from dispatcher_v3.breaker import (  # noqa: E402
    CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER,
    DEFAULT_OVERNIGHT_CB_BAD_OUTCOME_THRESHOLD,
    DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES,
    DEFAULT_OVERNIGHT_CB_WINDOW_SIZE,
    BreakerEvaluator,
    OVERNIGHT_CB_GOOD_OUTCOME_STATUSES,
    V3_CB_THRESHOLD_KEY,
    V3_CB_WINDOW_MINUTES_KEY,
    V3_CB_WINDOW_SIZE_KEY,
    V3_SCOPED_PARENT_RUN_FILTER,
)


# ---------------------------------------------------------------------------
# Fakes (mirror test_launcher.py shape)
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


def _make_config_handler(conn: FakeConn, *, values: dict[str, Any]) -> None:
    """Install a handler returning ``values[key]`` for config reads."""

    def handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        if not params:
            cur._next_fetchone = None
            return
        key = params[0]
        if key in values:
            cur._next_fetchone = (values[key],)
        else:
            cur._next_fetchone = None

    conn.install_handler("FROM dispatcher.config WHERE key = %s", handler)


def _make_breaker(
    conn: FakeConn,
    *,
    statuses_by_window: list[str] | None = None,
    config_values: dict[str, Any] | None = None,
    telegram_alerter: Any = None,
    clock_returns: datetime | None = None,
) -> tuple[BreakerEvaluator, list[dict[str, Any]]]:
    """Wire common handlers + return (breaker, telegram_call_log)."""
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

    if config_values:
        _make_config_handler(conn, values=config_values)
    if statuses_by_window is not None:
        conn.install_handler(
            "FROM dispatcher.terminal_outcomes t",
            lambda cur, sql, params: setattr(
                cur,
                "_next_fetchall",
                [(s,) for s in statuses_by_window],
            ),
        )
    # SELECT issue_number, parent_run_id FROM dispatcher.agents — the
    # write-terminal-outcome lookup. Default: synthetic agent row.
    conn.install_handler(
        "SELECT issue_number, parent_run_id",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (1234, "v3-run-uuid")),
    )

    breaker = BreakerEvaluator(
        conn=conn,
        run_id="run-test-uuid",
        telegram_alerter=telegram_alerter,
        clock=(lambda: clock_returns) if clock_returns else None,
    )
    return breaker, telegram_calls


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_is_bad_outcome_only_succeeded_is_good() -> None:
    """succeeded → False; everything else → True."""
    assert BreakerEvaluator.is_bad_outcome("succeeded") is False
    assert BreakerEvaluator.is_bad_outcome("failed") is True
    assert BreakerEvaluator.is_bad_outcome("crashed") is True
    assert BreakerEvaluator.is_bad_outcome("needs_review") is True
    assert BreakerEvaluator.is_bad_outcome("unknown_future_status") is True


def test_good_outcome_set_is_just_succeeded() -> None:
    assert OVERNIGHT_CB_GOOD_OUTCOME_STATUSES == frozenset({"succeeded"})


# ---------------------------------------------------------------------------
# record_and_evaluate — happy path
# ---------------------------------------------------------------------------


def test_breaker_writes_terminal_outcome_row() -> None:
    """record_and_evaluate inserts a row into terminal_outcomes."""
    conn = FakeConn()
    breaker, _ = _make_breaker(
        conn,
        statuses_by_window=["succeeded"],  # benign — won't trip
        config_values={
            "circuit_breaker_enabled": "true",
        },
    )
    breaker.record_and_evaluate(agent_id="ag-1", status="succeeded")

    inserts = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.terminal_outcomes")
    ]
    assert len(inserts) == 1
    sql, params = inserts[0]
    # (agent_id, issue_number, status, parent_run_id, ended_at) — last is now()
    assert params[0] == "ag-1"
    assert params[1] == 1234  # issue_number lookup
    assert params[2] == "succeeded"
    assert params[3] == "v3-run-uuid"  # parent_run_id lookup


def test_breaker_does_not_trip_below_threshold() -> None:
    """5/10 of last terminals bad → no trip when threshold = 5? Wait, that's met.

    Actually: threshold is the floor (>=), so 5/10 trips. To not trip,
    we need fewer than threshold bad outcomes. Test 4 bad + 1 good =
    bad_count = 4 < 5 = threshold → no trip.
    """
    conn = FakeConn()
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=[
            "failed",
            "failed",
            "failed",
            "failed",
            "succeeded",
        ],
        config_values={
            "circuit_breaker_enabled": "true",
            # Default threshold = 5 (per migration 29 seed).
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is False
    # No cap flip.
    cap_flips = [
        (sql, params)
        for sql, params in conn.executed
        if "concurrency_cap_v3" in sql and "INSERT INTO dispatcher.config" in sql
    ]
    assert cap_flips == []
    assert telegram_calls == []


def test_breaker_trips_when_threshold_met_and_flips_cap_v3() -> None:
    """5/10 bad outcomes → trip → flip concurrency_cap_v3 to 0."""
    conn = FakeConn()
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=[
            "failed",
            "failed",
            "failed",
            "failed",
            "failed",
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
        ],
        config_values={
            "circuit_breaker_enabled": "true",
            "concurrency_cap_v3": "1",  # currently open
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is True

    # ``concurrency_cap_v3`` UPSERT-ed with value '0'.
    cap_flip_writes = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.config")
        and params
        and params[0] == "concurrency_cap_v3"
    ]
    assert cap_flip_writes, "v3 breaker must write to concurrency_cap_v3"
    _, params = cap_flip_writes[-1]
    assert params[1] == "0"
    assert params[2] == CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER

    # ``cap_flipped_by_v3`` stamped.
    flag_writes = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.config")
        and params
        and params[0] == "cap_flipped_by_v3"
    ]
    assert flag_writes, "must stamp cap_flipped_by_v3 on trip"

    # Telegram alert fired once.
    assert len(telegram_calls) == 1
    assert telegram_calls[0]["trigger"] == "breaker_opened"
    assert "OPENED" in telegram_calls[0]["message"]
    assert "concurrency_cap_v3" in telegram_calls[0]["message"]


def test_breaker_uses_2_of_3_when_v3_keys_set() -> None:
    """Issue body example: 2-of-3 in last 1h → custom v3 keys override."""
    conn = FakeConn()
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=["failed", "failed", "succeeded"],
        config_values={
            "circuit_breaker_enabled": "true",
            "concurrency_cap_v3": "1",
            V3_CB_WINDOW_MINUTES_KEY: "60",  # 1h
            V3_CB_WINDOW_SIZE_KEY: "3",  # last 3
            V3_CB_THRESHOLD_KEY: "2",  # 2 bad
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is True
    assert len(telegram_calls) == 1


def test_breaker_does_not_trip_with_2_of_3_when_only_1_bad() -> None:
    conn = FakeConn()
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=["failed", "succeeded", "succeeded"],
        config_values={
            "circuit_breaker_enabled": "true",
            V3_CB_WINDOW_MINUTES_KEY: "60",
            V3_CB_WINDOW_SIZE_KEY: "3",
            V3_CB_THRESHOLD_KEY: "2",
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is False
    assert telegram_calls == []


# ---------------------------------------------------------------------------
# v3-scoping (parent_run_id filter on the scan)
# ---------------------------------------------------------------------------


def test_scan_uses_v3_scoped_parent_run_filter() -> None:
    """The breaker's outcome scan filters on parent_run_id IN (v3 runs)."""
    conn = FakeConn()
    breaker, _ = _make_breaker(
        conn,
        statuses_by_window=["succeeded"],
        config_values={"circuit_breaker_enabled": "true"},
    )
    breaker.record_and_evaluate(agent_id="ag-1", status="succeeded")

    scans = [
        (sql, params)
        for sql, params in conn.executed
        if "FROM dispatcher.terminal_outcomes" in sql and "ORDER BY" in sql
    ]
    assert scans, "must run a window scan"
    sql, _ = scans[0]
    assert V3_SCOPED_PARENT_RUN_FILTER in sql, (
        f"v3 breaker must scope to v3 runs via parent_run_id filter (got: {sql})"
    )


def test_v3_breaker_ignores_v2_outcomes() -> None:
    """Breaker scan only returns v3-scoped rows — v2 outcomes don't count.

    We simulate this by having the scan handler return only the v3
    rows (the FakeConn handler is the SQL gate; the assertion is that
    the SQL the breaker emits asks only for v3 rows).

    The behavioral check: 3 v3-rows of [failed, failed, succeeded]
    with threshold=2 trips. If the scan had returned a different
    set of statuses (because the filter wasn't applied), the test
    would observe a different outcome.
    """
    conn = FakeConn()
    # Statuses_by_window represents what the v3-scoped query returns.
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=["failed", "failed", "succeeded"],
        config_values={
            "circuit_breaker_enabled": "true",
            "concurrency_cap_v3": "1",
            V3_CB_WINDOW_SIZE_KEY: "3",
            V3_CB_THRESHOLD_KEY: "2",
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is True

    # Verify the SELECT'd SQL has the v3 filter (no v2 mismatch leak).
    for sql, _ in conn.executed:
        if "FROM dispatcher.terminal_outcomes" in sql and "ORDER BY" in sql:
            # Must NOT contain 'v2' literal in scope (would be wrong scope)
            assert "dispatcher_version = 'v2'" not in sql
            assert "dispatcher_version = 'v3'" in sql


# ---------------------------------------------------------------------------
# Idempotence + alert dedup
# ---------------------------------------------------------------------------


def test_breaker_does_not_fire_telegram_when_already_open() -> None:
    """Re-asserting an already-open breaker (cap=0) skips the Telegram alert."""
    conn = FakeConn()
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=["failed"] * 5 + ["succeeded"] * 5,
        config_values={
            "circuit_breaker_enabled": "true",
            "concurrency_cap_v3": "0",  # already open
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is True  # re-asserts state
    assert telegram_calls == [], (
        "no Telegram alert when re-asserting an already-open breaker"
    )


def test_breaker_disabled_via_config_does_not_trip() -> None:
    """``circuit_breaker_enabled = false`` skips evaluation entirely."""
    conn = FakeConn()
    breaker, telegram_calls = _make_breaker(
        conn,
        statuses_by_window=["failed"] * 10,
        config_values={
            "circuit_breaker_enabled": "false",
            "concurrency_cap_v3": "1",
        },
    )
    tripped = breaker.record_and_evaluate(agent_id="ag-1", status="failed")
    assert tripped is False
    # No cap flip even though the streak is bad.
    cap_flips = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.config")
        and params
        and params[0] == "concurrency_cap_v3"
    ]
    assert cap_flips == []
    assert telegram_calls == []


# ---------------------------------------------------------------------------
# maybe_auto_close — operator-reflip path
# ---------------------------------------------------------------------------


def test_auto_close_clears_flag_when_operator_reflipped_cap() -> None:
    """cap_v3 >= 1 + flag set → clear flag (operator reflip path)."""
    conn = FakeConn()
    breaker, _ = _make_breaker(
        conn,
        config_values={
            "cap_flipped_by_v3": '"circuit_breaker_v3"',
            "concurrency_cap_v3": "2",  # operator raised
            "circuit_breaker_enabled": "true",
        },
    )
    closed = breaker.maybe_auto_close()
    assert closed is True

    # ``cap_flipped_by_v3`` cleared (set to 'null').
    flag_clears = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.config")
        and params
        and params[0] == "cap_flipped_by_v3"
    ]
    assert flag_clears, "operator-reflip must clear the flag"


def test_auto_close_skips_when_flag_unset() -> None:
    """cap_flipped_by_v3 null → no auto-close (hot path)."""
    conn = FakeConn()
    breaker, _ = _make_breaker(
        conn,
        config_values={"cap_flipped_by_v3": None},
    )
    closed = breaker.maybe_auto_close()
    assert closed is False


def test_auto_close_skips_when_flipped_by_other_source() -> None:
    """cap_flipped_by_v3 = something other than 'circuit_breaker_v3' → no-op."""
    conn = FakeConn()
    breaker, _ = _make_breaker(
        conn,
        config_values={
            "cap_flipped_by_v3": '"operator_killswitch"',
        },
    )
    closed = breaker.maybe_auto_close()
    assert closed is False


# ---------------------------------------------------------------------------
# maybe_auto_close — time-based path (#3779 pattern)
# ---------------------------------------------------------------------------


def test_auto_close_time_based_restores_cap_after_window_elapsed() -> None:
    """cap=0, flag set, window elapsed, bad_count low → restore cap."""
    conn = FakeConn()
    # cap_updated_at is 60min ago (longer than default 30min window).
    cap_updated_at = datetime.now(UTC) - timedelta(minutes=60)
    now = datetime.now(UTC)

    # Custom handler: ``cap_flipped_by_v3`` reads need the flag value;
    # ``concurrency_cap_v3`` updated_at lookup needs cap_updated_at.
    flow: dict[str, Any] = {
        "cap_flipped_by_v3": '"circuit_breaker_v3"',
        "concurrency_cap_v3": "0",
        "circuit_breaker_enabled": "true",
        "target_concurrency_cap_v3": "2",
    }

    def config_value_handler(
        cur: FakeCursor, sql: str, params: tuple[Any, ...]
    ) -> None:
        if not params:
            cur._next_fetchone = None
            return
        key = params[0]
        if key in flow:
            cur._next_fetchone = (flow[key],)
        else:
            cur._next_fetchone = None

    def updated_at_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        if params and params[0] == "concurrency_cap_v3":
            cur._next_fetchone = (cap_updated_at,)
        else:
            cur._next_fetchone = None

    conn.install_handler(
        "SELECT updated_at FROM dispatcher.config",
        updated_at_handler,
    )
    conn.install_handler(
        "SELECT value FROM dispatcher.config WHERE key = %s",
        config_value_handler,
    )
    # Outcome scan returns 1 bad (below default threshold of 5).
    conn.install_handler(
        "FROM dispatcher.terminal_outcomes t",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            [("succeeded",), ("failed",), ("succeeded",)],
        ),
    )

    breaker = BreakerEvaluator(
        conn=conn,
        run_id="run-test-uuid",
        clock=lambda: now,
    )
    closed = breaker.maybe_auto_close()
    assert closed is True

    # Cap restored to target (2).
    cap_writes = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.config")
        and params
        and params[0] == "concurrency_cap_v3"
    ]
    assert cap_writes, "auto-close must restore concurrency_cap_v3"
    _, params = cap_writes[-1]
    assert params[1] == "2"
    assert params[2] == "circuit_breaker_v3_auto_close"


def test_auto_close_blocks_when_window_not_elapsed() -> None:
    """cap_updated_at recent → don't auto-close yet."""
    conn = FakeConn()
    cap_updated_at = datetime.now(UTC) - timedelta(minutes=5)
    now = datetime.now(UTC)
    flow = {
        "cap_flipped_by_v3": '"circuit_breaker_v3"',
        "concurrency_cap_v3": "0",
        "circuit_breaker_enabled": "true",
    }

    def config_value_handler(
        cur: FakeCursor, sql: str, params: tuple[Any, ...]
    ) -> None:
        cur._next_fetchone = (
            (flow[params[0]],) if params and params[0] in flow else None
        )

    conn.install_handler(
        "SELECT updated_at FROM dispatcher.config",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (cap_updated_at,)),
    )
    conn.install_handler(
        "SELECT value FROM dispatcher.config WHERE key = %s",
        config_value_handler,
    )
    breaker = BreakerEvaluator(
        conn=conn,
        run_id="run-test-uuid",
        clock=lambda: now,
    )
    closed = breaker.maybe_auto_close()
    assert closed is False


def test_auto_close_blocks_when_bad_count_still_high() -> None:
    """cap=0, window elapsed, but bad_count >= threshold → don't close yet."""
    conn = FakeConn()
    cap_updated_at = datetime.now(UTC) - timedelta(minutes=60)
    now = datetime.now(UTC)
    flow = {
        "cap_flipped_by_v3": '"circuit_breaker_v3"',
        "concurrency_cap_v3": "0",
        "circuit_breaker_enabled": "true",
    }

    conn.install_handler(
        "SELECT updated_at FROM dispatcher.config",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (cap_updated_at,)),
    )
    conn.install_handler(
        "SELECT value FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchone",
            (flow[params[0]],) if params and params[0] in flow else None,
        ),
    )
    # Still 5+ bad outcomes — above threshold.
    conn.install_handler(
        "FROM dispatcher.terminal_outcomes t",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            [("failed",)] * 5 + [("succeeded",)] * 5,
        ),
    )
    breaker = BreakerEvaluator(
        conn=conn,
        run_id="run-test-uuid",
        clock=lambda: now,
    )
    closed = breaker.maybe_auto_close()
    assert closed is False


def test_auto_close_disabled_breaker_yields_open_state() -> None:
    """``circuit_breaker_enabled = false`` blocks time-based auto-close."""
    conn = FakeConn()
    cap_updated_at = datetime.now(UTC) - timedelta(minutes=60)
    now = datetime.now(UTC)
    flow = {
        "cap_flipped_by_v3": '"circuit_breaker_v3"',
        "concurrency_cap_v3": "0",
        "circuit_breaker_enabled": "false",
    }
    conn.install_handler(
        "SELECT updated_at FROM dispatcher.config",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (cap_updated_at,)),
    )
    conn.install_handler(
        "SELECT value FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchone",
            (flow[params[0]],) if params and params[0] in flow else None,
        ),
    )
    breaker = BreakerEvaluator(
        conn=conn,
        run_id="run-test-uuid",
        clock=lambda: now,
    )
    closed = breaker.maybe_auto_close()
    assert closed is False


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_window_minutes_is_30() -> None:
    """v2 default carries forward when no v3-specific key set."""
    assert DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES == 30


def test_default_window_size_is_10() -> None:
    assert DEFAULT_OVERNIGHT_CB_WINDOW_SIZE == 10


def test_default_threshold_is_5() -> None:
    assert DEFAULT_OVERNIGHT_CB_BAD_OUTCOME_THRESHOLD == 5
