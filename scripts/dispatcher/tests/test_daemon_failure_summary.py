"""Unit tests for ``_build_failure_summary`` + ``_mark_agent_terminal``'s
``dispatcher.agents.failure_summary`` write-path (issue #2900).

The daemon populates ``failure_summary`` at terminal-time for rows where
``status IN ('failed', 'crashed', 'plan_blocked')``. The template pulls
from ``dispatcher.failures`` (category + details) and
``dispatcher.phase_outputs`` (phase, log_text), joined by agent_id.
Summary is ≤240 chars.

This test file is the ONLY consumer of ``_build_failure_summary`` —
other call sites use the higher-level ``_mark_agent_terminal`` wrapper
which calls the helper internally. We test the template function
directly so the AC verify-line ("unit test asserts that a failed agent
with category='subprocess_turn_limit', phase='ralph-reviewer (3)',
exit_code=124 produces a summary like …") passes on a pure function
without threading fetch mocks through the full terminal path.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Provide a stub ``psycopg`` module before importing the daemon — same
# pattern as test_daemon_phase3a/3b/3c/3d/3e.py.
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
# Shared fakes — same shape as test_daemon_phase3e.
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
    logger = logging.getLogger(f"dispatcher.test.failure_summary.{id(tmp_path)}")
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
# _build_failure_summary — template function (pure-ish — reads one row)
# --------------------------------------------------------------------------


class TestBuildFailureSummary:
    """The AC verify-line test ("category='subprocess_turn_limit',
    phase='ralph-reviewer (3)', exit_code=124 → 'ralph crashed at
    ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer
    exceeded max turns'") anchors this test class."""

    def test_ac_verify_line_subprocess_turn_limit(self, tmp_path: Path) -> None:
        """Reproduces the issue #2900 AC verify-line exactly."""
        d, conn, _handler = _make_daemon(tmp_path)
        # The helper SELECTs the latest failure + latest phase_outputs log_text.
        # Return the failure row first (category, details), then the
        # log_text row for the same agent.
        conn.cursor_instance.fetch_queue = [
            # First SELECT: (category, details_json)
            ("subprocess_turn_limit", {"stderr_tail": "reviewer exceeded max turns"}),
            # Second SELECT: (log_text,)
            ("=== stderr ===\nsome noise\nreviewer exceeded max turns\n",),
        ]
        summary = d._build_failure_summary(
            agent_id="agent-xyz",
            status="crashed",
            phase="ralph-reviewer (3)",
            exit_code=124,
        )
        assert summary is not None
        # Shape: "ralph crashed at ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer exceeded max turns"
        assert "ralph crashed at ralph-reviewer iteration 3" in summary
        assert "(subprocess_turn_limit)" in summary
        assert "reviewer exceeded max turns" in summary
        assert len(summary) <= 240

    def test_caps_at_240_chars(self, tmp_path: Path) -> None:
        """Long stderr tails are truncated to fit the column."""
        d, conn, _handler = _make_daemon(tmp_path)
        long_tail = "x" * 500
        conn.cursor_instance.fetch_queue = [
            ("subprocess_crash", {}),
            (f"=== stderr ===\n{long_tail}\n",),
        ]
        summary = d._build_failure_summary(
            agent_id="agent-long",
            status="failed",
            phase="ralph-worker (1)",
            exit_code=1,
        )
        assert summary is not None
        assert len(summary) <= 240

    def test_plan_blocked_phrasing(self, tmp_path: Path) -> None:
        """``status='plan_blocked'`` gets a distinct verb phrase — the
        plan correctly declined to proceed, not a genuine crash."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (
                "plan_go_false",
                {"block_reason": "scope is ambiguous — missing data model"},
            ),
            (None,),  # no phase_outputs log_text
        ]
        summary = d._build_failure_summary(
            agent_id="agent-pb",
            status="plan_blocked",
            phase="plan",
            exit_code=0,
        )
        assert summary is not None
        # Distinct from "crashed" — "plan phase returned go=false" is the
        # expected prefix per the issue's examples list.
        assert "plan" in summary.lower()
        assert "go=false" in summary or "plan_blocked" in summary
        # The block_reason from details is surfaced.
        assert "scope is ambiguous" in summary
        assert len(summary) <= 240

    def test_ci_red_after_retries(self, tmp_path: Path) -> None:
        """``ci_red_after_retries`` is a distinct category; the
        phase-group ``push_and_pr`` is the verb phrase source.

        Matches issue example:
          "push_and_pr failed (ci_red_after_retries): pre-push test regression in packages/scraper-framework/tests/"
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (
                "ci_red_after_retries",
                {
                    "detail": (
                        "pre-push test regression in packages/scraper-framework/tests/"
                    )
                },
            ),
            (None,),
        ]
        summary = d._build_failure_summary(
            agent_id="agent-ci",
            status="failed",
            phase="push_and_pr",
            exit_code=1,
        )
        assert summary is not None
        assert "push_and_pr" in summary
        assert "ci_red_after_retries" in summary
        # Detail text surfaces.
        assert "pre-push" in summary

    def test_missing_failure_row_builds_from_phase_alone(self, tmp_path: Path) -> None:
        """When no ``dispatcher.failures`` row exists for the agent (rare
        — e.g. plan_blocked without a failure row), the summary falls
        back to phase + status + exit_code without the category clause."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            None,  # no failure row
            None,  # no phase_outputs row
        ]
        summary = d._build_failure_summary(
            agent_id="agent-none",
            status="failed",
            phase="unknown",
            exit_code=137,
        )
        # A best-effort summary is still produced — the column exists to
        # carry SOMETHING for every failed/crashed/plan_blocked row.
        assert summary is not None
        # At minimum the phase + exit_code surface so triage has a toehold.
        assert "unknown" in summary or "137" in summary
        assert len(summary) <= 240

    def test_db_error_returns_none(self, tmp_path: Path) -> None:
        """A fetch failure must not break the terminal path — helper
        returns None and the caller writes NULL to the column."""
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(_sql: str, _params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        summary = d._build_failure_summary(
            agent_id="agent-db-down",
            status="failed",
            phase="ralph",
            exit_code=1,
        )
        assert summary is None

    def test_succeeded_status_returns_none(self, tmp_path: Path) -> None:
        """Non-terminal-failure statuses don't produce summaries — the
        column stays NULL for succeeded rows."""
        d, conn, _handler = _make_daemon(tmp_path)
        summary = d._build_failure_summary(
            agent_id="agent-ok",
            status="succeeded",
            phase="done",
            exit_code=0,
        )
        assert summary is None

    def test_needs_review_returns_none(self, tmp_path: Path) -> None:
        """``needs_review`` is a correct-outcome terminal — the draft
        PR IS the summary signal, not a failure string."""
        d, conn, _handler = _make_daemon(tmp_path)
        summary = d._build_failure_summary(
            agent_id="agent-nr",
            status="needs_review",
            phase="needs_review",
            exit_code=None,
        )
        assert summary is None


# --------------------------------------------------------------------------
# _mark_agent_terminal integration — writes failure_summary on failure
# terminals and leaves it NULL on success terminals
# --------------------------------------------------------------------------


class TestMarkAgentTerminalWritesFailureSummary:
    def test_failed_terminal_writes_failure_summary(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Pre-seed the fetch_queue for the _build_failure_summary helper.
        conn.cursor_instance.fetch_queue = [
            ("subprocess_turn_limit", {"stderr_tail": "reviewer exceeded max turns"}),
            ("=== stderr ===\nreviewer exceeded max turns\n",),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-failed",
            status="failed",
            phase="ralph-reviewer (3)",
            exit_code=124,
        )

        # Search for the failure_summary UPDATE — distinct from the
        # initial terminal UPDATE which sets status/phase/ended_at.
        summary_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "failure_summary" in e[0]
        ]
        assert summary_updates, (
            f"Expected failure_summary UPDATE, got executes: "
            f"{[e[0][:100] for e in conn.cursor_instance.executed]}"
        )
        sql, params = summary_updates[0]
        # params: (summary_text, agent_id)
        assert params[1] == "agent-failed"
        summary_text = params[0]
        assert isinstance(summary_text, str)
        assert "subprocess_turn_limit" in summary_text
        assert len(summary_text) <= 240

    def test_crashed_terminal_writes_failure_summary(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            ("stuck_timeout", {"stuck_seconds": 5400}),
            (None,),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-crashed",
            status="crashed",
            phase="ralph-worker (2)",
            exit_code=None,
        )

        summary_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "failure_summary" in e[0]
        ]
        assert summary_updates
        assert "stuck_timeout" in summary_updates[0][1][0]

    def test_plan_blocked_terminal_writes_failure_summary(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            ("plan_go_false", {"block_reason": "scope is ambiguous"}),
            (None,),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-pb", status="plan_blocked", phase="plan", exit_code=0
        )

        summary_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "failure_summary" in e[0]
        ]
        assert summary_updates
        assert "scope is ambiguous" in summary_updates[0][1][0]

    def test_succeeded_terminal_skips_failure_summary_write(
        self, tmp_path: Path
    ) -> None:
        """Successful agents leave ``failure_summary`` NULL — the helper
        returns None and the caller skips the templated write entirely.

        Post-#2913 the initial terminal UPDATE DOES include
        ``failure_summary = NULL`` inline (atomic clear on the
        crashed → retry_reset → succeeded retry-recovery path), so we
        gate on the parameterized ``SET failure_summary = %s`` shape
        the write-path emits — that one must not fire."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-ok", status="succeeded", phase="cleanup_done", exit_code=0
        )

        summary_writes = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "SET failure_summary = %s" in e[0]
        ]
        assert not summary_writes

    def test_needs_review_terminal_skips_failure_summary_write(
        self, tmp_path: Path
    ) -> None:
        """``needs_review`` is a correct-outcome terminal — no
        templated summary. Same guard shape as ``succeeded`` (see
        docstring): the initial UPDATE carries ``failure_summary = NULL``
        inline post-#2913, so we gate on the parameterized write shape."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-nr",
            status="needs_review",
            phase="needs_review",
            exit_code=None,
            pr_number=9001,
        )

        summary_writes = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "SET failure_summary = %s" in e[0]
        ]
        assert not summary_writes

    def test_failure_summary_write_failure_does_not_roll_back_terminal(
        self, tmp_path: Path
    ) -> None:
        """A DB hiccup writing failure_summary must not roll back the
        authoritative terminal-status update. Mirrors the diagnoser
        write-back's best-effort contract in the same method."""
        d, conn, handler = _make_daemon(tmp_path)
        # Feed enough fetch results so _build_failure_summary can at least try.
        conn.cursor_instance.fetch_queue = [
            ("subprocess_crash", {}),
            (None,),
        ]

        original_execute = conn.cursor_instance.execute

        def patched(sql: str, params: Any = None) -> None:
            if "UPDATE dispatcher.agents" in sql and "failure_summary" in sql:
                raise RuntimeError("db lost mid-summary-write")
            original_execute(sql, params)

        conn.cursor_instance.execute = patched  # type: ignore[method-assign]
        conn.cursor_instance.rowcount = 1

        # Must not raise.
        d._mark_agent_terminal(
            "agent-hiccup",
            status="failed",
            phase="ralph-reviewer (1)",
            exit_code=1,
        )
        # The terminal UPDATE still ran.
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert terminal_updates, "Terminal UPDATE must have committed"
        # Failure event logged.
        assert handler.events("failure_summary_write_failed")


# --------------------------------------------------------------------------
# Issue #2913: clear failure_summary on crashed → succeeded retry recovery.
#
# When an agent transitions through ``crashed → retrying → succeeded`` via
# the tier-1 retry path (``retry_reset`` phase transition), the
# ``failure_summary`` populated during the ``crashed`` terminal must be
# cleared when the status flips to ``succeeded`` (or any other correct-
# outcome terminal). Otherwise the admin cockpit renders the ✓ glyph with
# the old crash message as its tooltip — visually confusing.
#
# The fix is atomic with the status transition: include
# ``failure_summary = NULL`` in the same UPDATE SQL that sets
# status/phase/ended_at for correct-outcome terminals (``succeeded`` and
# ``needs_review``). Failure terminals (``failed``/``crashed``/
# ``plan_blocked``) continue to populate the column via the existing
# ``_build_failure_summary`` + ``_write_failure_summary`` path.
# --------------------------------------------------------------------------


class TestClearFailureSummaryOnSuccessTerminal:
    """Issue #2913 — the terminal UPDATE for correct-outcome statuses
    must explicitly NULL the ``failure_summary`` column so a row that
    previously held a crash message from an earlier terminal is cleaned
    up when the retry-recovery path flips it to ``succeeded``."""

    def test_succeeded_terminal_clears_failure_summary_in_same_update(
        self, tmp_path: Path
    ) -> None:
        """The terminal UPDATE must include ``failure_summary = NULL`` so
        the write is atomic with the status transition — no second
        round-trip, no window where the admin cockpit can read a
        ``succeeded`` row with a stale ``failure_summary``."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-recovered",
            status="succeeded",
            phase="verify",
            exit_code=0,
            pr_number=9999,
            issue_number=2908,
        )

        # Find the initial terminal UPDATE (the one with ended_at = now()).
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert terminal_updates, "Terminal UPDATE must have been executed"
        sql, _params = terminal_updates[0]
        # The clearing is part of the SAME statement — not a follow-up UPDATE.
        assert "failure_summary = NULL" in sql, (
            f"Terminal UPDATE for succeeded must clear failure_summary; got SQL: {sql}"
        )

    def test_needs_review_terminal_clears_failure_summary_in_same_update(
        self, tmp_path: Path
    ) -> None:
        """``needs_review`` is also a correct-outcome terminal — the draft
        PR is the signal, not a failure string. Same atomic-clear
        treatment as ``succeeded``."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-needs-review",
            status="needs_review",
            phase="needs_review",
            exit_code=None,
            pr_number=8888,
        )

        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert terminal_updates
        sql, _params = terminal_updates[0]
        assert "failure_summary = NULL" in sql

    def test_failed_terminal_does_not_include_null_clear_in_update(
        self, tmp_path: Path
    ) -> None:
        """Failure terminals keep the existing code path — the terminal
        UPDATE does NOT set ``failure_summary = NULL`` (the follow-up
        ``_write_failure_summary`` call populates the column with the
        templated one-liner). Guards against accidentally clobbering
        the new failure_summary write on the failure path."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Pre-seed the fetch queue so _build_failure_summary can run.
        conn.cursor_instance.fetch_queue = [
            ("subprocess_turn_limit", {"stderr_tail": "reviewer exceeded max turns"}),
            ("=== stderr ===\nreviewer exceeded max turns\n",),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-failed",
            status="failed",
            phase="ralph-reviewer (3)",
            exit_code=124,
        )

        # Initial terminal UPDATE (sets ended_at) must NOT carry the clear.
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert terminal_updates
        sql, _params = terminal_updates[0]
        assert "failure_summary = NULL" not in sql, (
            "Failure terminals must leave the initial UPDATE alone so "
            "the follow-up _write_failure_summary populates the column."
        )
        # Meanwhile the follow-up write-path UPDATE still ran.
        summary_writes = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "SET failure_summary = %s" in e[0]
        ]
        assert summary_writes, (
            "Failed terminals must still write the templated summary."
        )

    def test_crashed_terminal_does_not_include_null_clear_in_update(
        self, tmp_path: Path
    ) -> None:
        """Same guard for the ``crashed`` status — this is the terminal
        that populates ``failure_summary`` in the first place; the
        crashed → retry_reset → succeeded handoff is what the clear-
        on-success path (see ``test_crashed_then_succeeded_clears``)
        protects against."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            ("stuck_timeout", {"stuck_seconds": 5400}),
            (None,),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-crashed",
            status="crashed",
            phase="ralph-worker (2)",
            exit_code=None,
        )

        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert terminal_updates
        sql, _params = terminal_updates[0]
        assert "failure_summary = NULL" not in sql

    def test_plan_blocked_terminal_does_not_include_null_clear_in_update(
        self, tmp_path: Path
    ) -> None:
        """``plan_blocked`` is a correct-outcome terminal by intent, but
        it DOES populate ``failure_summary`` (operators still want to
        know WHY the plan declined). Same guard as ``failed``/
        ``crashed`` — the initial UPDATE leaves the column alone so
        the follow-up write populates it."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            ("plan_go_false", {"block_reason": "scope is ambiguous"}),
            (None,),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-pb", status="plan_blocked", phase="plan", exit_code=0
        )

        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert terminal_updates
        sql, _params = terminal_updates[0]
        assert "failure_summary = NULL" not in sql

    def test_non_terminal_phase_update_does_not_touch_failure_summary(
        self, tmp_path: Path
    ) -> None:
        """The non-terminal code path (e.g. Phase 3A post-PR hand-off
        where status='running', phase='awaiting_ci') must leave the
        column alone — no NULL clear, no write. Otherwise a running
        agent's in-progress handoff would blow away a legitimate
        summary from an earlier failed iteration."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-running",
            status="running",
            phase="awaiting_ci",
            exit_code=None,
            pr_number=7777,
        )

        # The non-terminal UPDATE path does not set ended_at — find it
        # by the phase-only UPDATE shape.
        non_terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "SET status = %s, phase = %s" in e[0]
            and "ended_at" not in e[0]
        ]
        assert non_terminal_updates, "Non-terminal UPDATE must have run"
        sql, _params = non_terminal_updates[0]
        assert "failure_summary" not in sql, (
            "Non-terminal UPDATE must not touch failure_summary — "
            "otherwise it would clobber summaries from earlier failures."
        )

    def test_crashed_then_succeeded_recovery_clears_failure_summary(
        self, tmp_path: Path
    ) -> None:
        """End-to-end reproduction of the issue #2913 scenario: agent
        hits ``crashed`` (populates failure_summary via the templated
        write-path), then the retry-recovery path later flips the same
        row to ``succeeded``. After the second call, the initial
        UPDATE has NULLed the column.

        This is the AC verify-line test — "a unit test reproduces the
        crashed → retry_reset → succeeded flow and asserts the final
        failure_summary is NULL."
        """
        d, conn, _handler = _make_daemon(tmp_path)
        # First transition: crashed — populates failure_summary.
        conn.cursor_instance.fetch_queue = [
            ("stuck_timeout", {"stuck_seconds": 5400}),
            (None,),
        ]
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-retry-recovery",
            status="crashed",
            phase="daemon_restart_abandoned",
            exit_code=None,
        )

        # Confirm the crashed path populated the column.
        first_terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert len(first_terminal_updates) == 1
        summary_writes_after_crash = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "SET failure_summary = %s" in e[0]
        ]
        assert summary_writes_after_crash, (
            "Crashed terminal should have written a failure_summary"
        )

        # Second transition: retry-recovery flips the same row to succeeded.
        d._mark_agent_terminal(
            "agent-retry-recovery",
            status="succeeded",
            phase="verify",
            exit_code=0,
            pr_number=2908,
        )

        # The second initial UPDATE must carry the NULL clear inline.
        second_terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "ended_at = now()" in e[0]
        ]
        assert len(second_terminal_updates) == 2, (
            "Both terminals should have executed a terminal UPDATE"
        )
        second_sql, _second_params = second_terminal_updates[1]
        assert "failure_summary = NULL" in second_sql, (
            "Retry-recovery crashed→succeeded MUST clear the old "
            f"failure_summary in the same UPDATE; got SQL: {second_sql}"
        )
