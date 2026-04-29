"""Regression tests for the time-based circuit-breaker auto-close path (#3779).

The pre-#3779 ``_check_circuit_breaker_auto_close`` only fired when the
operator manually raised ``concurrency_cap`` from 0 → ≥1. That path is
necessary but not sufficient: when the breaker opens (cap → 0) the
queue stops draining, so no new terminal outcomes arrive, so the
operator has to manually run ``breaker.sh reset`` once the bad-outcome
window rolls down. The breaker is in a closed-feedback-loop deadlock —
the only thing that would close it (good outcomes) is gated by the
breaker itself.

Issue #3779 adds a **time-based** auto-close path:

* When ``concurrency_cap == 0`` AND ``cap_flipped_by == "circuit_breaker"``
  (the breaker is open),
* AND ``now() > cap_updated_at + window_minutes`` (we have waited at
  least one full bad-outcome window since the breaker opened),
* AND ``bad_count < threshold`` over the current rolling window (the
  cluster of bad outcomes that opened the breaker has aged out),

then the daemon auto-closes the breaker:

* ``concurrency_cap`` is restored to ``target_concurrency_cap`` (the
  operator-configured target — distinct from the runtime value the
  breaker writes to). Defaults to 1 if no target row exists, matching
  the legacy ``start`` command behaviour.
* ``cap_flipped_by`` is cleared to null.
* ``daemon.circuit_breaker_auto_closed`` is logged.

These tests stage the relevant state via the fake-cursor pattern shared
with the rest of ``test_daemon_circuit_breaker.py`` and assert against
``conn.executed`` so they run in milliseconds without a real DB.

The acceptance criteria scenarios:

1. **Positive case** — breaker open, ≥window_minutes elapsed, bad_count
   under threshold → cap=target, flag cleared, event logged.
2. **Negative case (still over threshold)** — bad_count remains over
   threshold → no auto-close, cap stays 0.
3. **Negative case (window not elapsed)** — only 5 minutes since the
   breaker opened (not 35) → no auto-close, cap stays 0.
4. **Negative case (flag not breaker)** — cap=0 but flipped_by is null
   or "operator" — not the breaker's job to recover, no action.
5. **Default target** — when no ``target_concurrency_cap`` row exists,
   reset uses 1 (matching legacy ``start`` semantics).
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

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — mirror the pattern in test_daemon_circuit_breaker.py.
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
    logger = logging.getLogger(f"dispatcher.test.cb_auto_close.{id(tmp_path)}")
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
        baseline_repo_root=str(tmp_path),
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Helpers — stage the fetch queue for the time-based auto-close path.
# --------------------------------------------------------------------------


def _stage_time_based_state(
    conn: _FakeConnection,
    *,
    flipped_by: str | None = "circuit_breaker",
    cap_updated_minutes_ago: int = 35,
    window_minutes: int = 30,
    window_size: int = 10,
    threshold: int = 5,
    bad_outcomes: int = 2,
    target_cap: int | None = 4,
    enabled: bool = True,
) -> None:
    """Stage fetchone/fetchall queue for ``_check_circuit_breaker_auto_close``.

    Matches the SQL order the implementation will issue when called with
    ``current_cap=0``:

      1. SELECT value FROM dispatcher.config WHERE key='cap_flipped_by'
      2. SELECT value FROM dispatcher.config WHERE key='circuit_breaker_enabled'
      3. SELECT value, updated_at FROM dispatcher.config WHERE key='concurrency_cap'
      4. SELECT value FROM dispatcher.config WHERE key='circuit_breaker_window_minutes'
      5. SELECT value FROM dispatcher.config WHERE key='circuit_breaker_window_size'
      6. SELECT value FROM dispatcher.config WHERE key='circuit_breaker_bad_outcome_threshold'
      7. SELECT t.status, ... FROM dispatcher.terminal_outcomes ...  (fetchall)
      8. SELECT value FROM dispatcher.config WHERE key='target_concurrency_cap'
      9. UPDATE dispatcher.config SET value=<target> WHERE key='concurrency_cap'
     10. UPDATE dispatcher.config SET value='null' WHERE key='cap_flipped_by'

    Steps 4–10 only fire when steps 1–3 say the breaker is open and the
    window has elapsed.
    """
    cap_updated_at = datetime.now(UTC) - timedelta(minutes=cap_updated_minutes_ago)
    fetch_queue: list[Any] = [
        # 1. cap_flipped_by
        (flipped_by,) if flipped_by is not None else (None,),
        # 2. circuit_breaker_enabled
        (enabled,),
        # 3. cap_updated_at row — the implementation reads value+updated_at
        #    to know how long ago the breaker opened.
        (0, cap_updated_at),
        # 4. window_minutes
        (window_minutes,),
        # 5. window_size
        (window_size,),
        # 6. bad-outcome threshold
        (threshold,),
    ]
    # 7. terminal_outcomes scan — list of (status, latest_failure_category)
    #    pairs. ``bad_outcomes`` failed entries plus enough good ones to
    #    fill the window so the bad/total ratio is realistic.
    n_rows = min(window_size, max(bad_outcomes, 0) + 5)
    statuses: list[tuple[str, Any]] = [("failed", None)] * bad_outcomes + [
        ("succeeded", None)
    ] * max(0, n_rows - bad_outcomes)
    # 8. target_concurrency_cap. Only consumed when the auto-close
    #    proceeds; staging it unconditionally is harmless because
    #    fetch_queue is FIFO and unconsumed entries are ignored.
    if target_cap is not None:
        fetch_queue.append((target_cap,))
    else:
        fetch_queue.append((None,))

    conn.cursor_instance.fetch_queue = fetch_queue
    conn.cursor_instance.fetchall_queue.append(statuses)


# --------------------------------------------------------------------------
# Positive case — the regression scenario from the issue.
# --------------------------------------------------------------------------


class TestTimeBasedAutoClosePositiveCase:
    """The headline regression: window elapsed + bad_count under threshold."""

    def test_breaker_auto_closes_after_window_elapses_under_threshold(
        self, tmp_path: Path
    ) -> None:
        """Stage cap=0/flipped_by=circuit_breaker/35min ago/2 bad → cap=4, flag null."""
        d, conn, handler = _make_daemon(tmp_path)
        _stage_time_based_state(
            conn,
            flipped_by="circuit_breaker",
            cap_updated_minutes_ago=35,
            window_minutes=30,
            threshold=5,
            bad_outcomes=2,
            target_cap=4,
        )

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is True

        # concurrency_cap UPDATE fired with the target value.
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "concurrency_cap" in e[0]
        ]
        assert len(cap_updates) == 1, "expected exactly one UPDATE on concurrency_cap"
        # Either the new value was passed as a param or embedded in the
        # SQL. Accept both shapes; concrete fix uses %s param so check
        # the params tuple first.
        sql, params = cap_updates[0]
        if params is not None:
            assert "4" in str(params), (
                f"expected target=4 in UPDATE params, got {params!r}"
            )
        else:
            assert "'4'" in sql or "= '4'" in sql, (
                f"expected target=4 embedded in UPDATE SQL, got {sql!r}"
            )

        # cap_flipped_by UPDATE cleared the flag.
        flag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "cap_flipped_by" in e[0]
        ]
        assert len(flag_updates) == 1
        # Auto-close event logged.
        events = handler.events("circuit_breaker_auto_closed")
        assert len(events) == 1
        ev = events[0]
        assert getattr(ev, "previous_cap", None) == 0
        assert getattr(ev, "new_cap", None) == 4
        assert getattr(ev, "bad_count", None) == 2
        assert getattr(ev, "threshold", None) == 5

    def test_default_target_when_target_row_missing(self, tmp_path: Path) -> None:
        """No ``target_concurrency_cap`` row → reset uses 1 (legacy ``start`` semantics)."""
        d, conn, handler = _make_daemon(tmp_path)
        _stage_time_based_state(
            conn,
            flipped_by="circuit_breaker",
            cap_updated_minutes_ago=35,
            window_minutes=30,
            threshold=5,
            bad_outcomes=0,
            target_cap=None,  # row missing
        )

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is True
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "concurrency_cap" in e[0]
        ]
        assert len(cap_updates) == 1
        sql, params = cap_updates[0]
        if params is not None:
            assert "1" in str(params), (
                f"expected default target=1 in UPDATE params, got {params!r}"
            )
        else:
            assert "'1'" in sql or "= '1'" in sql
        events = handler.events("circuit_breaker_auto_closed")
        assert len(events) == 1
        assert getattr(events[0], "new_cap", None) == 1


# --------------------------------------------------------------------------
# Negative cases — the breaker must NOT auto-close prematurely.
# --------------------------------------------------------------------------


class TestTimeBasedAutoCloseNegativeCases:
    """The breaker must stay open until both conditions are satisfied."""

    def test_no_close_when_bad_count_still_over_threshold(self, tmp_path: Path) -> None:
        """Window elapsed but 6/N still bad → cap stays 0, flag stays."""
        d, conn, handler = _make_daemon(tmp_path)
        _stage_time_based_state(
            conn,
            flipped_by="circuit_breaker",
            cap_updated_minutes_ago=35,
            window_minutes=30,
            threshold=5,
            bad_outcomes=6,  # OVER threshold
            target_cap=4,
        )

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False
        # No UPDATE — neither cap nor flag.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []
        assert handler.events("circuit_breaker_auto_closed") == []

    def test_no_close_when_window_not_yet_elapsed(self, tmp_path: Path) -> None:
        """Only 5 min since breaker opened — must wait the full window."""
        d, conn, handler = _make_daemon(tmp_path)
        _stage_time_based_state(
            conn,
            flipped_by="circuit_breaker",
            cap_updated_minutes_ago=5,  # well under 30
            window_minutes=30,
            threshold=5,
            bad_outcomes=0,  # would otherwise qualify
            target_cap=4,
        )

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []
        assert handler.events("circuit_breaker_auto_closed") == []

    def test_no_close_when_flag_not_circuit_breaker(self, tmp_path: Path) -> None:
        """cap=0 but flipped_by is null — operator paused, not the breaker."""
        d, conn, handler = _make_daemon(tmp_path)
        # flipped_by None → short-circuit: only one fetch needed.
        conn.cursor_instance.fetch_queue = [(None,)]

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False
        # No UPDATE.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []
        assert handler.events("circuit_breaker_auto_closed") == []

    def test_no_close_when_flag_is_operator_string(self, tmp_path: Path) -> None:
        """Flag set to ``"operator"`` — not our concern."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("operator",)]

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []

    def test_no_close_when_breaker_disabled(self, tmp_path: Path) -> None:
        """``circuit_breaker_enabled=false`` → leave the open state alone.

        If the operator has disabled the breaker entirely, the
        time-based auto-close also yields — manual intervention is
        the contract under that knob.
        """
        d, conn, handler = _make_daemon(tmp_path)
        # cap_flipped_by + enabled=false → no further reads.
        conn.cursor_instance.fetch_queue = [
            ("circuit_breaker",),
            (False,),
        ]

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []


# --------------------------------------------------------------------------
# Backwards-compat — the operator-re-flip path (cap≥1) still works.
# This duplicates a slice of TestAutoClose in
# ``test_daemon_circuit_breaker.py``; keeping it here makes #3779's
# regression file self-contained.
# --------------------------------------------------------------------------


class TestOperatorReflipBackwardsCompat:
    """Pre-#3779 path: operator manually raises cap → flag clears."""

    def test_operator_reflip_still_clears_flag(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("circuit_breaker",)]

        closed = d._check_circuit_breaker_auto_close(current_cap=1)

        assert closed is True
        flag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "cap_flipped_by" in e[0]
        ]
        assert len(flag_updates) == 1
        events = handler.events("circuit_breaker_closed")
        assert len(events) == 1


# --------------------------------------------------------------------------
# Defensive — DB errors during auto-close must not raise.
# --------------------------------------------------------------------------


class TestTimeBasedAutoCloseFailureModes:
    """A failure in the auto-close path must never crash the scheduler tick."""

    def test_db_error_during_cap_flipped_by_read_returns_false(
        self, tmp_path: Path
    ) -> None:
        d, conn, _h = _make_daemon(tmp_path)

        class _BoomCursor(_FakeCursor):
            def execute(self, sql: str, params: Any = None) -> None:
                raise RuntimeError("simulated DB failure on flag read")

        conn.cursor_instance = _BoomCursor()

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False


# Keep ``json`` referenced so static-analysis doesn't strip the import —
# legacy fake-cursor JSONB tests in this suite may add cases that need
# it.
_ = json
