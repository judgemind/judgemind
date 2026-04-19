"""Unit tests for the Phase 3C failure-detection + retry machinery.

Issue #2791. Covers the §7 supervisor checks and the §8 tier-1
mechanical-fix catalog:

* Stuck-timeout detection flips an agent with no recent
  ``phase_transitions`` update to ``status='crashed'`` and enqueues a
  ``stuck_timeout`` retry marker.
* GitHub rate-limit guard writes a ``gh_rate_exhausted`` failure row,
  sets the daemon-level skip flag, and suppresses both
  ``_claim_and_orchestrate_one`` and ``_advance_running_agents`` while
  active. The flag clears once the reset epoch elapses.
* Subprocess crash classification maps Claude-p stderr patterns +
  Gemini exit codes to the §8 categories.
* Retry markers respect the 3-attempt cap — a fourth enqueue flips the
  agent to ``status='failed'`` for 3D's diagnoser.
* The retry marker processor resets due agents to
  ``status='retrying' phase='claiming'``, drops the worktree, and
  increments ``retries_used``.
* Auth-fail and turn-limit categories write failure rows but do NOT
  enqueue retry markers (tier 2/3 — 3D territory).

All external calls (``gh``, ``git``, ``claude -p``, ``scripts/
cleanup_worktree.sh``, psycopg) are mocked.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Install a psycopg stub whose ``errors.UniqueViolation`` is a real
# Exception subclass — matches the pattern in test_daemon_phase3a.py /
# phase3b.py so the daemon's ``except psycopg.errors.UniqueViolation``
# resolves consistently regardless of test-collection order.
if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — a superset of the phase3b fixtures with fetchall support.
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
    logger = logging.getLogger(f"dispatcher.test.phase3c.{id(tmp_path)}")
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
# _classify_subprocess_failure — pure function
# --------------------------------------------------------------------------


class TestClassifySubprocessFailure:
    def test_claude_reached_max_turns_returns_turn_limit(self) -> None:
        tail = "last few lines...\nError: Reached max turns (500)\n"
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("claude", 1, tail)
            == daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT
        )

    def test_claude_invalid_api_key_returns_auth_fail(self) -> None:
        tail = "Error: Invalid API key\n"
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("claude", 1, tail)
            == daemon.FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL
        )

    def test_claude_401_unauthorized_returns_auth_fail(self) -> None:
        tail = "401 Unauthorized: bad token\n"
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("claude", 1, tail)
            == daemon.FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL
        )

    def test_claude_unknown_exit_returns_crash(self) -> None:
        tail = "unexpected stderr line\n"
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("claude", 1, tail)
            == daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH
        )

    def test_claude_empty_tail_returns_crash(self) -> None:
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("claude", 1, "")
            == daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH
        )

    def test_gemini_exit_53_returns_turn_limit(self) -> None:
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("gemini", 53, "")
            == daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT
        )

    def test_gemini_exit_41_returns_auth_fail(self) -> None:
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("gemini", 41, "")
            == daemon.FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL
        )

    def test_gemini_unknown_exit_returns_crash(self) -> None:
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("gemini", 2, "")
            == daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH
        )

    def test_case_insensitive_regex_match(self) -> None:
        # Claude's error surface may vary casing over time. The classifier
        # must not be brittle to it.
        tail = "reached MAX turns somewhere\n"
        assert (
            daemon.DispatcherDaemon._classify_subprocess_failure("claude", 1, tail)
            == daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT
        )


# --------------------------------------------------------------------------
# _check_stuck_agents
# --------------------------------------------------------------------------


class TestCheckStuckAgents:
    def test_stale_agent_flagged_and_retry_marker_enqueued(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Seeded row: one stale agent.
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "ralph")],
        ]
        # Order inside _create_retry_marker: (1) COUNT prior markers,
        # (2) read backoff_seconds config.
        conn.cursor_instance.fetch_queue = [
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff schedule
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # Failure row inserted with correct category.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        params = failure_inserts[0][1]
        assert params[0] == "agent-1"  # agent_id
        assert params[1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        assert params[2] == "supervisor"

        # Agent flipped to 'crashed'.
        crashed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "crashed" in e[1]
        ]
        assert crashed_updates

        # daemon.failure_detected event.
        detected = handler.events("failure_detected")
        assert (
            detected and detected[0].category == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        )

        # Retry marker inserted.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        marker_params = marker_inserts[0][1]
        assert marker_params[0] == "agent-1"
        assert marker_params[1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        assert marker_params[2] == 1  # first attempt

    def test_no_stuck_agents_returns_zero(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        assert d._check_stuck_agents() == 0

    def test_db_error_returns_zero_and_rolls_back(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._check_stuck_agents() == 0
        assert conn.rollbacks >= 1

    def test_multiple_stuck_agents_all_flagged(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                ("agent-1", 1, "ralph"),
                ("agent-2", 2, "claiming"),
            ],
        ]
        # For each agent: prior count read + backoff config read.
        conn.cursor_instance.fetch_queue = [
            (0,),
            ("[60,300,900]",),
            (0,),
            ("[60,300,900]",),
        ]
        assert d._check_stuck_agents() == 2
        detected = handler.events("failure_detected")
        assert len(detected) == 2


# --------------------------------------------------------------------------
# _check_gh_rate_limit
# --------------------------------------------------------------------------


class TestCheckGhRateLimit:
    def _fake_run_factory(
        self, remaining: int, reset_epoch: int, returncode: int = 0
    ) -> Any:
        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = returncode
            r.stdout = json.dumps({"remaining": remaining, "reset": reset_epoch})
            r.stderr = ""
            return r

        return fake_run

    def test_healthy_budget_does_not_set_skip_flag(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        future = int(datetime.now(UTC).timestamp()) + 3600
        monkeypatch.setattr(subprocess, "run", self._fake_run_factory(4500, future))
        result = d._check_gh_rate_limit()
        assert result == {"remaining": 4500, "reset_ts": future}
        assert d._gh_rate_skip_until is None
        # No failure row written.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert inserts == []
        # daemon.gh_rate_check event logged.
        assert handler.events("gh_rate_check")

    def test_low_budget_writes_failure_and_sets_flag(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        future = int(datetime.now(UTC).timestamp()) + 600
        monkeypatch.setattr(subprocess, "run", self._fake_run_factory(50, future))
        d._check_gh_rate_limit()

        # Skip flag set.
        assert d._gh_rate_skip_until is not None
        # The flag should be near the reset epoch — tolerant within 2s.
        expected = datetime.fromtimestamp(future, tz=UTC)
        delta = abs((d._gh_rate_skip_until - expected).total_seconds())
        assert delta < 2

        # Failure row written with correct category.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(inserts) == 1
        params = inserts[0][1]
        assert params[0] is None  # agent_id NULL for daemon-level signal
        assert params[1] == daemon.FAILURE_CATEGORY_GH_RATE_EXHAUSTED

        # daemon.gh_rate_limited event.
        assert handler.events("gh_rate_limited")

    def test_subprocess_failure_does_not_set_flag(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stderr = "network error"
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = d._check_gh_rate_limit()
        assert result is None
        assert d._gh_rate_skip_until is None

    def test_gh_missing_does_not_crash(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError("gh missing")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = d._check_gh_rate_limit()
        assert result is None
        assert handler.events("gh_missing")

    def test_invalid_json_returns_none(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "not json{"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._check_gh_rate_limit() is None


class TestGhRateSkipActive:
    def test_no_flag_returns_false(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._gh_rate_skip_active() is False

    def test_future_flag_returns_true(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._gh_rate_skip_until = datetime.now(UTC) + timedelta(minutes=10)
        assert d._gh_rate_skip_active() is True

    def test_past_flag_clears_and_returns_false(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        d._gh_rate_skip_until = datetime.now(UTC) - timedelta(seconds=1)
        assert d._gh_rate_skip_active() is False
        assert d._gh_rate_skip_until is None
        assert handler.events("gh_rate_skip_cleared")


# --------------------------------------------------------------------------
# _create_retry_marker — attempt counting + cap behaviour
# --------------------------------------------------------------------------


class TestCreateRetryMarker:
    def test_first_attempt_inserts_marker(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Order: (1) COUNT prior markers, (2) read backoff_seconds.
        conn.cursor_instance.fetch_queue = [
            (0,),  # prior count = 0
            ("[60,300,900]",),  # backoff read
        ]
        attempt = d._create_retry_marker(
            agent_id="a1", reason=daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        )
        assert attempt == 1
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        # attempt=1, reason=stuck_timeout
        params = marker_inserts[0][1]
        assert params[1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        assert params[2] == 1

        created = handler.events("retry_marker_created")
        assert created and created[0].attempt == 1
        assert created[0].delay_seconds == 60

    def test_third_attempt_still_inserts(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (2,),  # prior attempts 1 and 2 already exist
            ("[60,300,900]",),
        ]
        attempt = d._create_retry_marker(
            agent_id="a1", reason=daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH
        )
        assert attempt == 3
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        params = marker_inserts[0][1]
        assert params[2] == 3

    def test_fourth_attempt_blocked_and_agent_failed(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Cap reached — only the prior-count read runs; no backoff read.
        conn.cursor_instance.fetch_queue = [
            (3,),
        ]
        attempt = d._create_retry_marker(
            agent_id="a1", reason=daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        )
        assert attempt is None
        # No new marker.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == []
        # daemon.retry_escalated event fires.
        assert handler.events("retry_escalated")
        # Agent flipped to 'failed'.
        failed = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed

    def test_non_tier_1_category_rejected(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # auth_fail is tier-2/3 territory (not in AUTO_RETRY_CATEGORIES).
        attempt = d._create_retry_marker(
            agent_id="a1", reason=daemon.FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL
        )
        assert attempt is None
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == []
        assert handler.events("retry_marker_skipped")

    def test_turn_limit_category_rejected(self, tmp_path: Path) -> None:
        # turn_limit is also out — tier 2, 3D handles retry-with-hint.
        d, _conn, handler = _make_daemon(tmp_path)
        attempt = d._create_retry_marker(
            agent_id="a1", reason=daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT
        )
        assert attempt is None
        assert handler.events("retry_marker_skipped")

    def test_malformed_backoff_config_falls_back_to_default(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (0,),  # prior count
            ("not valid json",),  # bad backoff config → falls back
        ]
        attempt = d._create_retry_marker(
            agent_id="a1", reason=daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        )
        assert attempt == 1
        created = handler.events("retry_marker_created")
        assert created and created[0].delay_seconds == daemon.DEFAULT_BACKOFF_SECONDS[0]


# --------------------------------------------------------------------------
# _backoff_seconds config reader
# --------------------------------------------------------------------------


class TestBackoffSeconds:
    def test_reads_json_list(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("[10, 20, 30]",)]
        assert d._backoff_seconds() == (10, 20, 30)

    def test_reads_python_list(self, tmp_path: Path) -> None:
        # psycopg returns JSONB as a decoded Python list.
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([10, 20, 30],)]
        assert d._backoff_seconds() == (10, 20, 30)

    def test_missing_row_returns_default(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._backoff_seconds() == daemon.DEFAULT_BACKOFF_SECONDS

    def test_non_list_returns_default(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [({"oops": True},)]
        assert d._backoff_seconds() == daemon.DEFAULT_BACKOFF_SECONDS

    def test_wrong_length_returns_default(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([10, 20],)]
        assert d._backoff_seconds() == daemon.DEFAULT_BACKOFF_SECONDS

    def test_non_positive_returns_default(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([60, 0, 900],)]
        assert d._backoff_seconds() == daemon.DEFAULT_BACKOFF_SECONDS


# --------------------------------------------------------------------------
# _process_retry_markers
# --------------------------------------------------------------------------


class TestProcessRetryMarkers:
    def test_due_marker_resets_agent(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,  # marker_id
                    "agent-1",  # agent_id
                    daemon.FAILURE_CATEGORY_STUCK_TIMEOUT,  # reason
                    1,  # attempt
                    str(tmp_path / "worktrees" / "agent-abc"),  # worktree_path
                    42,  # issue_number
                ),
            ],
        ]

        cleanup_called = {"count": 0}

        def fake_cleanup(worktree_path: str) -> bool:
            cleanup_called["count"] += 1
            return True

        monkeypatch.setattr(d, "_drop_worktree_best_effort", fake_cleanup)

        processed = d._process_retry_markers()
        assert processed == 1
        assert cleanup_called["count"] == 1

        # Agent reset to retrying + claiming with retries_used++.
        resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retrying" in (e[0] if isinstance(e[0], str) else "")
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert resets
        # Marker resolved.
        resolves = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.retry_markers" in e[0] and "resolved_at" in e[0]
        ]
        assert resolves

        # daemon.retry_processed event.
        assert handler.events("retry_processed")

    def test_no_due_markers_returns_zero(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        assert d._process_retry_markers() == 0

    def test_db_error_on_scan_returns_zero(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("scan failed")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._process_retry_markers() == 0
        assert conn.rollbacks >= 1

    def test_worktree_cleanup_failure_still_resets_agent(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,
                    "agent-1",
                    daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH,
                    1,
                    "/nonexistent",
                    42,
                ),
            ],
        ]

        # cleanup reports failure, but marker processor keeps going.
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: False)

        processed = d._process_retry_markers()
        assert processed == 1
        resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert resets


# --------------------------------------------------------------------------
# _drop_worktree_best_effort
# --------------------------------------------------------------------------


class TestDropWorktreeBestEffort:
    def test_cleanup_script_success(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # Fake a cleanup_worktree.sh at the expected location.
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "cleanup_worktree.sh").write_text("#!/bin/bash\nexit 0\n")

        # Pin _repo_root() to the tmp path so the script is found there.
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._drop_worktree_best_effort("/some/path") is True

    def test_empty_path_returns_false(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._drop_worktree_best_effort("") is False

    def test_fallback_to_git_worktree_remove(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # No cleanup_worktree.sh — falls straight through to git.
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            # Only git worktree remove should be called.
            assert cmd[:3] == ["git", "-C", str(tmp_path)]
            assert "worktree" in cmd and "remove" in cmd and "--force" in cmd
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._drop_worktree_best_effort("/some/path") is True

    def test_git_failure_returns_false(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "no such worktree"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._drop_worktree_best_effort("/some/path") is False


# --------------------------------------------------------------------------
# _handle_subprocess_failure — classifier + retry-marker wiring via
# _run_subprocess_or_fail's post-exit path
# --------------------------------------------------------------------------


class TestHandleSubprocessFailure:
    def test_crash_writes_failure_and_enqueues_retry(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (0,),  # prior count
            ("[60,300,900]",),  # backoff
        ]

        d._handle_subprocess_failure(
            agent_id="a1",
            phase="plan",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail="some generic error\n",
            duration_s=1.23,
            extra={},
        )

        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH

        # Retry marker enqueued (tier 1 auto-retry).
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts

    def test_turn_limit_does_not_enqueue_retry(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        d._handle_subprocess_failure(
            agent_id="a1",
            phase="ralph",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail="Error: Reached max turns (500)\n",
            duration_s=10.0,
            extra={},
        )

        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT
        # No retry marker for tier-2 category.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == []
        # Agent still marked 'failed' for 3D to handle.
        failed = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed

    def test_auth_fail_does_not_enqueue_retry(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        d._handle_subprocess_failure(
            agent_id="a1",
            phase="plan",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail="Error: Invalid API key\n",
            duration_s=0.5,
            extra={},
        )

        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == []


# --------------------------------------------------------------------------
# supervisor_tick integration — checks run, ticks survive check failures
# --------------------------------------------------------------------------


class TestSupervisorTickIntegration:
    def test_supervisor_tick_calls_new_checks(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # 1st fetchone: failures-in-last-hour count.
        conn.cursor_instance.fetch_queue = [(0,)]

        called: dict[str, int] = {
            "stuck": 0,
            "rate": 0,
            "advance": 0,
            "retry": 0,
        }
        d._check_stuck_agents = lambda: (
            called.__setitem__("stuck", called["stuck"] + 1) or 0
        )  # type: ignore[method-assign,assignment,return-value]
        d._check_gh_rate_limit = lambda: (
            called.__setitem__("rate", called["rate"] + 1) or None
        )  # type: ignore[method-assign,assignment,return-value]
        d._advance_running_agents = lambda: (
            called.__setitem__("advance", called["advance"] + 1) or 0
        )  # type: ignore[method-assign,assignment,return-value]
        d._process_retry_markers = lambda: (
            called.__setitem__("retry", called["retry"] + 1) or 0
        )  # type: ignore[method-assign,assignment,return-value]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        d.supervisor_tick()
        assert called == {"stuck": 1, "rate": 1, "advance": 1, "retry": 1}

    def test_supervisor_tick_skips_advance_when_rate_limited(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        # Pre-set the skip window in the future.
        d._gh_rate_skip_until = datetime.now(UTC) + timedelta(minutes=10)

        called: dict[str, int] = {"advance": 0}

        def bad_advance() -> int:
            called["advance"] += 1
            return 0

        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._advance_running_agents = bad_advance  # type: ignore[method-assign]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        summary = d.supervisor_tick()
        assert called["advance"] == 0
        assert summary["rate_skip_active"] == 1
        assert handler.events("advance_skipped_rate_limited")

    def test_supervisor_tick_survives_check_exceptions(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        def boom() -> int:
            raise RuntimeError("stuck check broke")

        # Stuck check raises, but the tick continues.
        d._check_stuck_agents = boom  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        summary = d.supervisor_tick()
        # Heartbeat still updated (even with stuck-check failure).
        hb_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.runs SET heartbeat_ts" in e[0]
        ]
        assert hb_updates
        assert summary["heartbeat_metric_emitted"] == 1
        # daemon.stuck_check_failed logged.
        assert handler.events("stuck_check_failed")


# --------------------------------------------------------------------------
# _resume_retrying_agent — Phase 3C-to-3A handoff
# --------------------------------------------------------------------------


class TestResumeRetryingAgent:
    def test_no_retrying_agent_returns_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._resume_retrying_agent() is False

    def test_retrying_agent_flipped_and_reorchestrated(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("resume-agent-id", 77)]
        monkeypatch.setattr(d, "_create_worktree", lambda _aid: tmp_path / "resumed")
        called: dict[str, Any] = {}

        def fake_run_phases(agent_id: str, issue_number: int, worktree: Path) -> None:
            called["agent_id"] = agent_id
            called["issue_number"] = issue_number
            called["worktree"] = worktree

        monkeypatch.setattr(d, "_run_orchestration_phases", fake_run_phases)

        assert d._resume_retrying_agent() is True
        # Status/phase values are literal-inline in the UPDATE SQL,
        # not params. Check the SQL text itself.
        resume_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "status = 'running'" in e[0]
            and "phase = 'claiming'" in e[0]
        ]
        assert resume_updates
        assert handler.events("resume_retrying_agent")
        assert called["agent_id"] == "resume-agent-id"
        assert called["issue_number"] == 77

    def test_worktree_create_failure_marks_failed_but_consumes_slot(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("resume-agent-id", 77)]

        def boom(_aid: str) -> Path:
            raise RuntimeError("worktree add exploded")

        monkeypatch.setattr(d, "_create_worktree", boom)
        monkeypatch.setattr(
            d,
            "_run_orchestration_phases",
            MagicMock(
                side_effect=AssertionError("must not run phases on bad worktree")
            ),
        )

        assert d._resume_retrying_agent() is True
        failed = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed
        assert handler.events("resume_worktree_create_failed")

    def test_claim_and_orchestrate_defers_to_resume_when_present(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_resume_retrying_agent", lambda: True)
        monkeypatch.setattr(
            d,
            "_latest_queue_snapshot_issues",
            MagicMock(
                side_effect=AssertionError("new-claim path must not run after resume")
            ),
        )
        # Should return early without exception.
        d._claim_and_orchestrate_one()


# --------------------------------------------------------------------------
# scheduler_tick gate respects rate-limit skip flag
# --------------------------------------------------------------------------


class TestSchedulerTickRateLimit:
    def test_scheduler_tick_skips_claim_when_rate_limited(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # 1st: commands UPDATE (no fetch). 2nd: concurrency_cap read.
        conn.cursor_instance.fetch_queue = [(1,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._claim_and_orchestrate_one = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called during rate limit")
        )
        d._gh_rate_skip_until = datetime.now(UTC) + timedelta(minutes=10)
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 0
        assert d._claim_and_orchestrate_one.call_count == 0  # type: ignore[attr-defined]
        assert handler.events("claim_skipped_rate_limited")
