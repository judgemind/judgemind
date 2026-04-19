"""Unit tests for the daemon housekeeping tick (issue #2778).

Covers the three behaviours added on top of Phase 2:

* ``_housekeeping_tick`` issues a parameterised DELETE against
  ``dispatcher.queue_snapshots`` using the retention window read from
  ``dispatcher.config`` (falls back to the 30-day default).
* Idempotency — calling the tick twice is safe; the second call commits
  (possibly deleting zero rows) without raising.
* Failure isolation — a DB error during the DELETE is caught, logged as
  ``housekeeping_failed``, rolled back, and does not propagate. The
  tick still returns (so sibling tables in the targets list can proceed).

All DB interaction is against the same ``_FakeCursor`` / ``_FakeConnection``
stubs used by ``test_daemon_phase2.py``; no real Postgres is needed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Ensure a ``psycopg`` module exists in sys.modules before importing
# the daemon. Mirrors the pattern used in test_daemon_phase2.py.
if "psycopg" not in sys.modules:  # pragma: no cover — fresh-venv guard
    sys.modules["psycopg"] = MagicMock()

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


class TestHousekeepingTick:
    """DELETE + structured log matches the spec in the issue body."""

    def test_deletes_with_default_cutoff_and_logs(self) -> None:
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

    def test_uses_config_override_when_set(self) -> None:
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

    def test_idempotent_second_call_logs_zero_deletes(self) -> None:
        """Running the tick twice with no new rows to prune is safe.

        Mirrors the real-world steady state: after the first hourly
        sweep catches up, subsequent ticks should delete 0 rows and emit
        a clean ``housekeeping_tick`` event with ``rows_deleted=0``.
        """
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

    def test_db_failure_logs_housekeeping_failed_and_continues(self) -> None:
        """A DB error during the DELETE must not crash the daemon.

        The tick catches the exception, rolls back, emits a
        ``housekeeping_failed`` event, and returns a ``-1`` sentinel for
        the failing table so the caller knows the sweep failed.
        """
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

    def test_failure_on_one_target_does_not_stop_siblings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-target failure isolation — siblings still run.

        Protects issue #2779's extension path: when ``phase_outputs``
        and ``notifications`` join the targets tuple, a ``queue_snapshots``
        DELETE failure must not starve them of their cleanup.
        """
        d, conn, handler = _make_daemon_with_capture()

        # Two fake targets. First one raises on DELETE; second one
        # succeeds. Both have their own config lookup returning None.
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
        """The AC mandates a queue_snapshots entry with the expected defaults."""
        targets = daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS
        names = [t[0] for t in targets]
        assert "queue_snapshots" in names
        entry = next(t for t in targets if t[0] == "queue_snapshots")
        assert entry[1] == "observed_at"
        assert entry[2] == "queue_snapshot_retention_days"
        assert entry[3] == daemon.DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS
