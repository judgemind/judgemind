"""Daemon-boot reap of orphaned async diagnoses (#3376, #3384).

When the previous daemon crashed (watchdog kill, panic, etc.) leaving
``dispatcher.diagnoses`` rows at ``status='pending'``, the new daemon's
boot path must:

  1. Read its own ``dispatcher.runs.started_at``.
  2. Find pending rows whose ``started_at`` predates that timestamp.
  3. For each orphan row, branch on PID liveness and cmdline identity:

     * No PID (crashed before PID UPDATE) → log ``diagnoser_orphan_pid_dead``,
       mark failed.
     * PID dead → log ``diagnoser_orphan_pid_dead``, mark failed.
     * PID alive AND cmdline matches ``claude -p /diagnose-failure`` →
       log ``diagnoser_orphan_killed``, KILL the process (SIGTERM →
       grace → SIGKILL), mark failed.
     * PID alive but cmdline mismatched (recycled PID) → log
       ``diagnoser_orphan_pid_recycled``, do NOT signal, mark failed.

  4. Return count of rows marked failed.

The cmdline-verification step (#3384) is the safe path: it confirms the
alive PID actually belongs to a prior-daemon diagnoser before signalling,
preventing accidental kill of an unrelated OS process that recycled the PID.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Make ``scripts`` importable.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402


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


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _make_daemon(tmp_path: Path) -> daemon.DispatcherDaemon:
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
        baseline_repo_root=tmp_path,
    )
    log = logging.getLogger(f"dispatcher.test.boot_orphan.{id(tmp_path)}")
    log.handlers = []
    log.propagate = False
    d = daemon.DispatcherDaemon(cfg, log)
    d._conn = _FakeConn()  # type: ignore[assignment]
    d._run_id = "current-run-id"
    return d


class TestOrphanReap:
    def test_marks_orphan_rows_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        """All orphan rows are marked failed; alive+matching PID gets SIGTERM."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        run_started = datetime.now(daemon.UTC)
        old1 = run_started - timedelta(minutes=30)
        old2 = run_started - timedelta(minutes=20)
        # First fetchone -> dispatcher.runs.started_at for the current run.
        # First fetchall -> orphan rows (predating run_started).
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        conn.cursor_instance.fetchall_queue = [  # type: ignore[union-attr]
            [
                (22, 11111, old1),
                (23, None, old1),  # orphan without PID — parent crashed mid-spawn
                (24, 33333, old2),
            ]
        ]
        marked: list[dict[str, Any]] = []

        def fake_mark_failed(diagnosis_id: int, reason: str) -> None:
            marked.append({"id": diagnosis_id, "reason": reason})

        monkeypatch.setattr(d, "_mark_diagnosis_failed", fake_mark_failed)

        # Make PID 11111 alive+matching, PID 33333 dead.
        def fake_process_alive(pid: int) -> bool:
            return pid == 11111

        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_process_alive",
            staticmethod(fake_process_alive),
        )

        def fake_cmdline_matches(pid: int) -> bool | None:
            if pid == 11111:
                return True
            return False

        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_diagnoser_cmdline_matches",
            staticmethod(fake_cmdline_matches),
        )

        kills: list[Any] = []
        monkeypatch.setattr(daemon.os, "kill", lambda *a, **_kw: kills.append(a))
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

        reaped = d._reap_orphaned_diagnoses_on_boot()
        assert reaped == 3
        assert {m["id"] for m in marked} == {22, 23, 24}
        for m in marked:
            assert m["reason"] == "diagnoser_orphaned_by_daemon_restart"
        # PID 11111 is alive+matching — must have been signalled (SIGTERM at minimum).
        sigtermed_pids = [a[0] for a in kills if len(a) > 1 and a[1] == signal.SIGTERM]
        assert 11111 in sigtermed_pids, (
            f"expected SIGTERM to alive+matching PID 11111; kills={kills}"
        )
        # PID 33333 is dead — must NOT have been signalled.
        killed_pids = {a[0] for a in kills}
        assert 33333 not in killed_pids, (
            f"dead PID 33333 should not be signalled; kills={kills}"
        )

    def test_no_orphans_returns_zero(self, monkeypatch: Any, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [(datetime.now(daemon.UTC),)]  # type: ignore[union-attr]
        # No orphan rows — fetchall_queue empty.
        reaped = d._reap_orphaned_diagnoses_on_boot()
        assert reaped == 0

    def test_emits_per_orphan_log_event(self, monkeypatch: Any, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # Capture log records.
        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        h = _Handler(level=logging.DEBUG)
        d._log.addHandler(h)
        d._log.setLevel(logging.DEBUG)

        run_started = datetime.now(daemon.UTC)
        old = run_started - timedelta(minutes=15)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        conn.cursor_instance.fetchall_queue = [  # type: ignore[union-attr]
            [(99, 4444, old)]
        ]
        monkeypatch.setattr(d, "_mark_diagnosis_failed", lambda *_a, **_kw: None)
        # PID 4444 is dead.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_process_alive",
            staticmethod(lambda _pid: False),
        )

        d._reap_orphaned_diagnoses_on_boot()

        events = [
            r
            for r in records
            if getattr(r, "event", None) == "diagnoser_orphan_pid_dead"
        ]
        assert events, "expected daemon.diagnoser_orphan_pid_dead log"

    def test_query_filters_by_started_at(self, tmp_path: Path) -> None:
        """The orphan-scan SELECT joins by ``started_at < this_run.started_at``
        so a freshly-spawned diagnosis (started by THIS daemon) is NOT
        misclassified as an orphan."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        run_started = datetime.now(daemon.UTC)
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        d._reap_orphaned_diagnoses_on_boot()
        executed = conn.cursor_instance.executed  # type: ignore[union-attr]
        scan_sql = [
            sql
            for sql, _ in executed
            if "SELECT diagnosis_id" in sql and "dispatcher.diagnoses" in sql
        ]
        assert scan_sql, "expected a SELECT against dispatcher.diagnoses"
        assert any(
            "status = 'pending'" in sql and "started_at <" in sql for sql in scan_sql
        )

    # ── AC1: alive + matching cmdline → SIGTERM sent ─────────────────────

    def test_orphan_alive_matching_cmdline_killed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Alive PID + matching cmdline → SIGTERM sent, row marked failed,
        ``diagnoser_orphan_killed`` event emitted."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]

        run_started = datetime.now(daemon.UTC)
        old = run_started - timedelta(minutes=10)
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        conn.cursor_instance.fetchall_queue = [[(55, 77777, old)]]  # type: ignore[union-attr]

        marked: list[dict[str, Any]] = []
        monkeypatch.setattr(
            d,
            "_mark_diagnosis_failed",
            lambda diagnosis_id, reason: marked.append(
                {"id": diagnosis_id, "reason": reason}
            ),
        )

        # PID 77777 is alive and cmdline matches.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_process_alive",
            staticmethod(lambda _pid: True),
        )
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_diagnoser_cmdline_matches",
            staticmethod(lambda _pid: True),
        )

        signals_sent: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            signals_sent.append((pid, sig))

        monkeypatch.setattr(daemon.os, "kill", fake_kill)
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

        # Capture log records to verify event name.
        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        d._log.addHandler(_Handler(level=logging.DEBUG))
        d._log.setLevel(logging.DEBUG)

        reaped = d._reap_orphaned_diagnoses_on_boot()

        assert reaped == 1
        assert marked == [{"id": 55, "reason": "diagnoser_orphaned_by_daemon_restart"}]
        # SIGTERM must have been sent to PID 77777.
        assert (77777, signal.SIGTERM) in signals_sent, (
            f"expected SIGTERM to 77777; signals={signals_sent}"
        )
        # Event log must say diagnoser_orphan_killed.
        kill_events = [
            r for r in records if getattr(r, "event", None) == "diagnoser_orphan_killed"
        ]
        assert kill_events, "expected diagnoser_orphan_killed log event"

    # ── AC2: alive + recycled PID → no signal ────────────────────────────

    def test_orphan_alive_recycled_pid_not_killed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Alive PID + mismatched cmdline → no signal sent, row marked failed,
        ``diagnoser_orphan_pid_recycled`` event emitted."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]

        run_started = datetime.now(daemon.UTC)
        old = run_started - timedelta(minutes=5)
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        conn.cursor_instance.fetchall_queue = [[(66, 88888, old)]]  # type: ignore[union-attr]

        marked: list[dict[str, Any]] = []
        monkeypatch.setattr(
            d,
            "_mark_diagnosis_failed",
            lambda diagnosis_id, reason: marked.append(
                {"id": diagnosis_id, "reason": reason}
            ),
        )

        # PID 88888 is alive but cmdline is `cat` — mismatched (recycled).
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_process_alive",
            staticmethod(lambda _pid: True),
        )
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_diagnoser_cmdline_matches",
            staticmethod(lambda _pid: False),
        )

        signals_sent: list[tuple[int, int]] = []
        monkeypatch.setattr(
            daemon.os,
            "kill",
            lambda pid, sig: signals_sent.append((pid, sig)),
        )
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        d._log.addHandler(_Handler(level=logging.DEBUG))
        d._log.setLevel(logging.DEBUG)

        reaped = d._reap_orphaned_diagnoses_on_boot()

        assert reaped == 1
        assert marked == [{"id": 66, "reason": "diagnoser_orphaned_by_daemon_restart"}]
        # No signal must have been sent to PID 88888.
        signalled_pids = {pid for pid, _sig in signals_sent}
        assert 88888 not in signalled_pids, (
            f"recycled PID 88888 must NOT be signalled; signals={signals_sent}"
        )
        # Event must say recycled.
        recycled_events = [
            r
            for r in records
            if getattr(r, "event", None) == "diagnoser_orphan_pid_recycled"
        ]
        assert recycled_events, "expected diagnoser_orphan_pid_recycled log event"

    # ── AC3: dead PID → logged distinctly ────────────────────────────────

    def test_orphan_dead_pid_logged_distinctly(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Dead PID → no signal, row marked failed, ``diagnoser_orphan_pid_dead``
        event emitted."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]

        run_started = datetime.now(daemon.UTC)
        old = run_started - timedelta(minutes=8)
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        conn.cursor_instance.fetchall_queue = [[(77, 99999, old)]]  # type: ignore[union-attr]

        marked: list[dict[str, Any]] = []
        monkeypatch.setattr(
            d,
            "_mark_diagnosis_failed",
            lambda diagnosis_id, reason: marked.append(
                {"id": diagnosis_id, "reason": reason}
            ),
        )

        # PID 99999 is dead.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_process_alive",
            staticmethod(lambda _pid: False),
        )

        signals_sent: list[tuple[int, int]] = []
        monkeypatch.setattr(
            daemon.os,
            "kill",
            lambda pid, sig: signals_sent.append((pid, sig)),
        )

        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        d._log.addHandler(_Handler(level=logging.DEBUG))
        d._log.setLevel(logging.DEBUG)

        reaped = d._reap_orphaned_diagnoses_on_boot()

        assert reaped == 1
        assert marked == [{"id": 77, "reason": "diagnoser_orphaned_by_daemon_restart"}]
        # No signal sent (PID is dead).
        assert signals_sent == [], (
            f"dead PID 99999 must not be signalled; signals={signals_sent}"
        )
        # Event must say pid_dead.
        dead_events = [
            r
            for r in records
            if getattr(r, "event", None) == "diagnoser_orphan_pid_dead"
        ]
        assert dead_events, "expected diagnoser_orphan_pid_dead log event"
        # The log record must carry subprocess_pid=99999.
        assert any(getattr(r, "subprocess_pid", None) == 99999 for r in dead_events), (
            "diagnoser_orphan_pid_dead should include subprocess_pid=99999"
        )

    # ── Null-PID path: pre-PID-UPDATE crash ──────────────────────────────

    def test_orphan_null_pid_path(self, monkeypatch: Any, tmp_path: Path) -> None:
        """Null subprocess_pid → no kill attempted, row marked failed,
        ``diagnoser_orphan_pid_dead`` event emitted (no PID to check)."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]

        run_started = datetime.now(daemon.UTC)
        old = run_started - timedelta(minutes=12)
        conn.cursor_instance.fetch_queue = [(run_started,)]  # type: ignore[union-attr]
        # subprocess_pid is None.
        conn.cursor_instance.fetchall_queue = [[(88, None, old)]]  # type: ignore[union-attr]

        marked: list[dict[str, Any]] = []
        monkeypatch.setattr(
            d,
            "_mark_diagnosis_failed",
            lambda diagnosis_id, reason: marked.append(
                {"id": diagnosis_id, "reason": reason}
            ),
        )

        signals_sent: list[tuple[int, int]] = []
        monkeypatch.setattr(
            daemon.os,
            "kill",
            lambda pid, sig: signals_sent.append((pid, sig)),
        )

        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        d._log.addHandler(_Handler(level=logging.DEBUG))
        d._log.setLevel(logging.DEBUG)

        reaped = d._reap_orphaned_diagnoses_on_boot()

        assert reaped == 1
        assert marked == [{"id": 88, "reason": "diagnoser_orphaned_by_daemon_restart"}]
        assert signals_sent == [], f"no PID → no signal; got signals={signals_sent}"
        dead_events = [
            r
            for r in records
            if getattr(r, "event", None) == "diagnoser_orphan_pid_dead"
        ]
        assert dead_events, "expected diagnoser_orphan_pid_dead log for null-PID orphan"
        assert any(
            getattr(r, "subprocess_pid", "MISSING") is None for r in dead_events
        ), "subprocess_pid should be None in the log record"
