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

        # Seeded row: one stale ralph agent. Elapsed 55000s > 54000s
        # (ralph default threshold bumped 10× in #2885). #2872 — the
        # SELECT returns (agent_id, issue_number, phase, elapsed_seconds)
        # and the Python-side comparison applies the per-phase
        # threshold. The kind column was removed from the SELECT
        # post-#2927 — every row is daemon-owned by construction.
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "ralph", 55000.0)],
        ]
        # Order inside _check_stuck_agents + _create_retry_marker:
        # (1) stuck_timeout_s_by_phase override read (returns None → fall
        # through to module defaults),
        # (2) COUNT prior markers in _create_retry_marker,
        # (3) read backoff_seconds config.
        # Post-#2927 the #2903 _lookup_agent_kind defensive fetch is
        # gone — no task-skill rows exist to guard against.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
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
        # Two stuck agents: ralph exceeds 54000s threshold at 55000s,
        # claiming exceeds 5min threshold at 600s. (#2872 per-phase
        # thresholds replace the single 30min global; #2885 bumped
        # ralph 10× from 90min to 15hr.)
        conn.cursor_instance.fetchall_queue = [
            [
                ("agent-1", 1, "ralph", 55000.0),
                ("agent-2", 2, "claiming", 600.0),
            ],
        ]
        # Queue: (1) stuck_timeout_s_by_phase override (unset),
        # then for each agent: (1) prior marker count, (2) backoff config.
        # Post-#2927 the #2903 _lookup_agent_kind fetch is gone.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase — unset
            (0,),  # prior marker count for agent-1
            ("[60,300,900]",),  # backoff
            (0,),  # prior marker count for agent-2
            ("[60,300,900]",),  # backoff
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
        # Order: (1) COUNT prior markers, (2) backoff. Post-#2927 the
        # #2903 _lookup_agent_kind fetch is gone — every row is
        # daemon-owned, no task-skill guard needed.
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
        # Cap reached — only the prior-count read; no backoff read.
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

    # ------------------------------------------------------------------
    # Issue #2936 — infra-preemption retries don't count toward attempt
    # budget. Paired tests: one infra-preemption reason preserves the
    # prior attempt; one budgeted reason increments as before. Both
    # assert the ``retry_counted`` field on the ``retry_processed``
    # log event for CloudWatch observability.
    # ------------------------------------------------------------------

    def test_daemon_restart_takes_terminal_and_reclaim_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """After #2925: ``daemon_restart_abandoned`` takes the terminal-and-reclaim path.

        Before #2925 this test asserted the old reset-to-retrying behavior.
        After #2925 infra-preemption markers call ``_mark_agent_terminal``
        (status='failed', phase=reason) and re-add ``agent/ready`` instead
        of resetting to ``status='retrying'``. The ``retry_processed`` event
        is no longer emitted; ``retry_terminal_and_reclaim`` takes its place.
        The ``retries_used`` column is still not incremented (preservation
        semantics carry over to the terminal-and-reclaim path).
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,  # marker_id
                    "agent-preempted-1",
                    daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
                    2,  # attempt (per-reason marker counter)
                    str(tmp_path / "worktrees" / "agent-preempted-1"),
                    42,  # issue_number
                ),
            ],
        ]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)
        monkeypatch.setattr(d, "_gh_issue_add_labels", lambda n, l: None)

        processed = d._process_retry_markers()
        assert processed == 1

        # retries_used must NOT be incremented.
        incremented = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert incremented == [], (
            "daemon_restart_abandoned must not increment retries_used"
        )

        # No reset to status='retrying' — the new path marks the row terminal.
        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "status = 'retrying'" in e[0]
        ]
        assert retrying_resets == [], (
            "infra-preemption path must not emit retrying reset after #2925"
        )

        # retry_terminal_and_reclaim event emitted; NOT retry_processed.
        reclaim = handler.events("retry_terminal_and_reclaim")
        assert len(reclaim) == 1
        assert getattr(reclaim[0], "reason", None) == (
            daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
        )
        assert handler.events("retry_processed") == []

    def test_killswitch_takes_terminal_and_reclaim_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """After #2925: ``paused_by_killswitch`` takes the terminal-and-reclaim path.

        Parallel to the daemon-restart case. Before #2925 this test
        asserted reset-to-retrying behavior; after #2925 it asserts
        the terminal path.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    2,  # marker_id
                    "agent-killswitched-1",
                    daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH,
                    2,  # attempt
                    str(tmp_path / "worktrees" / "agent-killswitched-1"),
                    43,
                ),
            ],
        ]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)
        monkeypatch.setattr(d, "_gh_issue_add_labels", lambda n, l: None)

        processed = d._process_retry_markers()
        assert processed == 1

        incremented = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert incremented == [], (
            "paused_by_killswitch must not increment retries_used"
        )

        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "status = 'retrying'" in e[0]
        ]
        assert retrying_resets == []

        reclaim = handler.events("retry_terminal_and_reclaim")
        assert len(reclaim) == 1
        assert getattr(reclaim[0], "reason", None) == (
            daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH
        )
        assert handler.events("retry_processed") == []

    def test_subprocess_crash_increments_attempt_at_2(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A budgeted retry at attempt=2 increments retries_used so the agent resumes at attempt=3.

        Parallel to ``test_daemon_restart_preserves_prior_attempt``
        for the opposite case — budgeted reasons still consume the
        retry budget. The ``daemon.retry_processed`` log event records
        ``retry_counted=true``.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    3,  # marker_id
                    "agent-crashed-1",
                    daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH,
                    2,  # attempt
                    str(tmp_path / "worktrees" / "agent-crashed-1"),
                    44,
                ),
            ],
        ]
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        processed = d._process_retry_markers()
        assert processed == 1

        # Reset UPDATE includes retries_used++ for budgeted reasons.
        incremented = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert len(incremented) == 1, (
            "subprocess_crash retry must increment retries_used"
        )

        events = handler.events("retry_processed")
        assert len(events) == 1
        assert events[0].retry_counted is True
        assert events[0].reason == daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH
        assert events[0].attempt == 2

    def test_infra_preemption_categories_contents(self) -> None:
        """Regression guard — the preemption set contains exactly the two infra categories.

        Adding a new category must be a conscious decision; tripping
        this assert forces a review of whether the new category is
        truly infrastructure preemption or an agent-driven failure.
        """
        assert daemon._INFRA_PREEMPTION_CATEGORIES == frozenset(
            {
                daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
                daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH,
            }
        )

    def test_killswitch_failure_category_string_matches_phase(self) -> None:
        """``FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH`` == ``KILLSWITCH_TERMINAL_PHASE``.

        The issue body explicitly directs us to reuse the phase
        string value for the new failure-category constant so a
        future killswitch retry path can write the same string to
        both ``phase`` (via _mark_agent_terminal) and
        ``retry_markers.reason`` (via _create_retry_marker) and have
        the preemption classifier recognize it without an extra
        mapping step.
        """
        assert (
            daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH
            == daemon.KILLSWITCH_TERMINAL_PHASE
            == "paused_by_killswitch"
        )


# --------------------------------------------------------------------------
# Issue #2925 — budgeted retries still use the reset-to-retrying path
# --------------------------------------------------------------------------


class TestProcessRetryMarkersBudgetedRetriesUnchanged:
    """Regression guard: non-infra retries still reset to retrying/claiming.

    After #2925 added the terminal-and-reclaim branch for infra-preemption,
    the budgeted paths (``subprocess_crash``, ``stuck_timeout``) must
    continue to use the original reset-to-retrying flow, including:
    - ``UPDATE dispatcher.agents SET status='retrying' phase='claiming'
      retries_used = retries_used + 1``
    - ``INSERT INTO dispatcher.phase_transitions (retry_reset)``
    - ``daemon.retry_processed`` log event (not ``retry_terminal_and_reclaim``)
    """

    def _make_budgeted_row(
        self,
        tmp_path: Path,
        reason: str,
    ) -> tuple[Any, Any, Any]:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    10,  # marker_id
                    "agent-budgeted",
                    reason,
                    1,  # attempt
                    str(tmp_path / "worktrees" / "agent-budgeted"),
                    99,  # issue_number
                ),
            ],
        ]
        return d, conn, handler

    def test_subprocess_crash_still_resets_to_retrying(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``subprocess_crash`` takes the reset-to-retrying path, not terminal."""
        d, conn, handler = self._make_budgeted_row(
            tmp_path, daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH
        )
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        processed = d._process_retry_markers()
        assert processed == 1

        # status='retrying' reset emitted with retries_used++.
        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "status = 'retrying'" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert retrying_resets, (
            "subprocess_crash must still emit retrying/claiming reset with retries_used++"
        )

        # retry_reset phase_transition inserted.
        retry_reset_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
            and e[1] is not None
            and "retry_reset" in str(e[1])
        ]
        assert retry_reset_inserts, (
            "subprocess_crash must insert retry_reset phase_transition"
        )

        # retry_processed event (not terminal_and_reclaim).
        assert handler.events("retry_processed"), (
            "subprocess_crash must emit retry_processed, not terminal_and_reclaim"
        )
        assert handler.events("retry_terminal_and_reclaim") == [], (
            "subprocess_crash must NOT emit retry_terminal_and_reclaim"
        )

    def test_stuck_timeout_still_resets_to_retrying(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``stuck_timeout`` takes the reset-to-retrying path, not terminal."""
        d, conn, handler = self._make_budgeted_row(
            tmp_path, daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        )
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        processed = d._process_retry_markers()
        assert processed == 1

        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "status = 'retrying'" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert retrying_resets, (
            "stuck_timeout must still emit retrying/claiming reset with retries_used++"
        )

        retry_reset_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
            and e[1] is not None
            and "retry_reset" in str(e[1])
        ]
        assert retry_reset_inserts, (
            "stuck_timeout must insert retry_reset phase_transition"
        )

        assert handler.events("retry_processed")
        assert handler.events("retry_terminal_and_reclaim") == []


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


# --------------------------------------------------------------------------
# #2821 — phase_failure_log secondary event + stderr_tail/preview 2000 cap
# + cleanup uses _git_parent_root
# --------------------------------------------------------------------------


def _write_phase_log(worktree: Path, phase: str, body: str) -> None:
    log_dir = worktree / "tmp"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"claude-p-{phase}.log").write_text(body)


class TestPhaseFailureLogEvent:
    """Full-log capture via secondary CloudWatch event (#2821).

    Each ``daemon.subprocess_failed`` is followed by a
    ``daemon.phase_failure_log`` event carrying up to 10k chars of the
    full merged stdout+stderr log. Both share ``agent_id`` so a single
    ``filter-log-events`` query returns the pair.
    """

    def test_emits_secondary_event_with_full_log_under_cap(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), ("[60,300,900]",)]
        worktree = tmp_path / "wt"
        body = "short full log body"
        _write_phase_log(worktree, "plan", body)

        d._handle_subprocess_failure(
            agent_id="a1",
            phase="plan",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail="tail here",
            duration_s=1.0,
            extra={},
            worktree=worktree,
        )

        primary = handler.events("subprocess_failed")
        secondary = handler.events("phase_failure_log")
        assert primary and secondary, "both events must fire"
        # Both share agent_id for the single-query triage pattern.
        assert getattr(primary[0], "agent_id", None) == "a1"
        assert getattr(secondary[0], "agent_id", None) == "a1"
        assert getattr(secondary[0], "phase", None) == "plan"
        assert getattr(secondary[0], "log_body", None) == body
        assert getattr(secondary[0], "log_chars_total", None) == len(body)
        assert getattr(secondary[0], "log_chars_emitted", None) == len(body)

    def test_truncates_log_body_at_10k_chars(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), ("[60,300,900]",)]
        worktree = tmp_path / "wt"
        # 15k-char log: 10k emitted, 15k total.
        body = "A" * 15000
        _write_phase_log(worktree, "plan", body)

        d._handle_subprocess_failure(
            agent_id="a2",
            phase="plan",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail="...",
            duration_s=1.0,
            extra={},
            worktree=worktree,
        )

        secondary = handler.events("phase_failure_log")
        assert secondary
        record = secondary[0]
        emitted = getattr(record, "log_body", "")
        assert len(emitted) == 10000
        assert getattr(record, "log_chars_total", None) == 15000
        assert getattr(record, "log_chars_emitted", None) == 10000
        # Tail preserved — failures surface at the tail, not the head.
        assert emitted == "A" * 10000

    def test_secondary_event_skipped_when_log_empty(self, tmp_path: Path) -> None:
        """No worktree log on disk → no secondary event.

        Prevents the no-signal edge case (e.g. preflight exception
        before subprocess spawn) from spamming CloudWatch with empty
        ``phase_failure_log`` envelopes.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), ("[60,300,900]",)]
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # No claude-p-plan.log written.

        d._handle_subprocess_failure(
            agent_id="a3",
            phase="plan",
            reason="timeout",
            exit_code=None,
            stderr_tail="",
            duration_s=None,
            extra={"timeout_seconds": 60},
            worktree=worktree,
        )

        primary = handler.events("subprocess_failed")
        secondary = handler.events("phase_failure_log")
        assert primary, "primary event still fires"
        assert not secondary, "secondary event must be skipped when log is empty"

    def test_secondary_event_skipped_when_worktree_arg_omitted(
        self, tmp_path: Path
    ) -> None:
        """Legacy callers that predate the worktree arg don't emit the event."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), ("[60,300,900]",)]

        d._handle_subprocess_failure(
            agent_id="a4",
            phase="plan",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail="tail",
            duration_s=1.0,
            extra={},
        )
        assert handler.events("subprocess_failed")
        assert not handler.events("phase_failure_log")


class TestStderrTailSizeCap:
    """Primary ``subprocess_failed`` event's ``stderr_tail`` capped at 2000 (#2821)."""

    def test_stderr_tail_emitted_verbatim_at_2000_chars(self, tmp_path: Path) -> None:
        """``_handle_subprocess_failure`` forwards the caller's tail verbatim.

        The 2000-char cap is applied at the call-site via
        ``_log_tail(max_chars=PHASE_STDERR_TAIL_MAX_CHARS)`` in
        ``_run_subprocess_or_fail``. This test verifies the pipeline
        preserves whatever size the caller hands in — the companion
        test below exercises the call-site cap end-to-end.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), ("[60,300,900]",)]
        tail = "T" * 2000

        d._handle_subprocess_failure(
            agent_id="a5",
            phase="plan",
            reason="nonzero_exit",
            exit_code=1,
            stderr_tail=tail,
            duration_s=1.0,
            extra={},
        )
        primary = handler.events("subprocess_failed")
        assert primary
        assert getattr(primary[0], "stderr_tail", None) == tail
        assert len(getattr(primary[0], "stderr_tail", "")) == 2000

    def test_run_subprocess_or_fail_caps_tail_at_2000(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The call-site tail read in ``_run_subprocess_or_fail`` honours
        :data:`PHASE_STDERR_TAIL_MAX_CHARS` (2000)."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), ("[60,300,900]",)]
        worktree = tmp_path / "wt"
        # 3000 chars — if the old 500 cap were still in place the tail
        # would be 500; with 2000 the tail is 2000.
        _write_phase_log(worktree, "plan", "X" * 3000)

        monkeypatch.setattr(d, "_spawn_phase_subprocess", lambda *a, **k: (1, 1.0))
        result = d._run_subprocess_or_fail("a6", "plan", worktree)
        assert result is None  # subprocess failed

        primary = handler.events("subprocess_failed")
        assert primary
        tail = getattr(primary[0], "stderr_tail", "")
        assert len(tail) == 2000
        assert tail == "X" * 2000


class TestDropWorktreeBestEffortGitParent:
    """#2821 — ``git worktree remove`` anchored to ``_git_parent_root``.

    In Fargate, ``_repo_root()`` returns ``/app`` (container CWD), which
    does NOT contain a ``.git`` directory — the baseline clone lives at
    ``_git_parent_root()`` instead. Before this fix, the cleanup
    fallback ran ``git -C /app worktree remove`` and failed with "fatal:
    not a git repository".
    """

    def test_uses_git_parent_root_when_baseline_set(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # Simulate Fargate layout: _repo_root = /app (no .git),
        # _git_parent_root = baseline clone at /var/lib/.../judgemind.
        repo_root = tmp_path / "app"
        repo_root.mkdir()
        baseline = tmp_path / "var" / "lib" / "dispatcher" / "judgemind"
        baseline.mkdir(parents=True)
        monkeypatch.setattr(d, "_repo_root", lambda: repo_root)
        monkeypatch.setattr(d, "_git_parent_root", lambda: baseline)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # No cleanup_worktree.sh at the repo_root — forces git fallback.
        assert d._drop_worktree_best_effort("/some/worktree") is True

        # Only one call (the fallback), anchored to the baseline via -C.
        git_remove = [c for c in captured if "worktree" in c and "remove" in c]
        assert git_remove
        cmd = git_remove[0]
        assert cmd[0] == "git"
        assert cmd[1] == "-C"
        assert cmd[2] == str(baseline)  # NOT str(repo_root)

    def test_uses_git_parent_root_even_when_cwd_outside_baseline(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Regression test for the actual #2821 symptom — ``git worktree
        remove`` was running with no ``-C`` anchored correctly and
        failing with "fatal: not a git repository" because the daemon's
        CWD had no ``.git`` child. The fix anchors to
        ``_git_parent_root()`` so the command works regardless of CWD.
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        repo_root = tmp_path / "somewhere_else"
        repo_root.mkdir()
        baseline = tmp_path / "baseline_clone"
        baseline.mkdir()
        monkeypatch.setattr(d, "_repo_root", lambda: repo_root)
        monkeypatch.setattr(d, "_git_parent_root", lambda: baseline)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._drop_worktree_best_effort("/some/worktree") is True

        git_remove = [c for c in captured if "worktree" in c and "remove" in c]
        assert git_remove
        assert git_remove[0][2] == str(baseline)
        assert git_remove[0][2] != str(repo_root)


class TestPersistPhaseOutputLogText:
    """#2821 — ``dispatcher.phase_outputs.log_text`` column is written."""

    def test_insert_includes_log_text_column(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        d._persist_phase_output(
            agent_id="a1",
            phase="plan",
            output_json={"go": True},
            log_text="the full phase log body",
        )

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert inserts
        sql, params = inserts[0]
        assert "log_text" in sql
        # Params ordering: (agent_id, phase, output_json_dumped, log_text)
        assert params[0] == "a1"
        assert params[1] == "plan"
        assert params[3] == "the full phase log body"

    def test_log_text_defaults_to_none_when_omitted(self, tmp_path: Path) -> None:
        """Backwards-compat for any caller that forgot to pass log_text."""
        d, conn, _handler = _make_daemon(tmp_path)

        d._persist_phase_output("a2", "plan", {"go": True})

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert inserts
        _, params = inserts[0]
        assert params[3] is None


class TestReadFullPhaseLog:
    """#2821 — full log body read helper."""

    def test_returns_full_body_no_truncation(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        body = "Z" * 50000
        _write_phase_log(worktree, "plan", body)
        assert d._read_full_phase_log(worktree, "plan") == body

    def test_returns_empty_when_log_missing(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        assert d._read_full_phase_log(worktree, "plan") == ""
