"""Daemon-boot reap of orphaned async diagnoses (#3376).

When the previous daemon crashed (watchdog kill, panic, etc.) leaving
``dispatcher.diagnoses`` rows at ``status='pending'``, the new daemon's
boot path must:

  1. Read its own ``dispatcher.runs.started_at``.
  2. Find pending rows whose ``started_at`` predates that timestamp.
  3. Mark them ``status='failed'`` with reason
     ``diagnoser_orphaned_by_daemon_restart``.
  4. NOT signal any subprocess — PIDs from a prior boot may have been
     recycled by the OS, signalling them is dangerous.

Today's three observed kills 20:14–20:34Z (diagnoses #22, #23, #24)
are exactly this case — the supervisor's reaper backstop should
catch them on first boot post-deploy.
"""

from __future__ import annotations

import logging
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

        # Critical: must NOT call os.kill — those PIDs may be recycled.
        kills: list[Any] = []
        monkeypatch.setattr(daemon.os, "kill", lambda *a, **_kw: kills.append(a))

        reaped = d._reap_orphaned_diagnoses_on_boot()
        assert reaped == 3
        assert {m["id"] for m in marked} == {22, 23, 24}
        for m in marked:
            assert m["reason"] == "diagnoser_orphaned_by_daemon_restart"
        # No signals sent — orphan reap never tries to kill.
        assert kills == []

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

        d._reap_orphaned_diagnoses_on_boot()

        events = [
            r
            for r in records
            if getattr(r, "event", None) == "diagnoser_orphaned_by_daemon_restart"
        ]
        assert events, "expected daemon.diagnoser_orphaned_by_daemon_restart log"

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
