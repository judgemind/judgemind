"""``dispatcher.config.max_concurrent_diagnoses`` cap (#3376).

When the number of pending diagnoses with a subprocess_pid is at or
above the cap, ``_run_diagnoser_pass`` defers new spawns until the
reaper drains completed runs. Prevents an unbounded backlog of
concurrent Opus-driven diagnoser sessions.
"""

from __future__ import annotations

import logging
import sys
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
    log = logging.getLogger(f"dispatcher.test.cap.{id(tmp_path)}")
    log.handlers = []
    log.propagate = False
    d = daemon.DispatcherDaemon(cfg, log)
    d._conn = _FakeConn()  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


def _candidate(failure_id: int, agent_id: str) -> dict[str, Any]:
    return {
        "failure_id": failure_id,
        "agent_id": agent_id,
        "category": "subprocess_crash",
        "tier": 2,
        "issue_number": 100 + failure_id,
        "details": {},
    }


class TestMaxConcurrentDiagnosesReader:
    def test_default_when_config_missing(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # fetchone -> None means no row.
        assert d._max_concurrent_diagnoses() == daemon.DEFAULT_MAX_CONCURRENT_DIAGNOSES

    def test_reads_native_int_from_jsonb(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [(5,)]  # type: ignore[union-attr]
        assert d._max_concurrent_diagnoses() == 5

    def test_reads_legacy_string_jsonb(self, tmp_path: Path) -> None:
        """psycopg fake-cursor tests sometimes return JSON-encoded
        strings. The reader handles ``json.loads`` for back-compat."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [("3",)]  # type: ignore[union-attr]
        assert d._max_concurrent_diagnoses() == 3

    def test_non_positive_falls_back_to_default(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [(0,)]  # type: ignore[union-attr]
        assert d._max_concurrent_diagnoses() == daemon.DEFAULT_MAX_CONCURRENT_DIAGNOSES

    def test_malformed_falls_back_to_default(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [("not-a-number",)]  # type: ignore[union-attr]
        assert d._max_concurrent_diagnoses() == daemon.DEFAULT_MAX_CONCURRENT_DIAGNOSES

    def test_default_constant_is_two(self) -> None:
        # Sanity check — the default cap matches migration 49's seed.
        assert daemon.DEFAULT_MAX_CONCURRENT_DIAGNOSES == 2


class TestSpawnPassDefersAtCap:
    def test_defers_when_in_flight_at_cap(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        # Cap = 1, in-flight = 1 -> at cap, defer everything.
        monkeypatch.setattr(d, "_max_concurrent_diagnoses", lambda: 1)
        monkeypatch.setattr(d, "_count_pending_diagnoses_with_pid", lambda: 1)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [_candidate(1, "a1"), _candidate(2, "a2")],
        )

        # Hard guards: nothing should be spawned/inserted.
        monkeypatch.setattr(
            d,
            "_insert_pending_diagnosis",
            lambda **_kw: (_ for _ in ()).throw(
                AssertionError("must not insert when at cap")
            ),
        )
        monkeypatch.setattr(
            d,
            "_spawn_diagnoser_subprocess",
            lambda _id: (_ for _ in ()).throw(
                AssertionError("must not spawn when at cap")
            ),
        )

        ran = d._run_diagnoser_pass()
        assert ran == 0

    def test_spawns_until_cap_then_defers(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Cap = 2, in-flight = 0, three candidates -> spawn 2, defer 1."""
        d = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(d, "_max_concurrent_diagnoses", lambda: 2)
        monkeypatch.setattr(d, "_count_pending_diagnoses_with_pid", lambda: 0)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [
                _candidate(1, "a1"),
                _candidate(2, "a2"),
                _candidate(3, "a3"),
            ],
        )
        monkeypatch.setattr(d, "_build_diagnoser_context", lambda _c: {})

        spawned: list[int] = []
        diagnosis_counter = {"n": 0}

        def fake_insert(**_kw: Any) -> int:
            diagnosis_counter["n"] += 1
            return diagnosis_counter["n"]

        def fake_spawn(diagnosis_id: int) -> int:
            spawned.append(diagnosis_id)
            return 1000 + diagnosis_id

        monkeypatch.setattr(d, "_insert_pending_diagnosis", fake_insert)
        monkeypatch.setattr(d, "_spawn_diagnoser_subprocess", fake_spawn)
        monkeypatch.setattr(d, "_record_diagnoser_subprocess_pid", lambda *_a: None)

        ran = d._run_diagnoser_pass()
        assert ran == 2
        assert len(spawned) == 2
        # Third candidate was deferred.

    def test_spawns_all_when_under_cap(self, monkeypatch: Any, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(d, "_max_concurrent_diagnoses", lambda: 10)
        monkeypatch.setattr(d, "_count_pending_diagnoses_with_pid", lambda: 0)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [_candidate(1, "a1"), _candidate(2, "a2")],
        )
        monkeypatch.setattr(d, "_build_diagnoser_context", lambda _c: {})
        diagnosis_counter = {"n": 0}

        def fake_insert(**_kw: Any) -> int:
            diagnosis_counter["n"] += 1
            return diagnosis_counter["n"]

        monkeypatch.setattr(d, "_insert_pending_diagnosis", fake_insert)
        monkeypatch.setattr(d, "_spawn_diagnoser_subprocess", lambda _id: 4242)
        monkeypatch.setattr(d, "_record_diagnoser_subprocess_pid", lambda *_a: None)

        ran = d._run_diagnoser_pass()
        assert ran == 2


class TestCountPendingDiagnoses:
    def test_zero_when_no_rows(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # fetchone -> None.
        assert d._count_pending_diagnoses_with_pid() == 0

    def test_returns_count_from_db(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [(7,)]  # type: ignore[union-attr]
        assert d._count_pending_diagnoses_with_pid() == 7

    def test_query_filters_by_status_and_pid(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        conn.cursor_instance.fetch_queue = [(0,)]  # type: ignore[union-attr]
        d._count_pending_diagnoses_with_pid()
        executed = conn.cursor_instance.executed  # type: ignore[union-attr]
        assert any(
            "status = 'pending'" in sql and "subprocess_pid IS NOT NULL" in sql
            for sql, _ in executed
        )
