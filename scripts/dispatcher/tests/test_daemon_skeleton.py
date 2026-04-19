"""Unit tests for ``scripts.dispatcher.daemon`` — Phase 1 skeleton.

Issue #2729. Mocks ``psycopg`` to exercise the daemon's DB interactions
without a real Postgres. Covers the five behaviors the issue explicitly
asks about:

- Lease check fires when an active peer row exists.
- Run registration inserts one row and captures the returned run_id.
- Supervisor tick UPDATEs ``dispatcher.runs.heartbeat_ts`` at least once.
- Scheduler tick marks all pending ``dispatcher.commands`` consumed.
- SIGTERM-equivalent (``request_stop``) sets ``stopped_at`` on shutdown.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Ensure a `psycopg` module exists in sys.modules before importing the
# daemon. The daemon lazily imports psycopg inside connect(); tests that
# patch `dispatcher.daemon.psycopg` via the real module path can still
# inject a mock through sys.modules, which is the pattern used for
# other scripts/ daemons (test_dev_db_query_runner.py et al.).
if "psycopg" not in sys.modules:  # pragma: no cover — exercised in fresh venvs only
    sys.modules["psycopg"] = MagicMock()

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------


def _make_logger() -> logging.Logger:
    """Return a silent logger — tests shouldn't care about log output."""
    logger = logging.getLogger("dispatcher.test")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class _FakeCursor:
    """Tiny stand-in for a psycopg cursor with scripted ``fetchone`` outputs.

    Records every ``execute`` call in ``executed`` so tests can assert on
    the SQL the daemon issues without parsing real SQL. Each test pushes
    the exact sequence of fetchone return values via ``fetch_queue``.
    """

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
    """Tracks commits / rollbacks and hands out a single cursor."""

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


def _make_daemon(
    *,
    fake_conn: _FakeConnection,
    tick_scheduler: int = 30,
    tick_supervisor: int = 120,
) -> daemon.DispatcherDaemon:
    """Build a daemon wired to ``fake_conn`` — skips the real connect() path."""
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake-for-tests",
        tick_scheduler_seconds=tick_scheduler,
        tick_supervisor_seconds=tick_supervisor,
        log_level="DEBUG",
        version_sha="deadbee",
        host="test-host",
        pid=4242,
    )
    d = daemon.DispatcherDaemon(cfg, _make_logger())
    d._conn = fake_conn  # type: ignore[assignment]  — test stub
    return d


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestLeaseCheck:
    """§17 Risk 2 — second daemon bails when first one's heartbeat is fresh."""

    def test_lease_held_raises(self) -> None:
        fake_conn = _FakeConnection()
        # SELECT ... returns a row → another daemon is active.
        fake_conn.cursor_instance.fetch_queue = [
            ("existing-run-id", "2026-04-18T00:00:00Z")
        ]

        d = _make_daemon(fake_conn=fake_conn)
        with pytest.raises(daemon.LeaseError) as excinfo:
            d.check_lease_and_register_run()
        assert "existing-run-id" in str(excinfo.value)
        # Lease contention rolls back, does not commit.
        assert fake_conn.rollbacks == 1
        assert fake_conn.commits == 0

    def test_no_active_peer_inserts_run(self) -> None:
        fake_conn = _FakeConnection()
        # SELECT returns None (no peer), INSERT RETURNING yields new run_id.
        fake_conn.cursor_instance.fetch_queue = [None, ("new-run-id",)]

        d = _make_daemon(fake_conn=fake_conn)
        run_id = d.check_lease_and_register_run()

        assert run_id == "new-run-id"
        assert d._run_id == "new-run-id"
        # Both lease SELECT and INSERT ran in the same transaction.
        executed = fake_conn.cursor_instance.executed
        assert any("FROM dispatcher.runs" in sql for sql, _ in executed)
        assert any("INSERT INTO dispatcher.runs" in sql for sql, _ in executed)
        assert fake_conn.commits == 1

    def test_insert_returns_no_row_raises(self) -> None:
        fake_conn = _FakeConnection()
        # Weird state: lease OK, but INSERT ... RETURNING yields nothing.
        fake_conn.cursor_instance.fetch_queue = [None, None]

        d = _make_daemon(fake_conn=fake_conn)
        with pytest.raises(RuntimeError):
            d.check_lease_and_register_run()


class TestSchedulerTick:
    """§6 step 1 — consume any pending commands.

    Phase 2 additions (queue scan, heartbeat metric) live in
    ``test_daemon_phase2.py``. These skeleton tests only exercise the
    step-1 command-consumption + concurrency-cap-read path, and stub
    ``_fetch_agent_ready_issues`` to isolate that path from the queue
    scan added in #2768.
    """

    def test_consumes_commands_and_reads_config(self) -> None:
        fake_conn = _FakeConnection()
        # UPDATE returns rowcount=3; SELECT config returns concurrency_cap=0
        # (the Phase 2 safe value — keeps the Phase 3A orchestration gate
        # closed so this skeleton test isolates the command-drain + config-
        # read path).
        fake_conn.cursor_instance.fetch_queue = [(0,)]

        d = _make_daemon(fake_conn=fake_conn)
        # Stub the queue/blocked scan fetches so the scheduler tick does
        # not try to shell out to ``gh`` in this skeleton test.
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._fetch_blocked_issues = lambda: []  # type: ignore[method-assign]
        # Set rowcount via a wrapper: execute() sets it before fetchone.
        # _FakeCursor doesn't auto-populate rowcount, so set it directly
        # between execute calls — the daemon calls execute then reads
        # self._conn.cursor().rowcount, which is a single attribute, so
        # we set rowcount on the cursor after the UPDATE by patching.

        original_execute = fake_conn.cursor_instance.execute

        def execute_with_rowcount(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if sql.startswith("UPDATE dispatcher.commands"):
                fake_conn.cursor_instance.rowcount = 3

        fake_conn.cursor_instance.execute = execute_with_rowcount  # type: ignore[method-assign]

        summary = d.scheduler_tick()
        assert summary["commands_consumed"] == 3
        assert summary["concurrency_cap"] == 0
        # Three commits on the first tick:
        #   1. command-consumption + config read,
        #   2. queue_snapshots INSERT (Phase 2 addition, #2768),
        #   3. blocked_snapshots INSERT (#2820) — always runs on the
        #      first tick so the admin page has a populated blocked
        #      panel immediately after daemon boot.
        # Each scan is isolated in its own transaction so a snapshot
        # failure does not roll back a successful command drain. Phase
        # 3A gate stays closed at ``concurrency_cap=0`` so no
        # additional reads/commits happen.
        assert fake_conn.commits == 3
        # Tick counter incremented.
        assert d._scheduler_ticks == 1

    def test_no_commands_to_consume(self) -> None:
        fake_conn = _FakeConnection()
        # No concurrency_cap row → fetchone returns None.
        fake_conn.cursor_instance.fetch_queue = [None]

        d = _make_daemon(fake_conn=fake_conn)
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._fetch_blocked_issues = lambda: []  # type: ignore[method-assign]
        summary = d.scheduler_tick()
        assert summary["commands_consumed"] == 0
        assert summary["concurrency_cap"] == -1  # sentinel for "unset"


class TestSupervisorTick:
    """§7 — UPDATE heartbeat_ts every tick; read failures count for smoke test."""

    def test_heartbeat_update_and_failures_read(self) -> None:
        fake_conn = _FakeConnection()
        # count(*) from failures → 7 rows in the last hour.
        fake_conn.cursor_instance.fetch_queue = [(7,)]

        d = _make_daemon(fake_conn=fake_conn)
        d._run_id = "test-run-id"  # simulate post-registration state

        summary = d.supervisor_tick()

        assert summary["failures_last_hour"] == 7
        assert d._supervisor_ticks == 1
        # The UPDATE must fire with the run_id as a bound parameter.
        executed = fake_conn.cursor_instance.executed
        update_calls = [
            e for e in executed if e[0].startswith("UPDATE dispatcher.runs")
        ]
        assert len(update_calls) == 1
        assert update_calls[0][1] == ("test-run-id",)
        # And the failures count must fire.
        assert any("dispatcher.failures" in sql for sql, _ in executed)
        # At least one commit — the heartbeat + failures-count block.
        # Later phases (3C, 3D) add optional DB reads inside the tick
        # (stuck-agent scan, diagnoser gates, retry-marker scan) that
        # each commit independently when the skeleton fake stubs have
        # empty fetch queues. The exact count is not a contract; the
        # "heartbeat round-trips at least once" signal is.
        assert fake_conn.commits >= 1
        assert d._last_heartbeat_at is not None


class TestShutdown:
    """SIGTERM-equivalent: ``request_stop`` sets the flag and mark_stopped UPDATEs the row."""

    def test_request_stop_sets_event(self) -> None:
        fake_conn = _FakeConnection()
        d = _make_daemon(fake_conn=fake_conn)
        assert not d._stop.is_set()
        d.request_stop(15, None)  # 15 = SIGTERM
        assert d._stop.is_set()

    def test_mark_stopped_updates_run_row(self) -> None:
        fake_conn = _FakeConnection()
        d = _make_daemon(fake_conn=fake_conn)
        d._run_id = "test-run-id"
        d.mark_stopped()

        executed = fake_conn.cursor_instance.executed
        assert len(executed) == 1
        sql, params = executed[0]
        assert "UPDATE dispatcher.runs SET stopped_at" in sql
        assert params == ("test-run-id",)
        assert fake_conn.commits == 1

    def test_mark_stopped_noop_if_not_registered(self) -> None:
        """No run_id → don't issue a UPDATE with a NULL key."""
        fake_conn = _FakeConnection()
        d = _make_daemon(fake_conn=fake_conn)
        # run_id is None — mark_stopped must be a silent no-op.
        d.mark_stopped()
        assert fake_conn.cursor_instance.executed == []
        assert fake_conn.commits == 0

    def test_close_idempotent(self) -> None:
        fake_conn = _FakeConnection()
        d = _make_daemon(fake_conn=fake_conn)
        d.close()
        d.close()
        assert fake_conn.closed


class TestRunLoop:
    """Smoke-test ``run_forever`` wires initial ticks + exits on stop event."""

    def test_stop_before_first_tick_still_runs_initial_ticks(self) -> None:
        """run_forever does the initial boot-tick pair before checking _stop."""
        fake_conn = _FakeConnection()
        # Boot scheduler + supervisor ticks each need one fetchone result:
        # scheduler: config → (5,); supervisor: failures count → (0,).
        fake_conn.cursor_instance.fetch_queue = [(5,), (0,)]

        d = _make_daemon(fake_conn=fake_conn)
        d._run_id = "test-run-id"
        # Pre-set the stop event so run_forever exits immediately after the
        # initial tick pair.
        d._stop.set()

        exit_code = d.run_forever()
        assert exit_code == 0
        assert d._scheduler_ticks == 1
        assert d._supervisor_ticks == 1

    def test_run_forever_honors_stop_event_within_one_second(self) -> None:
        """Trigger ``request_stop`` from another thread; loop exits quickly."""
        fake_conn = _FakeConnection()
        # Initial boot ticks only — don't need fetches for further ticks
        # because we stop the loop before the next cadence fires.
        fake_conn.cursor_instance.fetch_queue = [(5,), (0,)]

        d = _make_daemon(
            fake_conn=fake_conn,
            tick_scheduler=3600,  # arbitrarily far out
            tick_supervisor=3600,
        )
        d._run_id = "test-run-id"

        # Schedule a stop after 0.1s; run_forever's 1s poll slice guarantees
        # we exit within ~1.1s even on slow runners.
        t = threading.Timer(0.1, d.request_stop)
        t.start()
        try:
            exit_code = d.run_forever()
        finally:
            t.cancel()

        assert exit_code == 0


class TestArgparseAndConfig:
    """CLI: flag defaults + config-override JSON + env-var plumbing."""

    def test_default_flags(self) -> None:
        args = daemon._parse_args([])
        assert args.tick_scheduler_seconds == daemon.DEFAULT_SCHEDULER_TICK_SECONDS
        assert args.tick_supervisor_seconds == daemon.DEFAULT_SUPERVISOR_TICK_SECONDS
        assert args.log_level == "INFO"
        assert args.config_override is None

    def test_build_config_reads_env(self) -> None:
        args = daemon._parse_args(
            ["--tick-scheduler-seconds", "5", "--tick-supervisor-seconds", "10"]
        )
        env = {
            "DATABASE_URL": "postgres://x",
            "GIT_SHA": "feedc0de",
            "HOSTNAME": "task-abc",
        }
        cfg = daemon._build_config(args, env=env)
        assert cfg.database_url == "postgres://x"
        assert cfg.version_sha == "feedc0de"
        assert cfg.host == "task-abc"
        assert cfg.tick_scheduler_seconds == 5
        assert cfg.tick_supervisor_seconds == 10
        assert cfg.pid > 0

    def test_build_config_falls_back_to_unknown_sha(self) -> None:
        args = daemon._parse_args([])
        cfg = daemon._build_config(args, env={"DATABASE_URL": "postgres://x"})
        assert cfg.version_sha == "unknown"
        assert cfg.host  # non-empty fallback via socket.gethostname

    def test_build_config_override_parses_json(self) -> None:
        args = daemon._parse_args(["--config-override", '{"concurrency_cap": 0}'])
        cfg = daemon._build_config(args, env={"DATABASE_URL": "postgres://x"})
        assert cfg.config_override == {"concurrency_cap": 0}

    def test_build_config_override_bad_json_captured(self) -> None:
        args = daemon._parse_args(["--config-override", "{not json}"])
        cfg = daemon._build_config(args, env={"DATABASE_URL": "postgres://x"})
        assert "__parse_error__" in cfg.config_override


class TestMainEntrypoint:
    """End-to-end ``main()`` wiring — mocks the psycopg module."""

    def test_main_missing_database_url_returns_1(self) -> None:
        with patch.dict("os.environ", {"DATABASE_URL": ""}, clear=False):
            rc = daemon.main(
                ["--tick-scheduler-seconds", "1", "--tick-supervisor-seconds", "1"]
            )
        assert rc == 1

    def test_main_lease_held_returns_1(self) -> None:
        """Simulate a live peer row → main() should exit 1 without running ticks."""
        fake_conn = _FakeConnection()
        # SELECT returns a peer row → LeaseError.
        fake_conn.cursor_instance.fetch_queue = [
            ("peer-run-id", "2026-04-18T00:00:00Z")
        ]

        # Patch the psycopg module the daemon imports lazily.
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = fake_conn

        with (
            patch.dict("sys.modules", {"psycopg": mock_psycopg}),
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgres://fake", "GIT_SHA": "abc1234"},
                clear=False,
            ),
        ):
            rc = daemon.main(
                ["--tick-scheduler-seconds", "1", "--tick-supervisor-seconds", "1"]
            )
        assert rc == 1
        # Connection still closed despite the lease-held bail-out.
        assert fake_conn.closed
