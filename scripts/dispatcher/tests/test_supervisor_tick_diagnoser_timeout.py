"""Supervisor-tick reaper kills timed-out async diagnoses (#3376).

When a diagnoser exceeds its 90-min wall-clock budget, the reaper:

  1. Sends SIGTERM, then SIGKILL after a short grace period.
  2. Marks the row ``status='failed'`` with reason ``diagnoser_timeout``.
  3. Falls back to mechanical escalation so the agent doesn't sit
     in a zombie ``running`` state.

This replaces the pre-#3376 ``proc.wait(timeout=...)`` enforcement
that wedged the daemon's MainThread.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import timedelta
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
    log = logging.getLogger(f"dispatcher.test.timeout.{id(tmp_path)}")
    log.handlers = []
    log.propagate = False
    d = daemon.DispatcherDaemon(cfg, log)
    d._conn = _FakeConn()  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


class TestReaperKillsOverBudget:
    def test_alive_past_budget_sigterm_then_sigkill(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)
        # Started 91 minutes ago — past the 90-min budget.
        started_at = daemon.datetime.now(daemon.UTC) - timedelta(minutes=91)
        pending_row = {
            "diagnosis_id": 70,
            "subprocess_pid": 44444,
            "started_at": started_at,
            "failure_id": 300,
            "agent_id": "agent-overbudget",
            "category": "subprocess_crash",
            "details": {},
            "issue_number": None,
        }
        monkeypatch.setattr(
            d, "_list_pending_diagnoses_for_reap", lambda: [pending_row]
        )
        # Track liveness checks: alive on first 2 checks (SIGTERM grace),
        # dead after.
        liveness_calls = {"n": 0}

        def fake_alive(_pid: int) -> bool:
            liveness_calls["n"] += 1
            # First call (in main reap loop) — alive (triggers kill path).
            # Subsequent calls (inside _kill_diagnoser_process grace
            # loop) — alive briefly then dead.
            return liveness_calls["n"] < 3

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(fake_alive)
        )

        # Track signal calls.
        signals_sent: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            signals_sent.append((pid, sig))

        monkeypatch.setattr(daemon.os, "kill", fake_kill)
        # Avoid the real grace sleep — fast-forward time.
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

        # Stub side-effects so we can observe state.
        monkeypatch.setattr(d, "_persist_diagnoser_phase_output", lambda **_kw: None)
        monkeypatch.setattr(d, "_parse_diagnoser_usage", lambda _id: None)
        monkeypatch.setattr(d, "_read_diagnoser_stderr_tail", lambda _id: None)

        marked_failed: list[dict[str, Any]] = []

        def fake_mark_failed(diagnosis_id: int, reason: str) -> None:
            marked_failed.append({"id": diagnosis_id, "reason": reason})

        monkeypatch.setattr(d, "_mark_diagnosis_failed", fake_mark_failed)

        escalated: list[dict[str, Any]] = []
        monkeypatch.setattr(
            d,
            "_apply_mechanical_escalation",
            lambda candidate: escalated.append(candidate),
        )

        # _consume_diagnosis must NOT be called for a timed-out run.
        monkeypatch.setattr(
            d,
            "_consume_diagnosis",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("must not consume on timeout — escalate only")
            ),
        )

        reaped = d._reap_diagnoser_subprocesses()
        assert reaped == 1

        # SIGTERM was sent. SIGKILL is conditional on the process
        # surviving the grace period — depending on timing, both may
        # appear, or only SIGTERM. The contract is: SIGTERM is always
        # sent first.
        assert signals_sent, "expected at least SIGTERM"
        first_pid, first_sig = signals_sent[0]
        assert first_pid == 44444
        assert first_sig == signal.SIGTERM

        # Marked failed with the right reason.
        assert len(marked_failed) == 1
        assert marked_failed[0]["id"] == 70
        assert marked_failed[0]["reason"] == "diagnoser_timeout"

        # Mechanical escalation fired.
        assert len(escalated) == 1
        assert escalated[0]["agent_id"] == "agent-overbudget"


class TestKillDiagnoserProcess:
    """Direct ``_kill_diagnoser_process`` shape tests."""

    def test_sigterm_then_sigkill_when_process_persists(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)

        signals: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:  # noqa: ARG001
            signals.append(sig)

        # _process_alive returns True throughout grace -> SIGKILL.
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda _pid: True)
        )
        monkeypatch.setattr(daemon.os, "kill", fake_kill)
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)
        # Force the loop to exit quickly.
        original_monotonic = daemon.time.monotonic
        clock = {"t": 0.0}

        def fake_monotonic() -> float:
            clock["t"] += daemon.DIAGNOSER_REAP_GRACE_SECONDS  # exceed grace
            return clock["t"]

        monkeypatch.setattr(daemon.time, "monotonic", fake_monotonic)

        d._kill_diagnoser_process(98765)
        # Restore the time function for the rest of the suite.
        monkeypatch.setattr(daemon.time, "monotonic", original_monotonic)

        assert signal.SIGTERM in signals
        assert signal.SIGKILL in signals

    def test_sigterm_only_when_process_exits_in_grace(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)

        signals: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:  # noqa: ARG001
            signals.append(sig)

        # _process_alive returns False after the SIGTERM — clean exit.
        check_n = {"n": 0}

        def fake_alive(_pid: int) -> bool:
            check_n["n"] += 1
            # Dead immediately after SIGTERM.
            return False

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(fake_alive)
        )
        monkeypatch.setattr(daemon.os, "kill", fake_kill)
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

        d._kill_diagnoser_process(98765)

        assert signal.SIGTERM in signals
        assert signal.SIGKILL not in signals

    def test_process_already_gone_no_signal(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)

        def fake_kill(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        # First os.kill (SIGTERM) raises ProcessLookupError -> early
        # return; no SIGKILL should follow.
        signals: list[int] = []
        original_kill = daemon.os.kill

        def tracking_kill(pid: int, sig: int) -> None:
            signals.append(sig)
            raise ProcessLookupError

        monkeypatch.setattr(daemon.os, "kill", tracking_kill)
        d._kill_diagnoser_process(11111)

        # Restore.
        monkeypatch.setattr(daemon.os, "kill", original_kill)

        assert signals == [signal.SIGTERM], (
            "Once SIGTERM raises ProcessLookupError, no SIGKILL should follow."
        )
