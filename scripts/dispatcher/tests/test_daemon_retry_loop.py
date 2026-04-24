"""End-to-end integration test for the stuck → marker → process → resume loop.

Issue #2794. Wires _check_stuck_agents → _process_retry_markers →
_claim_and_orchestrate_one → _resume_retrying_agent →
_run_orchestration_phases in one contiguous test against the phase3c
fake-DB fixtures (#2791).

Helper classes (_FakeCursor, _FakeConnection, _CapturingLogHandler,
_make_daemon) are intentionally copy-pasted from test_daemon_phase3c.py.
The duplication is the project standard — keeping tests hermetic
outweighs the DRY benefit for daemon fixtures (see comment at
test_daemon_phase3c.py:47).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — copy of the phase3c fixtures (hermetic by design).
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
    logger = logging.getLogger(f"dispatcher.test.retry_loop.{id(tmp_path)}")
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
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# TestRetryLoopEndToEnd
# --------------------------------------------------------------------------


class TestRetryLoopEndToEnd:
    """One-shot walkthrough of stuck_timeout → retry_marker → process → resume.

    AC1: exercises the full 4-method sequence in a single test.
    AC2: `_latest_queue_snapshot_issues` is stubbed to raise AssertionError
         so that if `_claim_and_orchestrate_one` stops calling
         `_resume_retrying_agent` (i.e. the resumed= block at daemon.py:8157
         is removed), the test fails — either the AssertionError propagates
         from the claim path or `_run_orchestration_phases` is called with a
         fresh UUID instead of "retry-agent-1", causing the assertion on
         agent_id/issue_number to fail.
    """

    def test_stuck_agent_retries_and_resumes(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # ── Step 1: _check_stuck_agents ────────────────────────────────
        # Seed one stuck ralph agent.  Elapsed 55000s > 54000s (ralph
        # per-phase threshold, bumped 10× by #2885).
        conn.cursor_instance.fetchall_queue = [
            [("retry-agent-1", 2794, "ralph", 55000.0)],
        ]
        # Per _check_stuck_agents + _create_retry_marker call order:
        # (1) stuck_timeout_s_by_phase config override (unset → None),
        # (2) RETURNING failure_id from _write_failure INSERT,
        # (3) _has_prior_stuck_timeout_in_window (no prior → None),
        # (4) COUNT prior retry markers (0 → first attempt),
        # (5) backoff schedule.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
            (42,),  # failure_id RETURNING from _write_failure
            None,  # _has_prior_stuck_timeout_in_window — no prior
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff schedule
        ]

        flagged = d._check_stuck_agents()

        # ── Step 2: verify _check_stuck_agents results ─────────────────
        assert flagged == 1

        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        marker_params = marker_inserts[0][1]
        assert marker_params[0] == "retry-agent-1"
        assert marker_params[1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        assert marker_params[2] == 1  # attempt number

        # ── Step 3: seed for _process_retry_markers ────────────────────
        # Simulate a due marker row (backoff elapsed — the fake cursor
        # ignores the retry_after_ts <= now() SQL filter).
        worktree_dir = tmp_path / "worktrees" / "retry-agent-1"
        worktree_dir.mkdir(parents=True)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,  # marker_id
                    "retry-agent-1",  # agent_id
                    daemon.FAILURE_CATEGORY_STUCK_TIMEOUT,  # reason
                    1,  # attempt
                    str(worktree_dir),  # worktree_path
                    2794,  # issue_number
                ),
            ],
        ]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        processed = d._process_retry_markers()

        # ── Step 4: verify _process_retry_markers results ──────────────
        assert processed == 1

        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "status = 'retrying'" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert retrying_resets, (
            "expected UPDATE dispatcher.agents SET status='retrying' ... "
            "retries_used = retries_used + 1 from _process_retry_markers"
        )

        # ── Step 5: seed for _claim_and_orchestrate_one / _resume ──────
        # The _resume_retrying_agent SELECT uses fetchone().
        conn.cursor_instance.fetch_queue = [
            ("retry-agent-1", 2794),  # resume SELECT result
        ]

        # Fresh worktree path that _create_worktree will return.
        resumed_worktree = tmp_path / "resumed"
        resumed_worktree.mkdir()
        monkeypatch.setattr(d, "_create_worktree", lambda _agent_id: resumed_worktree)

        # AC2 regression guard: if _resume_retrying_agent is not called,
        # _claim_and_orchestrate_one falls through to
        # _latest_queue_snapshot_issues.  Stub it to raise so the test
        # fails loudly rather than silently claiming a new issue.
        def _forbidden_snapshot() -> list[int]:
            raise AssertionError(
                "claim path must not run after resume — "
                "_resume_retrying_agent was not called"
            )

        monkeypatch.setattr(d, "_latest_queue_snapshot_issues", _forbidden_snapshot)

        # Capture _run_orchestration_phases arguments.
        captured: dict[str, Any] = {}

        def _fake_run_phases(
            agent_id: str,
            issue_number: int,
            worktree: Path,
        ) -> None:
            captured["agent_id"] = agent_id
            captured["issue_number"] = issue_number
            captured["worktree"] = worktree

        monkeypatch.setattr(d, "_run_orchestration_phases", _fake_run_phases)

        # ── Step 6: call entry point and assert ────────────────────────
        d._claim_and_orchestrate_one()

        # _run_orchestration_phases must have been called with the
        # retrying agent's identity, not a freshly-generated UUID.
        assert captured.get("agent_id") == "retry-agent-1", (
            f"expected agent_id='retry-agent-1', got {captured.get('agent_id')!r}"
        )
        assert captured.get("issue_number") == 2794, (
            f"expected issue_number=2794, got {captured.get('issue_number')!r}"
        )
        assert captured.get("worktree") == resumed_worktree, (
            f"expected worktree={resumed_worktree!r}, got {captured.get('worktree')!r}"
        )

        # The resume_retrying_agent log event must have fired exactly once.
        resume_events = handler.events("resume_retrying_agent")
        assert len(resume_events) == 1, (
            f"expected exactly one 'resume_retrying_agent' log event, "
            f"got {len(resume_events)}"
        )
