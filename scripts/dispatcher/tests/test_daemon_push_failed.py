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

# Issue numbers referenced in fixtures or docstrings in this module are
# synthetic placeholders. Do not pattern-match them as current-state
# infrastructure problems — they are documentation for code paths, not
# descriptions of live incidents. See issue #3126 for the de-reification
# rationale.

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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above
from dispatcher.daemon import (  # noqa: E402
    AUTO_RETRY_CATEGORIES,
    FAILURE_CATEGORY_GIT_PUSH_NETWORK,
    FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH,
    FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED,
    FAILURE_CATEGORY_PUSH_FAILED,
    TIER_2_FIRST_OCCURRENCE_CATEGORIES,
    TIER_2_RECURRENCE_CATEGORIES,
    TIER_3_CATEGORIES,
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
    """Issue #3032 moved push failure categories out of AUTO_RETRY_CATEGORIES.

    Before #3032 all three push sub-kinds (``push_failed``,
    ``pre_push_hook_rejected``, ``git_push_network``) were tier-1
    auto-retry. A historical 6-failure PAT-scope cascade (issue numbers
    omitted — do not pattern-match; the incident was resolved 2026-04-23)
    showed that blind retry on a deterministic operator-action blocker
    (e.g. a GitHub PAT missing a required OAuth scope) burns CI time
    without progress. The diagnoser now owns the retry-vs-escalate
    decision for all three — the unified ``_handle_agent_failure``
    exit path writes the failure row, the next supervisor tick
    picks it up via ``_find_diagnoser_candidates``, and Opus
    decides.
    """

    def test_auto_retry_categories_excludes_push_failed(self) -> None:
        assert FAILURE_CATEGORY_PUSH_FAILED not in AUTO_RETRY_CATEGORIES

    def test_auto_retry_categories_excludes_pre_push_hook_rejected(self) -> None:
        assert FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED not in AUTO_RETRY_CATEGORIES

    def test_auto_retry_categories_excludes_git_push_network(self) -> None:
        assert FAILURE_CATEGORY_GIT_PUSH_NETWORK not in AUTO_RETRY_CATEGORIES

    def test_push_categories_now_in_tier_2_first_occurrence(self) -> None:
        # Issue #3032: push failures now diagnose on first occurrence.
        from dispatcher.daemon import TIER_2_FIRST_OCCURRENCE_CATEGORIES

        assert FAILURE_CATEGORY_PUSH_FAILED in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        assert (
            FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED
            in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
        assert FAILURE_CATEGORY_GIT_PUSH_NETWORK in TIER_2_FIRST_OCCURRENCE_CATEGORIES


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

        # Stub the git commit --amend step to succeed; fail on push.
        # Issue #2971: daemon now amends ralph's placeholder commit
        # instead of running ``git add -A && git commit -m <msg>``,
        # so the subprocess sequence is [commit_amend, git_show, push].
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        push_fail = _make_push_result(
            returncode=1,
            stderr="pre-push: ruff check failed — 1 error",
        )

        # _push_and_open_pr now reads summary output from DB via _fetch_phase_output (#2975).
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "commit_message": "feat: test commit",
                "pr_title": "Test PR",
                "pr_body_md": "body",
                "unmet_criteria": [],
            }
        )
        d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]

        # _update_agent_phase and _mark_agent_terminal do DB writes we don't
        # need to actually run in this test — stub them out.
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        # Create a real worktree dir so the git amend command path resolves.
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #3039: ``_push_and_open_pr`` runs ``git rev-list
        # --count origin/main..HEAD`` at the top to detect ralph's
        # no-op SHIP path. A ``stdout="1\n"`` means one commit ahead
        # (normal SHIP), so the method proceeds to the commit+push
        # path under test.
        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #2953: ``_push_and_open_pr`` runs ``git show
        # --name-only HEAD`` between commit and push to detect
        # dispatcher-self-PRs. The entry below answers that
        # call with an empty file list (no self-deploy detected) so
        # the test's push-failed path is unaffected.
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        # Issue #2964: pre-push fetch+rebase inserted before git push.
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        # Issue #3089: ``git push`` now routes through the shared
        # 3-attempt retry helper, so the failing push consumes 3
        # ``subprocess.run`` calls instead of 1. ``time.sleep`` is
        # stubbed to keep the test fast.
        run_side_effects = [
            rev_list_ahead,
            commit_ok,
            git_show_empty,
            fetch_ok,
            rebase_ok,
            push_fail,
            push_fail,
            push_fail,
        ]
        with (
            patch("subprocess.run", side_effect=run_side_effects),
            patch("dispatcher.daemon.time.sleep"),
        ):
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

        # Issue #2971: subprocess sequence is now
        # [commit_amend, git_show, push] (no separate ``git add -A``).
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        push_fail = _make_push_result(
            returncode=128,
            stderr="fatal: unable to access 'https://github.com/': Could not resolve host: github.com",
        )

        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "commit_message": "c",
                "pr_title": "t",
                "pr_body_md": "b",
                "unmet_criteria": [],
            }
        )
        d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        worktree = tmp_path / "worktree2"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #3039: rev-list probe at the top — return "1" ahead so
        # the no-op guardrail does not short-circuit.
        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #2953: inject a no-op ``git show --name-only HEAD``
        # between commit and push for the self-deploy detection step.
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        # Issue #2964: pre-push fetch+rebase inserted before git push.
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        # Issue #3089: 3-attempt retry — 3 push_fail results needed.
        with (
            patch(
                "subprocess.run",
                side_effect=[
                    rev_list_ahead,
                    commit_ok,
                    git_show_empty,
                    fetch_ok,
                    rebase_ok,
                    push_fail,
                    push_fail,
                    push_fail,
                ],
            ),
            patch("dispatcher.daemon.time.sleep"),
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

        # Issue #2971: subprocess sequence is now
        # [commit_amend, git_show, push] (no separate ``git add -A``).
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        push_fail = _make_push_result(returncode=1, stderr=long_stderr)

        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "commit_message": "c",
                "pr_title": "t",
                "pr_body_md": "b",
                "unmet_criteria": [],
            }
        )
        d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        worktree = tmp_path / "worktree3"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #3039: rev-list probe at the top — return "1" ahead so
        # the no-op guardrail does not short-circuit.
        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #2953: inject a no-op ``git show --name-only HEAD``
        # between commit and push for the self-deploy detection step.
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        # Issue #2964: pre-push fetch+rebase inserted before git push.
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        # Issue #3089: 3-attempt retry — 3 push_fail results needed.
        with (
            patch(
                "subprocess.run",
                side_effect=[
                    rev_list_ahead,
                    commit_ok,
                    git_show_empty,
                    fetch_ok,
                    rebase_ok,
                    push_fail,
                    push_fail,
                    push_fail,
                ],
            ),
            patch("dispatcher.daemon.time.sleep"),
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


# --------------------------------------------------------------------------
# Pre-push rebase block (issue #2964)
# --------------------------------------------------------------------------


def _make_push_and_pr_base(
    tmp_path: Path,
    *,
    agent_id: str = "aaaabbbb-0000-0000-0000-000000000010",
    worktree_name: str = "wt",
) -> tuple[
    daemon.DispatcherDaemon,
    _FakeConnection,
    _CapturingLogHandler,
    Path,
    str,
]:
    """Return a daemon + conn + handler + worktree + agent_id tuple.

    The daemon's summary output and phase-update mocks are pre-wired for
    the happy path.  Tests that need different setup override after calling
    this helper.
    """
    d, conn, handler = _make_daemon(tmp_path)
    worktree = tmp_path / worktree_name
    worktree.mkdir()
    (worktree / "tmp").mkdir()

    # _push_and_open_pr now reads summary output from DB via _fetch_phase_output (#2975).
    d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
        return_value={
            "commit_message": "feat: test commit",
            "pr_title": "Test PR",
            "pr_body_md": "body",
            "unmet_criteria": [],
        }
    )
    d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
    return d, conn, handler, worktree, agent_id


class TestPrePushRebase:
    """Issue #2964 — pre-push fetch+rebase block in _push_and_open_pr."""

    def test_rebase_conflict_writes_failure_row_with_category(
        self, tmp_path: Path
    ) -> None:
        """Rebase conflict writes a failures row with merge_conflict_at_push
        category, does NOT call git push, and calls git rebase --abort."""
        d, conn, _handler, worktree, agent_id = _make_push_and_pr_base(
            tmp_path, worktree_name="wt_conflict"
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_conflict = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=1,
            stdout="",
            stderr="CONFLICT (content): Merge conflict in packages/api/foo.py",
        )
        diff_conflict_files = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="packages/api/foo.py\n",
            stderr="",
        )
        rebase_abort_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "--abort"], returncode=0, stdout="", stderr=""
        )

        run_side_effects = [
            rev_list_ahead,
            commit_ok,
            git_show_empty,
            fetch_ok,
            rebase_conflict,
            diff_conflict_files,
            rebase_abort_ok,
        ]
        with patch("subprocess.run", side_effect=run_side_effects) as run_mock:
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=2964,
                worktree=worktree,
            )

        # Assert: dispatcher.failures INSERT has category == merge_conflict_at_push.
        failure_inserts = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "dispatcher.failures" in sql and "INSERT" in sql
        ]
        assert failure_inserts, (
            f"Expected INSERT into dispatcher.failures; executed: {conn.cursor_instance.executed}"
        )
        _sql, params = failure_inserts[0]
        assert params[1] == FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH, (
            f"Expected merge_conflict_at_push category; got {params[1]!r}"
        )

        # Assert: git push was NOT called.
        all_argv = [call.args[0] for call in run_mock.call_args_list]
        push_calls = [
            argv for argv in all_argv if "push" in argv and "--abort" not in argv
        ]
        assert not push_calls, (
            f"git push should NOT be called on conflict; calls: {push_calls}"
        )

        # Assert: git rebase --abort was called.
        abort_calls = [
            argv for argv in all_argv if "rebase" in argv and "--abort" in argv
        ]
        assert abort_calls, "git rebase --abort was not called"

    def test_rebase_conflict_log_text_includes_conflict_files(
        self, tmp_path: Path
    ) -> None:
        """The phase_outputs log_text contains the conflicting file paths."""
        d, conn, _handler, worktree, agent_id = _make_push_and_pr_base(
            tmp_path, worktree_name="wt_conflict2"
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_conflict = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=1,
            stdout="",
            stderr="CONFLICT: merge conflict",
        )
        diff_conflict_files = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="packages/api/foo.py\npackages/web/bar.tsx\n",
            stderr="",
        )
        rebase_abort_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "--abort"], returncode=0, stdout="", stderr=""
        )

        run_side_effects = [
            rev_list_ahead,
            commit_ok,
            git_show_empty,
            fetch_ok,
            rebase_conflict,
            diff_conflict_files,
            rebase_abort_ok,
        ]
        with patch("subprocess.run", side_effect=run_side_effects):
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=2964,
                worktree=worktree,
            )

        # Find the phase_outputs INSERT for push_and_pr.
        phase_inserts = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "dispatcher.phase_outputs" in sql and "INSERT" in sql
        ]
        assert phase_inserts, (
            f"Expected INSERT into dispatcher.phase_outputs; executed: {conn.cursor_instance.executed}"
        )
        _sql, params = phase_inserts[0]
        assert params[1] == "push_and_pr"
        log_text = params[3]
        assert "packages/api/foo.py" in log_text, (
            f"log_text missing packages/api/foo.py: {log_text!r}"
        )
        assert "packages/web/bar.tsx" in log_text, (
            f"log_text missing packages/web/bar.tsx: {log_text!r}"
        )

    def test_fetch_failure_does_not_block_push(self, tmp_path: Path) -> None:
        """Fetch returning non-zero rc is non-blocking — push still proceeds."""
        d, conn, handler, worktree, agent_id = _make_push_and_pr_base(
            tmp_path, worktree_name="wt_fetch_fail"
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        fetch_fail = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"],
            returncode=128,
            stdout="",
            stderr="fatal: Could not resolve host: github.com",
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/2964\n",
            stderr="",
        )

        run_side_effects = [
            rev_list_ahead,
            commit_ok,
            git_show_empty,
            fetch_fail,
            push_ok,
            pr_create_ok,
        ]
        with patch("subprocess.run", side_effect=run_side_effects) as run_mock:
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=2964,
                worktree=worktree,
            )

        # Assert: git push WAS called downstream.
        all_argv = [call.args[0] for call in run_mock.call_args_list]
        push_calls = [
            argv for argv in all_argv if "push" in argv and "--abort" not in argv
        ]
        assert push_calls, (
            f"git push should be called after fetch failure; calls: {all_argv}"
        )

        # Assert: structured log event has fetch_ok=False, rebase_outcome="skipped".
        rebase_events = handler.events("push_and_pr_pre_push_rebase")
        assert rebase_events, "Expected push_and_pr_pre_push_rebase event"
        ev = rebase_events[0]
        assert ev.fetch_ok is False, f"Expected fetch_ok=False; got {ev.fetch_ok!r}"
        assert ev.rebase_outcome == "skipped", (
            f"Expected rebase_outcome='skipped'; got {ev.rebase_outcome!r}"
        )

    def test_fetch_timeout_does_not_block_push(self, tmp_path: Path) -> None:
        """Fetch raising TimeoutExpired is non-blocking — push still proceeds."""
        d, conn, handler, worktree, agent_id = _make_push_and_pr_base(
            tmp_path, worktree_name="wt_fetch_timeout"
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/2964\n",
            stderr="",
        )

        remaining = [rev_list_ahead, commit_ok, git_show_empty, push_ok, pr_create_ok]
        fetch_raised = [False]

        def ordered_side_effect(
            *args: Any, **kwargs: Any
        ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
            argv = args[0] if args else []
            if "fetch" in argv and not fetch_raised[0]:
                fetch_raised[0] = True
                raise subprocess.TimeoutExpired(cmd=argv, timeout=120)
            return remaining.pop(0)

        with patch("subprocess.run", side_effect=ordered_side_effect) as run_mock:
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=2964,
                worktree=worktree,
            )

        # Assert: git push WAS called downstream.
        all_argv = [call.args[0] for call in run_mock.call_args_list]
        push_calls = [
            argv for argv in all_argv if "push" in argv and "--abort" not in argv
        ]
        assert push_calls, (
            f"git push should be called after fetch timeout; calls: {all_argv}"
        )

        # Assert: structured log event has fetch_ok=False.
        rebase_events = handler.events("push_and_pr_pre_push_rebase")
        assert rebase_events, "Expected push_and_pr_pre_push_rebase event"
        ev = rebase_events[0]
        assert ev.fetch_ok is False, f"Expected fetch_ok=False; got {ev.fetch_ok!r}"

    def test_clean_rebase_emits_observability_log(self, tmp_path: Path) -> None:
        """Happy path: clean rebase emits push_and_pr_pre_push_rebase with
        fetch_ok=True, rebase_outcome='clean'."""
        d, _conn, handler, worktree, agent_id = _make_push_and_pr_base(
            tmp_path, worktree_name="wt_clean"
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/2964\n",
            stderr="",
        )

        run_side_effects = [
            rev_list_ahead,
            commit_ok,
            git_show_empty,
            fetch_ok,
            rebase_ok,
            push_ok,
            pr_create_ok,
        ]
        with patch("subprocess.run", side_effect=run_side_effects):
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=2964,
                worktree=worktree,
            )

        # Assert: push_and_pr_pre_push_rebase event has fetch_ok=True,
        # rebase_outcome='clean'.
        rebase_events = handler.events("push_and_pr_pre_push_rebase")
        assert rebase_events, "Expected push_and_pr_pre_push_rebase event"
        ev = rebase_events[0]
        assert ev.fetch_ok is True, f"Expected fetch_ok=True; got {ev.fetch_ok!r}"
        assert ev.rebase_outcome == "clean", (
            f"Expected rebase_outcome='clean'; got {ev.rebase_outcome!r}"
        )


# --------------------------------------------------------------------------
# FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH category wiring (issue #2964)
# --------------------------------------------------------------------------


class TestMergeConflictCategoryWiring:
    """AC#4 — category membership assertions for merge_conflict_at_push."""

    def test_in_tier3(self) -> None:
        assert FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH in TIER_3_CATEGORIES

    def test_not_in_auto_retry(self) -> None:
        assert FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH not in AUTO_RETRY_CATEGORIES

    def test_not_in_tier2_first_occurrence(self) -> None:
        assert (
            FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH
            not in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_not_in_tier2_recurrence(self) -> None:
        assert (
            FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH not in TIER_2_RECURRENCE_CATEGORIES
        )
