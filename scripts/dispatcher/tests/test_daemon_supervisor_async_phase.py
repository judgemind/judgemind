"""Async phase subprocess spawn/reap (#3658).

Move verify / fix-ci / retro phase subprocesses off MainThread so a
slow 5+ minute verify cannot trip the 120s ``scheduler_tick_stalled_exiting``
watchdog.

Three test trios — one per phase (verify, fix-ci, retro):

1. ``test_<phase>_subprocess_does_not_block_supervisor_tick`` — assert
   that starting the phase does NOT block (no ``proc.wait()`` on MainThread),
   the agent appears in ``_phase_subprocess_inflight``, and a
   ``<phase>_started_async`` event is emitted.

2. ``test_followup_tick_finalizes_completed_<phase>`` — register an
   in-flight entry with a dead PID; assert the reaper finalizes it,
   reads output, advances phase, and clears the inflight entry.

3. ``test_<phase>_deadline_exceeded_does_not_kill_daemon`` — register
   an in-flight entry with a deadline in the past and a "alive" PID;
   assert the reaper kills the process group, routes the agent to the
   failure / timeout handler, and supervisor_tick returns normally.

These tests FAIL against the pre-#3658 code (which called
``_run_subprocess_or_fail`` synchronously on MainThread).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make ``scripts`` importable.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Minimal DB fakes — same pattern as test_daemon_verify_skip_ecs.py
# ---------------------------------------------------------------------------


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

    def close(self) -> None:
        pass


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
) -> tuple[daemon.DispatcherDaemon, _FakeConn, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.async_phase.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConn()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
        baseline_repo_root=tmp_path,
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


def _minimal_agent(
    tmp_path: Path,
    *,
    agent_id: str = "agent-test-1",
    phase: str = "awaiting_deploy",
    status: str = "succeeded",
    pr_number: int = 101,
    issue_number: int = 999,
    retries_used: int = 0,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "phase": phase,
        "pr_number": pr_number,
        "worktree_path": str(tmp_path),
        "retries_used": retries_used,
        "status": status,
        "execution_mode": "subprocess",
        "merge_unstick_attempts": 0,
    }


def _write_verified_output(worktree: Path, *, verdict: str = "VERIFIED") -> None:
    """Write a minimal verify phase_output.json so _finalize_verify can read it."""
    output_dir = worktree / "tmp" / "dispatcher-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "verify.json").write_text(
        json.dumps({"verdict": verdict, "evidence_md": "All good."})
    )


def _write_fix_ci_output(worktree: Path, *, verdict: str = "FLAKY") -> None:
    output_dir = worktree / "tmp" / "dispatcher-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "fix-ci.json").write_text(
        json.dumps({"verdict": verdict, "flaky_evidence": "flake"})
    )


def _write_retro_output(worktree: Path) -> None:
    output_dir = worktree / "tmp" / "dispatcher-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "retro.json").write_text(
        json.dumps({"no_findings": True, "retro_issues": []})
    )


# ---------------------------------------------------------------------------
# Verify trio
# ---------------------------------------------------------------------------


class TestVerifyAsyncPhase:
    """Verify subprocess is async: spawn/reap does not block MainThread."""

    def test_verify_subprocess_does_not_block_supervisor_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC1 + AC2: starting verify does NOT block; agent enters inflight."""
        d, _conn, handler = _make_daemon(tmp_path)

        spawn_calls: list[dict] = []

        def fake_spawn(
            phase: str,
            worktree: Path,
            agent_id: str,
            deadline_seconds: float,
            ctx: dict | None = None,
        ) -> int | None:
            spawn_calls.append(
                {
                    "phase": phase,
                    "agent_id": agent_id,
                    "deadline_seconds": deadline_seconds,
                }
            )
            # Register entry in inflight so _advance_running_agents skips next time.
            d._phase_subprocess_inflight[agent_id] = {
                "phase": phase,
                "pid": 99999,
                "worktree_path": str(worktree),
                "started_at": datetime.now(UTC),
                "deadline_at": datetime.now(UTC) + timedelta(seconds=deadline_seconds),
                "ctx": ctx or {},
            }
            return 99999

        monkeypatch.setattr(d, "_spawn_phase_subprocess_async", fake_spawn)

        # Stub the short-circuit reads so _start_verify gets past them.
        monkeypatch.setattr(d, "_read_verify_skip_reason", lambda _: None)
        monkeypatch.setattr(
            d, "_read_merged_at_and_verified_at", lambda _: (True, False)
        )

        # Minimal stubs for the input-bundle builder.
        monkeypatch.setattr(d, "_fetch_issue_bundle", lambda n: {"issue_body": ""})
        monkeypatch.setattr(d, "_fetch_phase_output", lambda *a, **kw: None)
        monkeypatch.setattr(d, "_select_deploy_status", lambda runs: None)
        monkeypatch.setattr(d, "_infer_change_type", lambda runs: "dx_tooling")
        monkeypatch.setattr(d, "_touched_services_from_runs", lambda runs: [])
        monkeypatch.setattr(d, "_write_phase_input", lambda *a, **kw: Path(tmp_path))

        agent = _minimal_agent(tmp_path, phase="awaiting_deploy")

        start = time.monotonic()
        d._start_verify(
            agent=agent,
            pr_status={},
            merge_sha="abc123",
            deploy_runs=[],
        )
        elapsed = time.monotonic() - start

        # Must return quickly — no proc.wait() on MainThread.
        assert elapsed < 2.0, (
            f"_start_verify must return within 2s (got {elapsed:.3f}s). "
            "Regression guard for issue #3658 — pre-fix code called "
            "_run_subprocess_or_fail which proc.wait()-ed synchronously."
        )

        # Agent must be registered as inflight.
        assert agent["agent_id"] in d._phase_subprocess_inflight, (
            "Agent must appear in _phase_subprocess_inflight after _start_verify"
        )
        assert d._phase_subprocess_inflight[agent["agent_id"]]["phase"] == "verify"

        # Async event must be emitted.
        started_events = handler.events("verify_started_async")
        assert len(started_events) == 1, (
            f"Expected exactly one verify_started_async event, got {len(started_events)}"
        )

    def test_followup_tick_finalizes_completed_verify(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC1: a subsequent tick's reaper finalizes a completed verify subprocess."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-verify-done"

        # Pre-populate inflight with a dead PID.
        d._phase_subprocess_inflight[agent_id] = {
            "phase": "verify",
            "pid": 11111,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC) - timedelta(seconds=10),
            "deadline_at": datetime.now(UTC) + timedelta(seconds=3000),
            "ctx": {
                "pr_number": 42,
                "issue_number": 99,
                "merge_sha": "deadbeef",
                "merged_at_set": True,
            },
        }

        # PID is gone (process completed).
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda pid: False)
        )

        # Write output so _finalize_verify reads VERIFIED.
        _write_verified_output(tmp_path, verdict="VERIFIED")

        # Stub DB writes to avoid psycopg.
        write_verified_calls: list[str] = []
        monkeypatch.setattr(
            d, "_write_verified_at", lambda aid: write_verified_calls.append(aid)
        )
        update_phase_calls: list[tuple] = []
        monkeypatch.setattr(
            d,
            "_update_agent_phase",
            lambda aid, phase: update_phase_calls.append((aid, phase)),
        )
        monkeypatch.setattr(d, "_persist_phase_output", lambda *a, **kw: None)
        monkeypatch.setattr(d, "_post_evidence_comment", lambda *a, **kw: None)

        # Run reaper.
        reaped = d._reap_phase_subprocesses()

        assert reaped == 1, f"Expected 1 reaped entry, got {reaped}"

        # Inflight entry must be cleared.
        assert agent_id not in d._phase_subprocess_inflight, (
            "Inflight entry must be cleared after finalization"
        )

        # _write_verified_at must have been called.
        assert agent_id in write_verified_calls, (
            "_write_verified_at must be called on VERIFIED verdict"
        )

        # Phase must advance to 'done'.
        assert any(
            aid == agent_id and ph == "done" for aid, ph in update_phase_calls
        ), f"Phase must advance to 'done'; got {update_phase_calls}"

    def test_verify_deadline_exceeded_does_not_kill_daemon(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC3: deadline-exceeded reap kills process group and transitions to failure."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-verify-timeout"

        # Register entry with deadline in the past.
        d._phase_subprocess_inflight[agent_id] = {
            "phase": "verify",
            "pid": 22222,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC) - timedelta(seconds=4000),
            "deadline_at": datetime.now(UTC) - timedelta(seconds=100),  # in the past
            "ctx": {
                "pr_number": 55,
                "issue_number": 88,
                "merge_sha": "abc",
                "merged_at_set": True,
            },
        }

        # PID is still alive (deadline exceeded but process not done).
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda pid: True)
        )

        kill_calls: list[int] = []
        monkeypatch.setattr(
            d, "_kill_phase_subprocess", lambda pid: kill_calls.append(pid)
        )

        restore_calls: list[str] = []
        monkeypatch.setattr(
            d,
            "_restore_succeeded_and_advance_done",
            lambda aid: restore_calls.append(aid),
        )

        # Run reaper — must not raise.
        reaped = d._reap_phase_subprocesses()

        assert reaped == 1, f"Expected 1 reaped entry, got {reaped}"

        # Process must have been killed.
        assert 22222 in kill_calls, (
            "_kill_phase_subprocess must be called for a deadline-exceeded PID"
        )

        # Inflight cleared.
        assert agent_id not in d._phase_subprocess_inflight

        # Deadline-exceeded events emitted.
        deadline_events = handler.events("phase_async_deadline_exceeded")
        assert len(deadline_events) == 1
        assert deadline_events[0].phase == "verify"


# ---------------------------------------------------------------------------
# Fix-CI trio
# ---------------------------------------------------------------------------


class TestFixCiAsyncPhase:
    """Fix-CI subprocess is async: spawn/reap does not block MainThread."""

    def test_fix_ci_subprocess_does_not_block_supervisor_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Starting fix-ci does NOT block; agent enters inflight."""
        d, _conn, handler = _make_daemon(tmp_path)

        spawn_calls: list[dict] = []

        def fake_spawn(
            phase: str,
            worktree: Path,
            agent_id: str,
            deadline_seconds: float,
            ctx: dict | None = None,
        ) -> int | None:
            spawn_calls.append({"phase": phase, "agent_id": agent_id})
            d._phase_subprocess_inflight[agent_id] = {
                "phase": phase,
                "pid": 88888,
                "worktree_path": str(worktree),
                "started_at": datetime.now(UTC),
                "deadline_at": datetime.now(UTC) + timedelta(seconds=deadline_seconds),
                "ctx": ctx or {},
            }
            return 88888

        monkeypatch.setattr(d, "_spawn_phase_subprocess_async", fake_spawn)

        # Stub helpers that _start_fix_ci calls.
        monkeypatch.setattr(d, "_extract_failing_jobs", lambda pr_status: [])
        monkeypatch.setattr(d, "_fetch_pr_diff", lambda wt, pr: "")
        monkeypatch.setattr(d, "_branch_for_agent", lambda aid: "feature/test")
        monkeypatch.setattr(d, "_write_phase_input", lambda *a, **kw: Path(tmp_path))

        agent = _minimal_agent(tmp_path, phase="awaiting_ci", status="running")

        start = time.monotonic()
        d._start_fix_ci(agent=agent, pr_status={})
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, (
            f"_start_fix_ci must return within 2s (got {elapsed:.3f}s). "
            "Regression guard for issue #3658."
        )
        assert agent["agent_id"] in d._phase_subprocess_inflight
        assert d._phase_subprocess_inflight[agent["agent_id"]]["phase"] == "fix-ci"

        started_events = handler.events("fix_ci_started_async")
        assert len(started_events) == 1

    def test_followup_tick_finalizes_completed_fix_ci(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Reaper finalizes a completed fix-ci subprocess."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-fix-ci-done"
        agent_dict = _minimal_agent(tmp_path, agent_id=agent_id, phase="awaiting_ci")

        d._phase_subprocess_inflight[agent_id] = {
            "phase": "fix-ci",
            "pid": 33333,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC) - timedelta(seconds=60),
            "deadline_at": datetime.now(UTC) + timedelta(seconds=18000),
            "ctx": {
                "agent": dict(agent_dict),
                "pr_number": 77,
                "issue_number": 11,
                "retries_used": 0,
            },
        }

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda pid: False)
        )

        # Write FLAKY output so the finalizer takes the no-op FLAKY path.
        _write_fix_ci_output(tmp_path, verdict="FLAKY")

        log_info_calls: list[str] = []
        original_info = d._log.info
        monkeypatch.setattr(
            d._log,
            "info",
            lambda msg, **kw: (
                log_info_calls.append(kw.get("extra", {}).get("event", ""))
                or original_info(msg, **kw)
            ),
        )
        monkeypatch.setattr(d, "_persist_phase_output", lambda *a, **kw: None)

        reaped = d._reap_phase_subprocesses()

        assert reaped == 1
        assert agent_id not in d._phase_subprocess_inflight

        flaky_events = handler.events("fix_ci_flaky")
        assert len(flaky_events) == 1

    def test_fix_ci_deadline_exceeded_does_not_kill_daemon(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Deadline-exceeded fix-ci reap kills PG and marks agent failed."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-fix-ci-timeout"

        d._phase_subprocess_inflight[agent_id] = {
            "phase": "fix-ci",
            "pid": 44444,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC) - timedelta(seconds=20000),
            "deadline_at": datetime.now(UTC) - timedelta(seconds=200),
            "ctx": {
                "pr_number": 77,
                "issue_number": 11,
                "retries_used": 0,
            },
        }

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda pid: True)
        )
        kill_calls: list[int] = []
        monkeypatch.setattr(
            d, "_kill_phase_subprocess", lambda pid: kill_calls.append(pid)
        )
        failure_calls: list[str] = []
        monkeypatch.setattr(
            d,
            "_handle_agent_failure",
            lambda *, agent_id, **kw: failure_calls.append(agent_id),
        )

        reaped = d._reap_phase_subprocesses()

        assert reaped == 1
        assert 44444 in kill_calls
        assert agent_id not in d._phase_subprocess_inflight
        assert any(aid == agent_id for aid in failure_calls)

        deadline_events = handler.events("phase_async_deadline_exceeded")
        assert len(deadline_events) == 1
        assert deadline_events[0].phase == "fix-ci"


# ---------------------------------------------------------------------------
# Retro trio
# ---------------------------------------------------------------------------


class TestRetroAsyncPhase:
    """Retro subprocess is async: spawn/reap does not block MainThread."""

    def test_retro_subprocess_does_not_block_supervisor_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Starting retro does NOT block; agent enters inflight."""
        d, _conn, handler = _make_daemon(tmp_path)

        spawn_calls: list[dict] = []

        def fake_spawn(
            phase: str,
            worktree: Path,
            agent_id: str,
            deadline_seconds: float,
            ctx: dict | None = None,
        ) -> int | None:
            spawn_calls.append({"phase": phase, "agent_id": agent_id})
            d._phase_subprocess_inflight[agent_id] = {
                "phase": phase,
                "pid": 77777,
                "worktree_path": str(worktree),
                "started_at": datetime.now(UTC),
                "deadline_at": datetime.now(UTC) + timedelta(seconds=deadline_seconds),
                "ctx": ctx or {},
            }
            return 77777

        monkeypatch.setattr(d, "_spawn_phase_subprocess_async", fake_spawn)
        monkeypatch.setattr(d, "_build_retro_input", lambda agent: {})
        monkeypatch.setattr(d, "_write_phase_input", lambda *a, **kw: Path(tmp_path))

        agent = _minimal_agent(tmp_path, phase="done", status="succeeded")

        start = time.monotonic()
        d._start_retro(agent=agent)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, (
            f"_start_retro must return within 2s (got {elapsed:.3f}s). "
            "Regression guard for issue #3658."
        )
        assert agent["agent_id"] in d._phase_subprocess_inflight
        assert d._phase_subprocess_inflight[agent["agent_id"]]["phase"] == "retro"

        started_events = handler.events("retro_started_async")
        assert len(started_events) == 1

    def test_followup_tick_finalizes_completed_retro(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Reaper finalizes a completed retro subprocess."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-retro-done"

        d._phase_subprocess_inflight[agent_id] = {
            "phase": "retro",
            "pid": 55555,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC) - timedelta(seconds=120),
            "deadline_at": datetime.now(UTC) + timedelta(seconds=3000),
            "ctx": {"issue_number": 200, "pr_number": 201},
        }

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda pid: False)
        )
        _write_retro_output(tmp_path)

        write_retroed_calls: list[str] = []
        monkeypatch.setattr(
            d, "_write_retroed_at", lambda aid: write_retroed_calls.append(aid)
        )
        update_phase_calls: list[tuple] = []
        monkeypatch.setattr(
            d,
            "_update_agent_phase",
            lambda aid, phase: update_phase_calls.append((aid, phase)),
        )
        monkeypatch.setattr(d, "_persist_phase_output", lambda *a, **kw: None)
        monkeypatch.setattr(d, "_file_retro_issue", lambda *a, **kw: None)

        reaped = d._reap_phase_subprocesses()

        assert reaped == 1
        assert agent_id not in d._phase_subprocess_inflight

        # _write_retroed_at must have been called.
        assert agent_id in write_retroed_calls, (
            "_write_retroed_at must be called after successful retro"
        )

        # Phase must advance to retro_done.
        assert any(
            aid == agent_id and ph == daemon.PHASE_RETRO_DONE
            for aid, ph in update_phase_calls
        ), f"Phase must advance to PHASE_RETRO_DONE; got {update_phase_calls}"

        done_events = handler.events("retro_done")
        assert len(done_events) == 1

    def test_retro_deadline_exceeded_does_not_kill_daemon(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Deadline-exceeded retro reap kills PG and advances to retro_failed."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-retro-timeout"

        d._phase_subprocess_inflight[agent_id] = {
            "phase": "retro",
            "pid": 66666,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC) - timedelta(seconds=5000),
            "deadline_at": datetime.now(UTC) - timedelta(seconds=300),
            "ctx": {"issue_number": 300, "pr_number": 301},
        }

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_process_alive", staticmethod(lambda pid: True)
        )
        kill_calls: list[int] = []
        monkeypatch.setattr(
            d, "_kill_phase_subprocess", lambda pid: kill_calls.append(pid)
        )

        update_phase_calls: list[tuple] = []
        monkeypatch.setattr(
            d,
            "_update_agent_phase",
            lambda aid, ph: update_phase_calls.append((aid, ph)),
        )

        reaped = d._reap_phase_subprocesses()

        assert reaped == 1
        assert 66666 in kill_calls
        assert agent_id not in d._phase_subprocess_inflight

        # Finalizer must advance phase to retro_failed (via timeout path).
        assert any(
            aid == agent_id and ph == daemon.PHASE_RETRO_FAILED
            for aid, ph in update_phase_calls
        ), (
            f"Phase must advance to PHASE_RETRO_FAILED on timeout; got {update_phase_calls}"
        )

        deadline_events = handler.events("phase_async_deadline_exceeded")
        assert len(deadline_events) == 1
        assert deadline_events[0].phase == "retro"


# ---------------------------------------------------------------------------
# _advance_running_agents inflight skip
# ---------------------------------------------------------------------------


class TestAdvanceRunningAgentsSkipsInflight:
    """_advance_running_agents must skip agents with an in-flight subprocess."""

    def test_inflight_agent_not_re_entered(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """If an agent_id is in _phase_subprocess_inflight, the advance loop
        must NOT call _start_verify / _run_retro_phase for it."""
        d, _conn, handler = _make_daemon(tmp_path)
        agent_id = "agent-inflight-skip"

        # Pre-register as inflight.
        d._phase_subprocess_inflight[agent_id] = {
            "phase": "verify",
            "pid": 12345,
            "worktree_path": str(tmp_path),
            "started_at": datetime.now(UTC),
            "deadline_at": datetime.now(UTC) + timedelta(seconds=3000),
            "ctx": {},
        }

        # Make the agent appear advanceable as an awaiting_deploy row.
        advanceable = [
            {
                "agent_id": agent_id,
                "issue_number": 123,
                "phase": "awaiting_deploy",
                "pr_number": 42,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "succeeded",
                "merge_unstick_attempts": 0,
                "execution_mode": "subprocess",
            }
        ]
        monkeypatch.setattr(d, "_list_advanceable_agents", lambda: advanceable)

        advance_deploy_calls: list[str] = []
        monkeypatch.setattr(
            d,
            "_advance_awaiting_deploy",
            lambda agent: advance_deploy_calls.append(agent["agent_id"]),
        )

        d._advance_running_agents()

        # Inflight agent must NOT be passed to _advance_awaiting_deploy.
        assert agent_id not in advance_deploy_calls, (
            "_advance_awaiting_deploy must not be called for an inflight agent — "
            "the reaper owns finalization. Regression guard for issue #3658."
        )

        # Event emitted for the skip.
        skip_events = handler.events("advance_skipped_async_inflight")
        assert len(skip_events) == 1
