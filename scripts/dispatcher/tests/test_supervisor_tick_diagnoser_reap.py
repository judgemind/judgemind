"""Supervisor-tick reaper consumes completed async diagnoses (#3376).

When the diagnoser subprocess has exited (process gone), the reaper
pass on a subsequent supervisor tick must:

  1. Read the recommendation from ``dispatcher.diagnoses.recommendation``.
  2. Consume the recommendation via ``_consume_diagnosis``.
  3. Read ``next_directive`` and act via ``_consume_next_directive``.
  4. Persist diagnoser metering / phase output.

This integration test simulates a diagnoser that wrote
recommendation+directive+stdout, then exited. The reaper picks the row
up on the next tick and runs the full consume sequence.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
    log = logging.getLogger(f"dispatcher.test.reap.{id(tmp_path)}")
    log.handlers = []
    log.propagate = False
    d = daemon.DispatcherDaemon(cfg, log)
    d._conn = _FakeConn()  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


class TestReaperConsumesCompletedDiagnosis:
    """When the subprocess is gone, the reaper consumes recommendation
    + directive on the next tick."""

    def test_completed_diagnosis_marked_and_consumed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)

        # Simulate: one pending diagnosis with PID, process gone.
        pending_row = {
            "diagnosis_id": 50,
            "subprocess_pid": 22222,
            "started_at": daemon.datetime.now(daemon.UTC),
            "failure_id": 100,
            "agent_id": "agent-test",
            "category": "subprocess_crash",
            "details": {"issue_number": 1234},
            "issue_number": 1234,
        }
        monkeypatch.setattr(
            d, "_list_pending_diagnoses_for_reap", lambda: [pending_row]
        )
        # PID is not alive — process completed.
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda _pid: False)
        )
        # Stub metering — assert it was called.
        persisted: list[dict[str, Any]] = []

        def fake_persist(
            agent_id: str,
            diagnosis_id: int,
            usage: Any,
            log_text: Any,
        ) -> None:
            persisted.append({"agent_id": agent_id, "diagnosis_id": diagnosis_id})

        monkeypatch.setattr(d, "_persist_diagnoser_phase_output", fake_persist)
        monkeypatch.setattr(d, "_parse_diagnoser_usage", lambda _id: None)
        monkeypatch.setattr(d, "_read_diagnoser_stderr_tail", lambda _id: None)

        consumed: dict[str, Any] = {}

        def fake_consume(diagnosis_id: int, candidate: dict[str, Any]) -> str:
            consumed["id"] = diagnosis_id
            consumed["candidate"] = candidate
            return "retry"

        monkeypatch.setattr(d, "_consume_diagnosis", fake_consume)

        directive_calls: list[dict[str, Any]] = []

        def fake_consume_directive(
            *,
            diagnosis_id: int,
            candidate: dict[str, Any],
            consumed_action: str,
        ) -> str:
            directive_calls.append(
                {
                    "diagnosis_id": diagnosis_id,
                    "candidate": candidate,
                    "consumed_action": consumed_action,
                }
            )
            return "respawn_at_plan"

        monkeypatch.setattr(d, "_consume_next_directive", fake_consume_directive)

        reaped = d._reap_diagnoser_subprocesses()
        assert reaped == 1
        assert len(persisted) == 1
        assert persisted[0]["diagnosis_id"] == 50
        assert consumed["id"] == 50
        # Reaper must pass an issue_number to the consumer so action
        # handlers (block_and_comment, escalate) can post on the right
        # GH issue.
        assert consumed["candidate"]["issue_number"] == 1234
        assert len(directive_calls) == 1
        assert directive_calls[0]["consumed_action"] == "retry"

    def test_reaper_calls_mark_completed_after_consume_diagnosis(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Issue #3422 — the daemon (not the SKILL) writes status='completed'.

        After _consume_diagnosis runs successfully, the reaper must
        result in _mark_diagnosis_completed being called with the
        diagnosis_id.  We verify by checking that the fake cursor's
        ``executed`` queue contains an UPDATE with status='completed'.
        """
        from unittest.mock import patch

        d = _make_daemon(tmp_path)

        pending_row = {
            "diagnosis_id": 51,
            "subprocess_pid": 22223,
            "started_at": daemon.datetime.now(daemon.UTC),
            "failure_id": 101,
            "agent_id": "agent-test-2",
            "category": "subprocess_crash",
            "details": {"issue_number": 5001},
            "issue_number": 5001,
        }
        monkeypatch.setattr(
            d, "_list_pending_diagnoses_for_reap", lambda: [pending_row]
        )
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda _pid: False)
        )
        monkeypatch.setattr(
            d, "_persist_diagnoser_phase_output", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(d, "_parse_diagnoser_usage", lambda _id: None)
        monkeypatch.setattr(d, "_read_diagnoser_stderr_tail", lambda _id: None)
        monkeypatch.setattr(d, "_consume_next_directive", lambda **_kw: "terminal")

        mark_completed_calls: list[int] = []

        with patch.object(
            d,
            "_mark_diagnosis_completed",
            wraps=lambda did: mark_completed_calls.append(did),
        ):
            # _consume_diagnosis is real but stubs its inner read to return a valid action.
            with patch.object(
                d,
                "_read_recommendation",
                return_value={"action": "retry", "reasoning": "t"},
            ):
                with patch.object(d, "_consume_action_retry"):
                    d._reap_diagnoser_subprocesses()

        # The reaper must have triggered _mark_diagnosis_completed(51).
        assert 51 in mark_completed_calls, (
            "_mark_diagnosis_completed was not called with diagnosis_id=51 after _consume_diagnosis"
        )


class TestMarkDiagnosisCompleted:
    """Direct unit tests for _mark_diagnosis_completed (issue #3422)."""

    def test_issues_correct_update_sql(self, tmp_path: Path) -> None:
        """_mark_diagnosis_completed must UPDATE status='completed' + completed_at."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        d._mark_diagnosis_completed(diagnosis_id=42)

        assert conn.commits == 1
        assert len(conn.cursor_instance.executed) == 1
        sql, params = conn.cursor_instance.executed[0]
        assert "SET status = 'completed'" in sql
        assert "completed_at = now()" in sql
        assert "dispatcher.diagnoses" in sql
        assert params == (42,)

    def test_db_error_does_not_propagate(self, tmp_path: Path) -> None:
        """A DB error in _mark_diagnosis_completed must not crash the caller."""
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]

        def _boom(_sql: str, _params: Any = None) -> None:
            raise RuntimeError("db down")

        conn.cursor_instance.execute = _boom  # type: ignore[assignment]
        # Must not raise.
        d._mark_diagnosis_completed(diagnosis_id=99)


class TestReaperLeavesAliveAndWithinBudget:
    """Diagnoser still running, within 90-min budget — leave alone."""

    def test_alive_within_budget_not_reaped(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)
        pending_row = {
            "diagnosis_id": 60,
            "subprocess_pid": 33333,
            # Started 10 minutes ago — well within 90-min budget.
            "started_at": daemon.datetime.now(daemon.UTC)
            - daemon.datetime.now(daemon.UTC).utcoffset()
            if daemon.datetime.now(daemon.UTC).utcoffset()
            else daemon.datetime.now(daemon.UTC),
            "failure_id": 200,
            "agent_id": "agent-mid-run",
            "category": "subprocess_crash",
            "details": {},
            "issue_number": None,
        }
        # Override with explicit recent time.
        from datetime import timedelta

        pending_row["started_at"] = daemon.datetime.now(daemon.UTC) - timedelta(
            minutes=10
        )

        monkeypatch.setattr(
            d, "_list_pending_diagnoses_for_reap", lambda: [pending_row]
        )
        # PID alive.
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda _pid: True)
        )

        # Hard guard: nothing should be consumed/killed.
        monkeypatch.setattr(
            d,
            "_consume_diagnosis",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("must not consume an alive within-budget run")
            ),
        )
        monkeypatch.setattr(
            d,
            "_kill_diagnoser_process",
            lambda _pid: (_ for _ in ()).throw(
                AssertionError("must not kill an alive within-budget run")
            ),
        )

        reaped = d._reap_diagnoser_subprocesses()
        assert reaped == 0


class TestListPendingDiagnosesForReap:
    """The reaper's DB query joins diagnoses with failures so the
    consumer dict has agent_id + category + issue_number."""

    def test_returns_joined_rows(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        conn = d._conn  # type: ignore[assignment]
        from datetime import datetime

        now = datetime.now(daemon.UTC)
        # JOIN result row: diagnosis_id, pid, started_at, failure_id,
        # agent_id, category, details (JSONB).
        conn.cursor_instance.fetchall_queue = [  # type: ignore[union-attr]
            [
                (
                    77,
                    4242,
                    now,
                    1001,
                    "agent-uuid-x",
                    "subprocess_crash",
                    json.dumps({"issue_number": 5678}),
                ),
            ]
        ]
        rows = d._list_pending_diagnoses_for_reap()
        assert len(rows) == 1
        row = rows[0]
        assert row["diagnosis_id"] == 77
        assert row["subprocess_pid"] == 4242
        assert row["agent_id"] == "agent-uuid-x"
        assert row["category"] == "subprocess_crash"
        assert row["issue_number"] == 5678
        assert row["details"] == {"issue_number": 5678}

    def test_returns_empty_on_no_rows(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # fetchall_queue empty -> _FakeCursor.fetchall returns [].
        rows = d._list_pending_diagnoses_for_reap()
        assert rows == []


class TestProcessAliveCheck:
    """``_process_alive(pid)`` uses ``os.kill(pid, 0)``."""

    def test_alive_pid_returns_true(self, monkeypatch: Any) -> None:
        with patch("dispatcher.daemon.os.kill") as mock_kill:
            mock_kill.return_value = None
            assert daemon.DispatcherDaemon._process_alive(12345) is True
            mock_kill.assert_called_once_with(12345, 0)

    def test_dead_pid_returns_false(self) -> None:
        with patch("dispatcher.daemon.os.kill", side_effect=ProcessLookupError):
            assert daemon.DispatcherDaemon._process_alive(99999) is False

    def test_permission_error_treated_as_alive(self) -> None:
        # Defensive: better to wait one more tick than misclassify.
        with patch("dispatcher.daemon.os.kill", side_effect=PermissionError):
            assert daemon.DispatcherDaemon._process_alive(1) is True
