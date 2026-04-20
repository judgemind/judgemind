"""Unit tests for the ``git_push_failed`` path improvements (issue #2902).

Acceptance criteria covered:
- AC#2 — ``git_push_failed`` writes a ``dispatcher.failures`` row with a
  meaningful, classifier-derived category.
- AC#3 — ``git_push_failed`` writes a ``push_and_pr`` phase_outputs row
  with ``log_text`` = full stderr (no length cap).
- AC#4 — ``_build_failure_summary`` produces an informative tooltip for
  push_and_pr failures that includes category-in-parens and a colon-separated
  detail.

The classifier tests cover ``_classify_push_failure`` directly; the
failure-row and phase-output tests exercise the non-zero-returncode branch
of ``_push_and_open_pr`` by patching ``subprocess.run`` and asserting on the
cursor's executed SQL.

Fakes + psycopg MagicMock stub follow the pattern from
``test_daemon_failure_summary.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Provide a stub ``psycopg`` module before importing the daemon — same
# pattern as test_daemon_failure_summary.py.
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
from dispatcher.daemon import (  # noqa: E402
    AUTO_RETRY_CATEGORIES,
    FAILURE_CATEGORY_GIT_PUSH_NETWORK,
    FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED,
    FAILURE_CATEGORY_PUSH_FAILED,
    _classify_push_failure,
)


# --------------------------------------------------------------------------
# Shared fakes — same shape as test_daemon_failure_summary.py
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
    logger = logging.getLogger(f"dispatcher.test.push_failed.{id(tmp_path)}")
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
# _classify_push_failure
# --------------------------------------------------------------------------


class TestClassifyPushFailure:
    """AC#2 — classifier returns correct category for each stderr shape."""

    def test_classify_push_failure_pre_push_hook(self) -> None:
        """stderr with 'pre-push:' → pre_push_hook_rejected."""
        stderr = (
            "remote: Permission to foo/bar.git denied.\n"
            "pre-push: ruff check failed — 2 errors\n"
            "error: failed to push some refs"
        )
        assert _classify_push_failure(stderr) == FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED

    def test_classify_push_failure_network_could_not_resolve(self) -> None:
        """'could not resolve host' → git_push_network."""
        stderr = "fatal: unable to access 'https://github.com/': Could not resolve host: github.com"
        assert _classify_push_failure(stderr) == FAILURE_CATEGORY_GIT_PUSH_NETWORK

    def test_classify_push_failure_network_connection_refused(self) -> None:
        """'connection refused' → git_push_network."""
        stderr = "fatal: Connection refused to remote"
        assert _classify_push_failure(stderr) == FAILURE_CATEGORY_GIT_PUSH_NETWORK

    def test_classify_push_failure_network_url_error(self) -> None:
        """'The requested URL returned error' → git_push_network."""
        stderr = "error: The requested URL returned error: 503"
        assert _classify_push_failure(stderr) == FAILURE_CATEGORY_GIT_PUSH_NETWORK

    def test_classify_push_failure_fallback(self) -> None:
        """Unrecognised stderr → push_failed (catch-all)."""
        stderr = "error: remote rejected (branch protection is active)"
        assert _classify_push_failure(stderr) == FAILURE_CATEGORY_PUSH_FAILED

    def test_classify_push_failure_empty_stderr(self) -> None:
        """Empty stderr → push_failed (catch-all)."""
        assert _classify_push_failure("") == FAILURE_CATEGORY_PUSH_FAILED

    def test_classify_push_failure_case_insensitive(self) -> None:
        """Pattern matching is case-insensitive (e.g. 'PRE-PUSH:')."""
        assert (
            _classify_push_failure("PRE-PUSH: hook returned 1")
            == FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED
        )


# --------------------------------------------------------------------------
# AUTO_RETRY_CATEGORIES includes push failure kinds
# --------------------------------------------------------------------------


class TestAutoRetryCategories:
    """AC#2 — all three push failure categories are in AUTO_RETRY_CATEGORIES."""

    def test_auto_retry_categories_include_push_failed(self) -> None:
        assert FAILURE_CATEGORY_PUSH_FAILED in AUTO_RETRY_CATEGORIES

    def test_auto_retry_categories_include_pre_push_hook_rejected(self) -> None:
        assert FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED in AUTO_RETRY_CATEGORIES

    def test_auto_retry_categories_include_git_push_network(self) -> None:
        assert FAILURE_CATEGORY_GIT_PUSH_NETWORK in AUTO_RETRY_CATEGORIES


# --------------------------------------------------------------------------
# _push_and_open_pr — non-zero returncode branch
# --------------------------------------------------------------------------


def _make_push_result(
    returncode: int,
    stderr: str = "",
    stdout: str = "",
) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.CompletedProcess(
        args=["git", "push"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestGitPushFailedWritesFailureRow:
    """AC#2 — a ``dispatcher.failures`` row is written on push failure."""

    def test_git_push_failed_writes_failure_row(self, tmp_path: Path) -> None:
        """Non-zero ``git push`` exit writes a failures row with a classified
        category."""
        d, conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000001"

        # Stub the git add / commit steps to succeed; fail on push.
        add_ok = subprocess.CompletedProcess(
            args=["git", "add"], returncode=0, stdout="", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit"], returncode=0, stdout="", stderr=""
        )
        push_fail = _make_push_result(
            returncode=1,
            stderr="pre-push: ruff check failed — 1 error",
        )

        # _push_and_open_pr reads _agent_summary_output and _agent_unmet_criteria.
        d._agent_summary_output = {  # type: ignore[attr-defined]
            "commit_message": "feat: test commit",
            "pr_title": "Test PR",
            "pr_body_md": "body",
        }
        d._agent_unmet_criteria = []  # type: ignore[attr-defined]

        # _update_agent_phase and _mark_agent_terminal do DB writes we don't
        # need to actually run in this test — stub them out.
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        # Create a real worktree dir so the git add command path resolves.
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #2953: ``_push_and_open_pr`` now runs ``git show
        # --name-only HEAD`` between commit and push to detect
        # dispatcher-self-PRs. The fourth entry below answers that
        # call with an empty file list (no self-deploy detected) so
        # the test's push-failed path is unaffected.
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        run_side_effects = [add_ok, commit_ok, git_show_empty, push_fail]
        with patch("subprocess.run", side_effect=run_side_effects):
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=42,
                worktree=worktree,
            )

        # Find the INSERT into dispatcher.failures.
        failure_inserts = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "dispatcher.failures" in sql and "INSERT" in sql
        ]
        assert len(failure_inserts) >= 1, (
            "Expected at least one INSERT into dispatcher.failures; "
            f"executed: {conn.cursor_instance.executed}"
        )
        _sql, params = failure_inserts[0]
        # params: (agent_id, category, detected_by, details_json)
        assert params[0] == agent_id
        # Category must be the classifier-derived value, not a generic stub.
        assert params[1] == FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED, (
            f"Expected pre_push_hook_rejected from classifier; got {params[1]!r}"
        )
        details = json.loads(params[3])
        assert details["phase"] == "push_and_pr"
        assert "pre-push" in details["stderr_tail"]

    def test_git_push_failed_category_for_network_error(self, tmp_path: Path) -> None:
        """Network-layer push failure is classified as git_push_network."""
        d, conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000002"

        add_ok = subprocess.CompletedProcess(
            args=["git", "add"], returncode=0, stdout="", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit"], returncode=0, stdout="", stderr=""
        )
        push_fail = _make_push_result(
            returncode=128,
            stderr="fatal: unable to access 'https://github.com/': Could not resolve host: github.com",
        )

        d._agent_summary_output = {
            "commit_message": "c",
            "pr_title": "t",
            "pr_body_md": "b",
        }  # type: ignore[attr-defined]
        d._agent_unmet_criteria = []  # type: ignore[attr-defined]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        worktree = tmp_path / "worktree2"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #2953: inject a no-op ``git show --name-only HEAD``
        # between commit and push for the self-deploy detection step.
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        with patch(
            "subprocess.run",
            side_effect=[add_ok, commit_ok, git_show_empty, push_fail],
        ):
            d._push_and_open_pr(agent_id=agent_id, issue_number=99, worktree=worktree)

        failure_inserts = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "dispatcher.failures" in sql and "INSERT" in sql
        ]
        assert failure_inserts, "Expected INSERT into dispatcher.failures"
        _sql, params = failure_inserts[0]
        assert params[1] == FAILURE_CATEGORY_GIT_PUSH_NETWORK


# --------------------------------------------------------------------------
# _push_and_open_pr — phase_outputs row
# --------------------------------------------------------------------------


class TestGitPushFailedWritesPhaseOutputRow:
    """AC#3 — a ``dispatcher.phase_outputs`` row is written with full stderr
    as ``log_text`` (no length cap) on push failure."""

    def test_git_push_failed_writes_phase_output_row(self, tmp_path: Path) -> None:
        """After a push failure, phase_outputs row exists for push_and_pr."""
        d, conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000003"
        # Use a long stderr to verify it is NOT truncated.
        long_stderr = "pre-push: " + "x" * 5000

        add_ok = subprocess.CompletedProcess(
            args=["git", "add"], returncode=0, stdout="", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit"], returncode=0, stdout="", stderr=""
        )
        push_fail = _make_push_result(returncode=1, stderr=long_stderr)

        d._agent_summary_output = {
            "commit_message": "c",
            "pr_title": "t",
            "pr_body_md": "b",
        }  # type: ignore[attr-defined]
        d._agent_unmet_criteria = []  # type: ignore[attr-defined]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        worktree = tmp_path / "worktree3"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #2953: inject a no-op ``git show --name-only HEAD``
        # between commit and push for the self-deploy detection step.
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        with patch(
            "subprocess.run",
            side_effect=[add_ok, commit_ok, git_show_empty, push_fail],
        ):
            d._push_and_open_pr(agent_id=agent_id, issue_number=7, worktree=worktree)

        # Find the INSERT into dispatcher.phase_outputs for push_and_pr.
        phase_inserts = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "dispatcher.phase_outputs" in sql and "INSERT" in sql
        ]
        assert phase_inserts, (
            "Expected INSERT into dispatcher.phase_outputs; "
            f"executed: {conn.cursor_instance.executed}"
        )
        # Params order: (agent_id, phase, output_json, log_text, attempt, ...)
        _sql, params = phase_inserts[0]
        assert params[0] == agent_id
        assert params[1] == "push_and_pr"
        # log_text must be the full stderr — not truncated to 200 or 4000 chars.
        log_text = params[3]
        assert log_text is not None
        assert len(log_text) == len(long_stderr), (
            f"log_text truncated: got {len(log_text)} chars, expected {len(long_stderr)}"
        )
        assert log_text == long_stderr


# --------------------------------------------------------------------------
# _build_failure_summary — push_and_pr tooltip shape (AC#4)
# --------------------------------------------------------------------------


class TestGitPushFailedSummaryTooltip:
    """AC#4 — tooltip for push_and_pr failures includes category and detail."""

    def test_git_push_failed_summary_contains_category_and_detail(
        self, tmp_path: Path
    ) -> None:
        """``_build_failure_summary`` with a push_failed category + stderr
        produces a summary that contains category-in-parens AND colon-separated
        detail. No bare 'push_and_pr failed' with no detail.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            # First SELECT: (category, details_json)
            (
                "pre_push_hook_rejected",
                {
                    "stderr_tail": (
                        "pre-push: ruff check failed — 1 error\n"
                        "error: failed to push some refs to 'origin'"
                    )
                },
            ),
            # Second SELECT: (log_text,)
            (
                "pre-push: ruff check failed — 1 error\n"
                "error: failed to push some refs to 'origin'\n",
            ),
        ]
        summary = d._build_failure_summary(
            agent_id="agent-push-fail",
            status="failed",
            phase="push_and_pr",
            exit_code=1,
        )
        assert summary is not None, (
            "Expected a non-None summary for push_and_pr failure"
        )
        # Must include parenthesised category (human-friendly display name).
        assert "(pre-push hook rejected)" in summary, (
            f"Summary missing category paren: {summary!r}"
        )
        # Must include some detail from stderr — not just the bare phase name.
        assert (
            "pre-push" in summary or "ruff" in summary or "push" in summary.lower()
        ), f"Summary has no detail from stderr: {summary!r}"
        assert len(summary) <= 240, f"Summary exceeds 240-char limit: {len(summary)}"

    def test_push_failed_renders_humanized(self, tmp_path: Path) -> None:
        """``push_failed`` category maps to 'git push failed' display name."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (
                "push_failed",
                {"stderr_tail": "remote: Repository not found."},
            ),
            ("remote: Repository not found.\n",),
        ]
        summary = d._build_failure_summary(
            agent_id="agent-push-failed",
            status="failed",
            phase="push_and_pr",
            exit_code=128,
        )
        assert summary is not None
        # Display name must appear, not the raw token.
        assert "(git push failed)" in summary, f"Unexpected summary: {summary!r}"
        assert "push_failed" not in summary, (
            f"Raw category token leaked into summary: {summary!r}"
        )
