"""Unit tests for the daemon housekeeping tick (issues #2778, #2779).

Covers the behaviours added on top of Phase 2:

* ``_housekeeping_tick`` issues a parameterised DELETE per target in
  ``_HOUSEKEEPING_TARGETS`` using the retention window read from
  ``dispatcher.config`` (falls back to the per-target default).
* Per-target behaviour is verified for all three live targets:
  ``queue_snapshots``, ``phase_outputs``, ``notifications``.
* Idempotency — calling the tick twice is safe; the second call commits
  (possibly deleting zero rows) without raising.
* Failure isolation — a DB error during one target's DELETE is caught,
  logged as ``housekeeping_failed``, rolled back, and does not propagate;
  the tick still runs sibling targets.

Single-target tests monkeypatch ``_HOUSEKEEPING_TARGETS`` to isolate the
target under test. Siblings are covered by
``test_failure_on_one_target_does_not_stop_siblings`` and
``test_live_targets_all_prune_in_one_tick``.

All DB interaction is against the same ``_FakeCursor`` / ``_FakeConnection``
stubs used by ``test_daemon_phase2.py``; no real Postgres is needed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_phase2.py shape)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
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
    """Collect emitted ``LogRecord``s so tests can assert on them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        """All captured records whose ``extra.event`` equals ``name``."""
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon_with_capture() -> tuple[
    daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler
]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger("dispatcher.test.housekeeping")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake-for-tests",
        tick_scheduler_seconds=30,
        tick_supervisor_seconds=120,
        tick_housekeeping_seconds=3600,
        log_level="DEBUG",
        version_sha="deadbee",
        host="test-host",
        pid=4242,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-dev",
        heartbeat_metric_namespace="Judgemind/Dispatcher",
        aws_region="us-west-2",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]  — test stub
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# _read_retention_days — config lookup
# --------------------------------------------------------------------------


class TestReadRetentionDays:
    """Config override falls back cleanly to the hardcoded default."""

    def test_returns_config_override_when_set(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        # dispatcher.config returns 7 for the retention key.
        conn.cursor_instance.fetch_queue = [(7,)]
        assert d._read_retention_days("queue_snapshot_retention_days", 30) == 7

    def test_returns_default_when_row_missing(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]
        assert d._read_retention_days("queue_snapshot_retention_days", 30) == 30

    def test_returns_default_when_value_null(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [(None,)]
        assert d._read_retention_days("queue_snapshot_retention_days", 30) == 30

    def test_returns_default_when_value_non_numeric(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [("banana",)]
        assert d._read_retention_days("queue_snapshot_retention_days", 30) == 30

    def test_returns_default_when_value_non_positive(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [(0,)]
        assert d._read_retention_days("queue_snapshot_retention_days", 30) == 30

    def test_returns_default_on_db_error(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("connection lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        # Must not raise; returns the hardcoded default.
        assert d._read_retention_days("queue_snapshot_retention_days", 30) == 30
        assert conn.rollbacks >= 1


# --------------------------------------------------------------------------
# _housekeeping_tick — DELETE + cutoff + structured log
# --------------------------------------------------------------------------


def _isolate_targets_to(
    monkeypatch: pytest.MonkeyPatch, table: str
) -> tuple[str, str, str, int]:
    """Monkeypatch ``_HOUSEKEEPING_TARGETS`` to a single live-target entry.

    Looks up the real tuple in the daemon module and narrows it to the
    one target under test. Avoids hand-copying column names (which would
    drift from the production tuple) and ensures the per-target tests
    exercise the real configuration, not synthetic stand-ins.
    """
    entry = next(
        t for t in daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS if t[0] == table
    )
    monkeypatch.setattr(daemon.DispatcherDaemon, "_HOUSEKEEPING_TARGETS", (entry,))
    return entry


class TestHousekeepingTickQueueSnapshots:
    """DELETE + structured log for the ``queue_snapshots`` target (#2778)."""

    def test_deletes_with_default_cutoff_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "queue_snapshots")
        d, conn, handler = _make_daemon_with_capture()
        # First execute() is the config SELECT (returns None → default).
        # Second execute() is the DELETE; 1234 rows swept.
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.queue_snapshots" in sql:
                conn.cursor_instance.rowcount = 1234

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        # One DELETE bound to the default cutoff (30 days).
        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.queue_snapshots")
        ]
        assert len(deletes) == 1
        sql, params = deletes[0]
        assert "observed_at < now() - make_interval(days => %s)" in sql
        assert params == (daemon.DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS,)

        # Structured log emitted with the four required fields.
        ticks = handler.events("housekeeping_tick")
        assert len(ticks) == 1
        record = ticks[0]
        assert record.table == "queue_snapshots"
        assert record.rows_deleted == 1234
        assert record.cutoff_days == daemon.DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS
        assert record.run_id == "test-run-id"

        # Result dict surfaces the rowcount.
        assert result == {"queue_snapshots": 1234}
        # Counter advances.
        assert d._housekeeping_ticks == 1

    def test_uses_config_override_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "queue_snapshots")
        d, conn, handler = _make_daemon_with_capture()
        # Config row sets retention to 7 days.
        conn.cursor_instance.fetch_queue = [(7,)]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.queue_snapshots" in sql:
                conn.cursor_instance.rowcount = 10

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        d._housekeeping_tick()

        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.queue_snapshots")
        ]
        assert deletes[0][1] == (7,)
        # Log line also reflects the override.
        ticks = handler.events("housekeeping_tick")
        assert ticks[0].cutoff_days == 7

    def test_idempotent_second_call_logs_zero_deletes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running the tick twice with no new rows to prune is safe.

        Mirrors the real-world steady state: after the first hourly
        sweep catches up, subsequent ticks should delete 0 rows and emit
        a clean ``housekeeping_tick`` event with ``rows_deleted=0``.
        """
        _isolate_targets_to(monkeypatch, "queue_snapshots")
        d, conn, handler = _make_daemon_with_capture()
        # Two ticks × (config lookup returns None → default, DELETE → 0 rows).
        conn.cursor_instance.fetch_queue = [None, None]
        # rowcount stays at 0 — nothing to delete.

        d._housekeeping_tick()
        d._housekeeping_tick()

        ticks = handler.events("housekeeping_tick")
        assert len(ticks) == 2
        assert all(t.rows_deleted == 0 for t in ticks)
        # Two DELETE statements, same bound cutoff.
        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.queue_snapshots")
        ]
        assert len(deletes) == 2
        assert all(
            p == (daemon.DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS,) for _, p in deletes
        )
        assert d._housekeeping_ticks == 2

    def test_db_failure_logs_housekeeping_failed_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DB error during the DELETE must not crash the daemon.

        The tick catches the exception, rolls back, emits a
        ``housekeeping_failed`` event, and returns a ``-1`` sentinel for
        the failing table so the caller knows the sweep failed.
        """
        _isolate_targets_to(monkeypatch, "queue_snapshots")
        d, conn, handler = _make_daemon_with_capture()
        # Config lookup returns None → default.
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            if "DELETE FROM dispatcher.queue_snapshots" in sql:
                raise RuntimeError("deadlock detected")
            original_execute(sql, params)

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        # Must not raise out of the tick.
        result = d._housekeeping_tick()

        # ``housekeeping_failed`` emitted with detail + table.
        failures = handler.events("housekeeping_failed")
        assert len(failures) == 1
        assert failures[0].table == "queue_snapshots"
        assert failures[0].cutoff_days == (daemon.DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS)
        assert "deadlock" in failures[0].detail
        # No success event.
        assert handler.events("housekeeping_tick") == []
        # Rollback happened.
        assert conn.rollbacks >= 1
        # Sentinel returned.
        assert result == {"queue_snapshots": -1}
        # Counter still advances — ``_housekeeping_ticks`` is a
        # loop-level counter, not a success counter.
        assert d._housekeeping_ticks == 1


class TestHousekeepingTickPhaseOutputs:
    """DELETE + structured log for the ``phase_outputs`` target (#2779).

    Mirrors ``TestHousekeepingTickQueueSnapshots`` but targets the
    ``phase_outputs`` entry added in #2779. The cutoff column is ``ts``
    and the default retention is ``DEFAULT_PHASE_OUTPUT_RETENTION_DAYS``.
    """

    def test_deletes_with_default_cutoff_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "phase_outputs")
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.phase_outputs" in sql:
                conn.cursor_instance.rowcount = 77

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.phase_outputs")
        ]
        assert len(deletes) == 1
        sql, params = deletes[0]
        # phase_outputs uses the ``ts`` column (see migration 21).
        assert "ts < now() - make_interval(days => %s)" in sql
        assert params == (daemon.DEFAULT_PHASE_OUTPUT_RETENTION_DAYS,)

        ticks = handler.events("housekeeping_tick")
        assert len(ticks) == 1
        record = ticks[0]
        assert record.table == "phase_outputs"
        assert record.rows_deleted == 77
        assert record.cutoff_days == daemon.DEFAULT_PHASE_OUTPUT_RETENTION_DAYS
        assert record.run_id == "test-run-id"

        assert result == {"phase_outputs": 77}
        assert d._housekeeping_ticks == 1

    def test_uses_config_override_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "phase_outputs")
        d, conn, handler = _make_daemon_with_capture()
        # Operator shortens phase_outputs retention to 14 days.
        conn.cursor_instance.fetch_queue = [(14,)]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.phase_outputs" in sql:
                conn.cursor_instance.rowcount = 5

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        d._housekeeping_tick()

        # Config lookup hit the right key.
        configs = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT value FROM dispatcher.config" in e[0]
        ]
        assert configs[0][1] == ("phase_output_retention_days",)
        # DELETE bound to the override.
        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.phase_outputs")
        ]
        assert deletes[0][1] == (14,)
        ticks = handler.events("housekeeping_tick")
        assert ticks[0].cutoff_days == 14

    def test_idempotent_second_call_logs_zero_deletes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "phase_outputs")
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None, None]

        d._housekeeping_tick()
        d._housekeeping_tick()

        ticks = handler.events("housekeeping_tick")
        assert len(ticks) == 2
        assert all(t.table == "phase_outputs" for t in ticks)
        assert all(t.rows_deleted == 0 for t in ticks)
        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.phase_outputs")
        ]
        assert len(deletes) == 2
        assert all(
            p == (daemon.DEFAULT_PHASE_OUTPUT_RETENTION_DAYS,) for _, p in deletes
        )
        assert d._housekeeping_ticks == 2

    def test_db_failure_logs_housekeeping_failed_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "phase_outputs")
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            if "DELETE FROM dispatcher.phase_outputs" in sql:
                raise RuntimeError("connection reset")
            original_execute(sql, params)

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        failures = handler.events("housekeeping_failed")
        assert len(failures) == 1
        assert failures[0].table == "phase_outputs"
        assert failures[0].cutoff_days == daemon.DEFAULT_PHASE_OUTPUT_RETENTION_DAYS
        assert "connection reset" in failures[0].detail
        assert handler.events("housekeeping_tick") == []
        assert conn.rollbacks >= 1
        assert result == {"phase_outputs": -1}
        assert d._housekeeping_ticks == 1


class TestHousekeepingTickNotifications:
    """DELETE + structured log for the ``notifications`` target (#2779).

    Mirrors ``TestHousekeepingTickQueueSnapshots`` but targets the
    ``notifications`` entry added in #2779. The cutoff column is
    ``created_at`` and the default retention is
    ``DEFAULT_NOTIFICATION_RETENTION_DAYS``.
    """

    def test_deletes_with_default_cutoff_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "notifications")
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.notifications" in sql:
                conn.cursor_instance.rowcount = 321

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.notifications")
        ]
        assert len(deletes) == 1
        sql, params = deletes[0]
        # notifications uses the ``created_at`` column (see migration 21).
        assert "created_at < now() - make_interval(days => %s)" in sql
        assert params == (daemon.DEFAULT_NOTIFICATION_RETENTION_DAYS,)

        ticks = handler.events("housekeeping_tick")
        assert len(ticks) == 1
        record = ticks[0]
        assert record.table == "notifications"
        assert record.rows_deleted == 321
        assert record.cutoff_days == daemon.DEFAULT_NOTIFICATION_RETENTION_DAYS
        assert record.run_id == "test-run-id"

        assert result == {"notifications": 321}
        assert d._housekeeping_ticks == 1

    def test_uses_config_override_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "notifications")
        d, conn, handler = _make_daemon_with_capture()
        # Operator shortens notification retention to 3 days.
        conn.cursor_instance.fetch_queue = [(3,)]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.notifications" in sql:
                conn.cursor_instance.rowcount = 1

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        d._housekeeping_tick()

        configs = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT value FROM dispatcher.config" in e[0]
        ]
        assert configs[0][1] == ("notification_retention_days",)
        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.notifications")
        ]
        assert deletes[0][1] == (3,)
        ticks = handler.events("housekeeping_tick")
        assert ticks[0].cutoff_days == 3

    def test_idempotent_second_call_logs_zero_deletes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "notifications")
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None, None]

        d._housekeeping_tick()
        d._housekeeping_tick()

        ticks = handler.events("housekeeping_tick")
        assert len(ticks) == 2
        assert all(t.table == "notifications" for t in ticks)
        assert all(t.rows_deleted == 0 for t in ticks)
        deletes = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("DELETE FROM dispatcher.notifications")
        ]
        assert len(deletes) == 2
        assert all(
            p == (daemon.DEFAULT_NOTIFICATION_RETENTION_DAYS,) for _, p in deletes
        )
        assert d._housekeeping_ticks == 2

    def test_db_failure_logs_housekeeping_failed_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_targets_to(monkeypatch, "notifications")
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            if "DELETE FROM dispatcher.notifications" in sql:
                raise RuntimeError("serialization failure")
            original_execute(sql, params)

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        failures = handler.events("housekeeping_failed")
        assert len(failures) == 1
        assert failures[0].table == "notifications"
        assert failures[0].cutoff_days == daemon.DEFAULT_NOTIFICATION_RETENTION_DAYS
        assert "serialization failure" in failures[0].detail
        assert handler.events("housekeeping_tick") == []
        assert conn.rollbacks >= 1
        assert result == {"notifications": -1}
        assert d._housekeeping_ticks == 1


class TestHousekeepingTickAllLiveTargets:
    """Whole-tuple checks — the live ``_HOUSEKEEPING_TARGETS`` tuple.

    These run against the production tuple (no monkeypatch) so they
    catch regressions where someone drops a target or reorders entries.
    """

    def test_live_targets_all_prune_in_one_tick(self) -> None:
        """Every live target runs its DELETE in a single tick.

        Covers the issue #2779 acceptance criterion that
        ``phase_outputs`` and ``notifications`` DELETEs both appear in
        the housekeeping path alongside ``queue_snapshots``.
        """
        d, conn, handler = _make_daemon_with_capture()
        # One config lookup per target returning None → each falls back
        # to its per-target default.
        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count
        # rowcount stays at 0 — we only care that each DELETE fires.

        d._housekeeping_tick()

        # One housekeeping_tick event per live target.
        ticks = handler.events("housekeeping_tick")
        tick_tables = {t.table for t in ticks}
        assert tick_tables == {
            "queue_snapshots",
            "blocked_snapshots",
            "phase_outputs",
            "notifications",
            "ralph_patches",
        }

        # One DELETE per live target, and each uses the right column.
        deletes_by_table = {}
        for sql, params in conn.cursor_instance.executed:
            if sql.startswith("DELETE FROM dispatcher."):
                # Extract table name from "DELETE FROM dispatcher.<table> ..."
                table = sql.split("dispatcher.", 1)[1].split()[0]
                deletes_by_table[table] = (sql, params)
        assert set(deletes_by_table) == {
            "queue_snapshots",
            "blocked_snapshots",
            "phase_outputs",
            "notifications",
            "ralph_patches",
        }
        # Per-target column assertions — matches the migration schema.
        assert "observed_at <" in deletes_by_table["queue_snapshots"][0]
        assert "observed_at <" in deletes_by_table["blocked_snapshots"][0]
        assert "ts <" in deletes_by_table["phase_outputs"][0]
        assert "created_at <" in deletes_by_table["notifications"][0]
        assert "created_at <" in deletes_by_table["ralph_patches"][0]

    def test_per_target_failure_isolation_across_live_targets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DB error on ``phase_outputs`` must not starve ``notifications``.

        Directly exercises the AC "Each DELETE is independently
        try/except wrapped — one table's DB error doesn't skip the
        others" on the new targets added in #2779.
        """
        d, conn, handler = _make_daemon_with_capture()
        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count

        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            if "DELETE FROM dispatcher.phase_outputs" in sql:
                raise RuntimeError("phase_outputs deadlock")
            original_execute(sql, params)
            if "DELETE FROM dispatcher.notifications" in sql:
                conn.cursor_instance.rowcount = 9

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        # phase_outputs logged failure; queue_snapshots and notifications
        # still logged success.
        failures = handler.events("housekeeping_failed")
        assert [f.table for f in failures] == ["phase_outputs"]

        successes = {t.table for t in handler.events("housekeeping_tick")}
        assert "queue_snapshots" in successes
        assert "notifications" in successes
        assert "phase_outputs" not in successes

        # Result surfaces the -1 sentinel for the failing target and the
        # rowcount for the siblings (which ran to completion).
        assert result["phase_outputs"] == -1
        assert result["notifications"] == 9

    def test_failure_on_one_target_does_not_stop_siblings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Abstract-mechanism check: per-target failure isolation.

        Complements ``test_per_target_failure_isolation_across_live_targets``
        (which uses the live tuple) by exercising the generic iteration
        contract with synthetic targets — if a future target is added
        and the loop ever becomes short-circuited, this test fails even
        when the live-tuple test does not.
        """
        d, conn, handler = _make_daemon_with_capture()

        # Two fake targets. First one raises on DELETE; second one
        # succeeds. Both have their own config lookup returning None.
        # Column names are synthetic — the test only cares about the
        # loop contract, not the real schema.
        fake_targets = (
            ("queue_snapshots", "observed_at", "queue_snapshot_retention_days", 30),
            ("phase_outputs", "emitted_at", "phase_outputs_retention_days", 14),
        )
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_HOUSEKEEPING_TARGETS", fake_targets
        )
        conn.cursor_instance.fetch_queue = [None, None]

        original_execute = conn.cursor_instance.execute

        def partial_failure(sql: str, params: Any = None) -> None:
            if "DELETE FROM dispatcher.queue_snapshots" in sql:
                raise RuntimeError("deadlock detected")
            original_execute(sql, params)
            if "DELETE FROM dispatcher.phase_outputs" in sql:
                conn.cursor_instance.rowcount = 42

        conn.cursor_instance.execute = partial_failure  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        # Failure event for the first target.
        failures = handler.events("housekeeping_failed")
        assert len(failures) == 1
        assert failures[0].table == "queue_snapshots"

        # Success event for the second target — proves sibling isolation.
        successes = handler.events("housekeeping_tick")
        assert len(successes) == 1
        assert successes[0].table == "phase_outputs"
        assert successes[0].rows_deleted == 42
        assert successes[0].cutoff_days == 14

        assert result == {"queue_snapshots": -1, "phase_outputs": 42}


# --------------------------------------------------------------------------
# Config / CLI plumbing
# --------------------------------------------------------------------------


class TestHousekeepingConfigPlumbing:
    """CLI flag + DaemonConfig default wiring."""

    def test_build_config_default_housekeeping_tick_seconds(self) -> None:
        args = daemon._parse_args([])
        cfg = daemon._build_config(args, env={"DATABASE_URL": "postgres://x"})
        assert cfg.tick_housekeeping_seconds == daemon.DEFAULT_HOUSEKEEPING_TICK_SECONDS

    def test_build_config_respects_cli_override(self) -> None:
        args = daemon._parse_args(["--tick-housekeeping-seconds", "300"])
        cfg = daemon._build_config(args, env={"DATABASE_URL": "postgres://x"})
        assert cfg.tick_housekeeping_seconds == 300

    def test_housekeeping_targets_includes_queue_snapshots(self) -> None:
        """The #2778 AC mandates a queue_snapshots entry with expected defaults."""
        targets = daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS
        names = [t[0] for t in targets]
        assert "queue_snapshots" in names
        entry = next(t for t in targets if t[0] == "queue_snapshots")
        assert entry[1] == "observed_at"
        assert entry[2] == "queue_snapshot_retention_days"
        assert entry[3] == daemon.DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS

    def test_housekeeping_targets_includes_phase_outputs(self) -> None:
        """#2779 AC: phase_outputs entry uses ``ts`` and 30-day default."""
        targets = daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS
        names = [t[0] for t in targets]
        assert "phase_outputs" in names
        entry = next(t for t in targets if t[0] == "phase_outputs")
        assert entry[1] == "ts"
        assert entry[2] == "phase_output_retention_days"
        assert entry[3] == daemon.DEFAULT_PHASE_OUTPUT_RETENTION_DAYS
        assert entry[3] == 30

    def test_housekeeping_targets_includes_notifications(self) -> None:
        """#2779 AC: notifications entry uses ``created_at`` and 30-day default."""
        targets = daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS
        names = [t[0] for t in targets]
        assert "notifications" in names
        entry = next(t for t in targets if t[0] == "notifications")
        assert entry[1] == "created_at"
        assert entry[2] == "notification_retention_days"
        assert entry[3] == daemon.DEFAULT_NOTIFICATION_RETENTION_DAYS
        assert entry[3] == 30


# --------------------------------------------------------------------------
# #3801 — bulk-clear stale agent_task_arn rows.
#
# The pre-#3801 reaper iterated ``_reap_finalize_ecs_success`` over every
# row whose ARN ECS no longer had metadata for. At observed
# ``reap_active=75 / reap_untracked=74`` that's 74-148 sequential ``gh``
# subprocess calls per scheduler tick, which holds the GIL long enough
# that the watchdog cannot preempt — the load-bearing wedge of
# 2026-04-29.
#
# The fix consolidated stale-ARN cleanup into a single bulk SQL UPDATE
# in :meth:`_housekeeping_tick`. These tests verify:
# - the UPDATE clears 100 stale rows in one round-trip,
# - it does NOT touch fresh rows (started_at within the cutoff),
# - it uses the configured cutoff (``stale_arn_clear_age_hours`` config
#   key, default 2h),
# - it surfaces the cleared count in a structured log event,
# - it is wired into ``_housekeeping_tick`` (regression test against
#   accidental decoupling).
# --------------------------------------------------------------------------


class TestClearStaleAgentTaskArns:
    """Bulk-clear stale ``agent_task_arn`` rows (#3801)."""

    def test_default_cutoff_is_2_hours(self) -> None:
        """The default ``stale_arn_clear_age_hours`` is 2h (matches the
        spec — gives the reaper one full Fargate retention window
        before housekeeping pre-empts it)."""
        assert daemon.DEFAULT_STALE_ARN_CLEAR_AGE_HOURS == 2

    def test_uses_default_cutoff_when_config_missing(self) -> None:
        """No ``stale_arn_clear_age_hours`` row in ``dispatcher.config``
        falls back to :data:`DEFAULT_STALE_ARN_CLEAR_AGE_HOURS`."""
        d, conn, handler = _make_daemon_with_capture()
        # Config lookup returns None → default. Then the UPDATE runs.
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "UPDATE dispatcher.agents" in sql and "agent_task_arn = NULL" in sql:
                # Pretend 100 stale rows were cleared.
                conn.cursor_instance.rowcount = 100

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        cleared = d._clear_stale_agent_task_arns()

        # The UPDATE was issued with the default cutoff bound via %s.
        updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if sql.startswith("UPDATE dispatcher.agents")
        ]
        assert len(updates) == 1
        sql, params = updates[0]
        assert "SET agent_task_arn = NULL" in sql
        assert "agent_task_arn IS NOT NULL" in sql
        assert "started_at < now() - make_interval(hours => %s)" in sql
        assert params == (daemon.DEFAULT_STALE_ARN_CLEAR_AGE_HOURS,)
        # rowcount surfaces.
        assert cleared == 100

        # Structured log event with rows_cleared + cutoff_hours.
        events = handler.events("stale_arn_bulk_cleared")
        assert len(events) == 1
        rec = events[0]
        assert rec.rows_cleared == 100
        assert rec.cutoff_hours == daemon.DEFAULT_STALE_ARN_CLEAR_AGE_HOURS
        assert rec.run_id == "test-run-id"

    def test_uses_config_override_when_set(self) -> None:
        """An override row in ``dispatcher.config`` overrides the default."""
        d, conn, _handler = _make_daemon_with_capture()
        # Config lookup returns 6 — operator wants a tighter window.
        conn.cursor_instance.fetch_queue = [(6,)]

        d._clear_stale_agent_task_arns()

        updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if sql.startswith("UPDATE dispatcher.agents")
        ]
        assert len(updates) == 1
        assert updates[0][1] == (6,)

    def test_clears_100_stale_rows_in_one_update(self) -> None:
        """Regression test for the wedge cause: bulk SQL clears the entire
        backlog in one round-trip — NOT one UPDATE per row."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "UPDATE dispatcher.agents" in sql:
                conn.cursor_instance.rowcount = 100

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        cleared = d._clear_stale_agent_task_arns()

        # ONE UPDATE statement — not 100.
        updates = [
            sql
            for sql, _params in conn.cursor_instance.executed
            if sql.startswith("UPDATE dispatcher.agents")
        ]
        assert len(updates) == 1
        assert cleared == 100

    def test_does_not_touch_fresh_rows(self) -> None:
        """The UPDATE filters by ``started_at < now() - interval`` so
        rows whose ``started_at`` is within the window are unaffected.

        We verify the SQL shape — the actual filter behaviour is tested
        end-to-end against a real Postgres in the CI integration shard.
        Here we just pin the WHERE clause so an accidental drop of the
        ``started_at`` filter would fail the unit test.
        """
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]

        d._clear_stale_agent_task_arns()

        updates = [
            sql
            for sql, _params in conn.cursor_instance.executed
            if sql.startswith("UPDATE dispatcher.agents")
        ]
        assert len(updates) == 1
        # Both filters present — without them the UPDATE would null
        # every row, including fresh ones.
        assert "agent_task_arn IS NOT NULL" in updates[0]
        assert "started_at < now() - make_interval(hours => %s)" in updates[0]

    def test_zero_rows_cleared_emits_zero_event(self) -> None:
        """Steady-state: after the first sweep catches up, future runs
        clear zero rows. The event must still emit so operators can see
        the housekeeping ran."""
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [None]
        # rowcount stays at 0.

        cleared = d._clear_stale_agent_task_arns()
        assert cleared == 0

        events = handler.events("stale_arn_bulk_cleared")
        assert len(events) == 1
        assert events[0].rows_cleared == 0


class TestHousekeepingTickWiresInBulkClear:
    """``_housekeeping_tick`` calls ``_clear_stale_agent_task_arns`` (#3801).

    Regression: an accidental decouple (e.g. removing the call in
    ``_housekeeping_tick``) would silently break the wedge fix.
    """

    def test_housekeeping_tick_calls_clear_stale_agent_task_arns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, handler = _make_daemon_with_capture()

        called = {"count": 0}

        def fake_clear() -> int:
            called["count"] += 1
            return 7

        monkeypatch.setattr(d, "_clear_stale_agent_task_arns", fake_clear, raising=True)

        # Stub out reconcile so the tick doesn't try to talk to GitHub.
        monkeypatch.setattr(
            d,
            "_reconcile_stale_merged_at",
            lambda: {"checked": 0, "cleared": 0, "errors": 0},
        )

        d._housekeeping_tick()
        # Bulk-clear MUST have been called exactly once per tick.
        assert called["count"] == 1
        # The tick-level counter still advances.
        assert d._housekeeping_ticks == 1
        # Nothing in the prune loop logged a failure.
        assert handler.events("stale_arn_bulk_clear_failed") == []

    def test_housekeeping_tick_isolates_bulk_clear_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``_clear_stale_agent_task_arns`` raises, the tick logs and
        continues. Per-target prune results are not affected."""
        d, conn, handler = _make_daemon_with_capture()

        def boom() -> int:
            raise RuntimeError("connection lost")

        monkeypatch.setattr(d, "_clear_stale_agent_task_arns", boom, raising=True)
        monkeypatch.setattr(
            d,
            "_reconcile_stale_merged_at",
            lambda: {"checked": 0, "cleared": 0, "errors": 0},
        )

        # Make the per-target prune trivially succeed.
        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count

        # MUST NOT raise.
        result = d._housekeeping_tick()

        # Failure event surfaced.
        failures = handler.events("stale_arn_bulk_clear_failed")
        assert len(failures) == 1
        # Per-target sweeps still ran.
        assert "queue_snapshots" in result
        # Counter still advances.
        assert d._housekeeping_ticks == 1


class TestStaleArnConfigPlumbing:
    """Constants + config key shape (#3801)."""

    def test_default_constant_is_module_level(self) -> None:
        assert hasattr(daemon, "DEFAULT_STALE_ARN_CLEAR_AGE_HOURS")
        assert isinstance(daemon.DEFAULT_STALE_ARN_CLEAR_AGE_HOURS, int)
        assert daemon.DEFAULT_STALE_ARN_CLEAR_AGE_HOURS > 0

    def test_backup_watchdog_constants_are_gone(self) -> None:
        """#3801 deleted the backup watchdog (#3794). Its module-level
        constants must not be re-introduced — the per-row reaper wedge
        cause is now gone, so the backup watchdog has no rationale.
        """
        assert not hasattr(daemon, "DEFAULT_BACKUP_WATCHDOG_EXIT_THRESHOLD_SECONDS")
        assert not hasattr(daemon, "BACKUP_WATCHDOG_POLL_INTERVAL_SECONDS")
