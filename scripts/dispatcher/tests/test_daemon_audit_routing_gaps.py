"""Unit tests for the five audit-gap routing fixes (#3067-#3071).

Covers the five `_mark_agent_terminal(status="failed")` call sites
identified by the #3062 audit that bypassed the #3032 unified failure
path:

* **#3067** — pre-push ``git commit --amend`` exception / non-zero exit
  in :meth:`DispatcherDaemon._push_and_open_pr` routes through
  :meth:`_handle_agent_failure` with
  :data:`FAILURE_CATEGORY_GIT_COMMIT_FAILED` (tier-2 first-occurrence).

* **#3068** — ``/task-v2-fix-ci`` ``verdict='BLOCKED'`` in
  :meth:`_run_fix_ci` routes with
  :data:`FAILURE_CATEGORY_FIX_CI_BLOCKED` (tier-3).

* **#3069** — the eight terminal sites inside
  :meth:`_apply_fix_ci_patch` (missing commit message + every git-add /
  git-commit / git-push failure mode) route with
  :data:`FAILURE_CATEGORY_FIX_CI_APPLY_FAILED` (tier-2 first-
  occurrence), each carrying a distinct ``sub_reason`` in ``details``.

* **#3070** — post-merge deploy workflow failure in
  :meth:`_advance_awaiting_deploy` routes with
  :data:`FAILURE_CATEGORY_DEPLOY_FAILED` (tier-2 first-occurrence).

* **#3071** — post-merge ``/task-v2-verify`` ``verdict='FAILED'`` in
  :meth:`_run_verify_and_complete` routes with
  :data:`FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE` (tier-3).

Each test class asserts:

* Tier-set membership of the new category (AC #1 for each issue).
* Call-site routes through ``_handle_agent_failure`` with the right
  category + details shape (AC #2 for each issue).
* The failure row is lifted by :meth:`_find_diagnoser_candidates` with
  the expected ``tier`` (AC #3 — mirrors the ralph_not_ship
  selection-helper coverage).

Mirrors ``test_daemon_ralph_not_ship.py`` /
``test_daemon_summary_output_incomplete.py`` style — same helper shape.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes
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
    tmp_path: Path, *, scope: str
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.audit_routing.{scope}.{id(tmp_path)}")
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


def _make_failure_row(
    *,
    failure_id: int,
    agent_id: str,
    category: str,
    details: dict[str, Any],
) -> tuple[int, str, str, dict[str, Any], datetime]:
    return (
        failure_id,
        agent_id,
        category,
        details,
        datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC),
    )


# ==========================================================================
# #3067 — pre-push git_commit_failed routes through _handle_agent_failure
# ==========================================================================


class TestGitCommitFailedTierRegistration:
    def test_git_commit_failed_is_tier_2_first_occurrence(self) -> None:
        """``git_commit_failed`` diagnoses on first occurrence — the
        worktree still holds ralph's changes so the diagnoser can pick
        retry vs escalate based on stderr.
        """
        assert (
            daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED
            in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_git_commit_failed_not_in_auto_retry(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED
            not in daemon.AUTO_RETRY_CATEGORIES
        )

    def test_git_commit_failed_not_in_tier3(self) -> None:
        """Tier buckets must be disjoint — the candidate scanner's
        tier assignment branches on exclusive membership."""
        assert daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED not in daemon.TIER_3_CATEGORIES


class TestGitCommitFailedRouting:
    """Integration against ``_push_and_open_pr`` — focus on the two
    ``git commit --amend`` terminal sites. Stubbing out enough of the
    surrounding machinery that the commit branch can fire cleanly."""

    def _patch(
        self,
        d: daemon.DispatcherDaemon,
        *,
        summary_output: dict[str, Any],
        commit_raises: bool = False,
        commit_nonzero: bool = False,
    ) -> None:
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._is_noop_ship = MagicMock(return_value=False)  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._fetch_phase_output = MagicMock(return_value=summary_output)  # type: ignore[method-assign]
        d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]

        import subprocess  # noqa: PLC0415

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            if commit_raises:
                raise OSError("cannot fork git")
            result = MagicMock()
            result.returncode = 1 if commit_nonzero else 0
            result.stdout = ""
            result.stderr = (
                "error: no changes added to commit\n" if commit_nonzero else ""
            )
            return result

        # Patch subprocess.run inside the daemon module.
        self._orig_run = subprocess.run
        subprocess.run = fake_run  # type: ignore[assignment]

    def test_commit_exception_routes(self, tmp_path: Path, monkeypatch: Any) -> None:
        """``git commit --amend`` raising triggers the unified handler
        with ``sub_reason='commit_exception'``.
        """
        d, _conn, _handler = _make_daemon(tmp_path, scope="git_commit_exc")
        worktree = tmp_path / "wt"
        (worktree / "tmp").mkdir(parents=True)

        # Patch the subprocess call via monkeypatch so we restore cleanly.
        import subprocess  # noqa: PLC0415

        def _raise(*args: Any, **kwargs: Any) -> Any:
            raise OSError("cannot fork git")

        monkeypatch.setattr(subprocess, "run", _raise)

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._is_noop_ship = MagicMock(return_value=False)  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._fetch_phase_output = MagicMock(
            return_value={  # type: ignore[method-assign]
                "commit_message": "feat(foo): bar (#3067)",
                "pr_title": "feat(foo): bar (#3067)",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3067",
                "unmet_criteria": [],
            }
        )
        d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]

        d._push_and_open_pr("agent-commit-exc", 3067, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-commit-exc"
        assert kwargs["phase"] == "push_and_pr"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED
        assert kwargs["issue_number"] == 3067
        assert kwargs["details"]["sub_reason"] == "commit_exception"
        assert "cannot fork" in kwargs["details"]["detail"]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_commit_nonzero_routes(self, tmp_path: Path, monkeypatch: Any) -> None:
        """``git commit --amend`` returning non-zero triggers the
        unified handler with ``sub_reason='commit_nonzero_exit'`` and
        the stderr tail.
        """
        d, _conn, _handler = _make_daemon(tmp_path, scope="git_commit_nonzero")
        worktree = tmp_path / "wt"
        (worktree / "tmp").mkdir(parents=True)

        import subprocess  # noqa: PLC0415

        def _nonzero(*args: Any, **kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "error: nothing to commit, working tree clean\n"
            return result

        monkeypatch.setattr(subprocess, "run", _nonzero)

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._is_noop_ship = MagicMock(return_value=False)  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._fetch_phase_output = MagicMock(
            return_value={  # type: ignore[method-assign]
                "commit_message": "feat(foo): bar (#3067)",
                "pr_title": "feat(foo): bar (#3067)",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3067",
                "unmet_criteria": [],
            }
        )
        d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]

        d._push_and_open_pr("agent-commit-nonzero", 3067, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED
        assert kwargs["details"]["sub_reason"] == "commit_nonzero_exit"
        assert kwargs["details"]["exit_code"] == 1
        assert "nothing to commit" in kwargs["stderr_tail"]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]


class TestGitCommitFailedCandidatePickup:
    def test_row_is_tier_2_candidate(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path, scope="git_commit_scan")
        row = _make_failure_row(
            failure_id=9001,
            agent_id="agent-gc",
            category=daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED,
            details={
                "issue_number": 3067,
                "sub_reason": "commit_nonzero_exit",
                "phase": "push_and_pr",
                "exit_code": 1,
                "stderr_tail": "error: nothing to commit",
            },
        )
        conn.cursor_instance.fetchall_queue = [[row]]

        candidates = d._find_diagnoser_candidates()

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["failure_id"] == 9001
        assert cand["category"] == daemon.FAILURE_CATEGORY_GIT_COMMIT_FAILED
        assert cand["tier"] == 2
        assert cand["issue_number"] == 3067


# ==========================================================================
# #3068 — fix_ci BLOCKED verdict routes through _handle_agent_failure
# ==========================================================================


class TestFixCiBlockedTierRegistration:
    def test_fix_ci_blocked_is_tier_3(self) -> None:
        """Exact analog of ralph_not_ship — fix-ci returning BLOCKED
        with a block_reason is "implementation can't continue" with
        no mechanical retry that fixes it.
        """
        assert daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED in daemon.TIER_3_CATEGORIES

    def test_fix_ci_blocked_not_in_auto_retry(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED not in daemon.AUTO_RETRY_CATEGORIES
        )

    def test_fix_ci_blocked_not_in_tier2_buckets(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED
            not in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
        assert (
            daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED
            not in daemon.TIER_2_RECURRENCE_CATEGORIES
        )


class TestFixCiBlockedRouting:
    def _patch(
        self,
        d: daemon.DispatcherDaemon,
        *,
        fix_ci_output: dict[str, Any],
    ) -> None:
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=fix_ci_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._extract_failing_jobs = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._fetch_pr_diff = MagicMock(return_value="")  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._apply_fix_ci_patch = MagicMock()  # type: ignore[method-assign]

    def test_blocked_verdict_routes(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_blocked")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        self._patch(
            d,
            fix_ci_output={
                "verdict": "BLOCKED",
                "block_reason": "pytest fixture relies on S3 creds not in CI",
            },
        )

        agent = {
            "agent_id": "agent-blocked",
            "pr_number": 5001,
            "issue_number": 3068,
            "worktree_path": str(worktree),
            "retries_used": 1,
        }
        pr_status: dict[str, Any] = {"statusCheckRollup": []}

        d._run_fix_ci(agent, pr_status)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-blocked"
        assert kwargs["phase"] == "awaiting_ci"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED
        assert kwargs["issue_number"] == 3068
        details = kwargs["details"]
        assert details["verdict"] == "BLOCKED"
        assert "pytest fixture" in details["block_reason"]
        assert details["pr_number"] == 5001
        assert details["retries_used"] == 1
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_unrecognized_verdict_also_routes(self, tmp_path: Path) -> None:
        """Unrecognized verdict (neither PATCHED / FLAKY / BLOCKED) also
        routes — the daemon's `else` branch treats it as BLOCKED."""
        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_weird")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        self._patch(
            d,
            fix_ci_output={"verdict": "CONFUSED", "block_reason": None},
        )

        agent = {
            "agent_id": "agent-weird",
            "pr_number": 5002,
            "issue_number": 3068,
            "worktree_path": str(worktree),
            "retries_used": 0,
        }

        d._run_fix_ci(agent, {"statusCheckRollup": []})

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED
        assert kwargs["details"]["verdict"] == "CONFUSED"
        assert kwargs["details"]["block_reason"] is None

    def test_patched_verdict_does_not_route(self, tmp_path: Path) -> None:
        """Regression — PATCHED must still flow to ``_apply_fix_ci_patch``
        and not touch the BLOCKED routing path."""
        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_patched")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        self._patch(
            d,
            fix_ci_output={
                "verdict": "PATCHED",
                "commit_message": "fix: y (#3068)",
                "changed_files": ["foo.py"],
            },
        )

        agent = {
            "agent_id": "agent-patched",
            "pr_number": 5003,
            "issue_number": 3068,
            "worktree_path": str(worktree),
            "retries_used": 0,
        }

        d._run_fix_ci(agent, {"statusCheckRollup": []})

        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
        d._apply_fix_ci_patch.assert_called_once()  # type: ignore[union-attr]


class TestFixCiBlockedCandidatePickup:
    def test_row_is_tier_3_candidate(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path, scope="fix_ci_blocked_scan")
        row = _make_failure_row(
            failure_id=9002,
            agent_id="agent-fcb",
            category=daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED,
            details={
                "issue_number": 3068,
                "verdict": "BLOCKED",
                "block_reason": "needs human review",
                "pr_number": 5001,
                "retries_used": 2,
            },
        )
        conn.cursor_instance.fetchall_queue = [[row]]

        candidates = d._find_diagnoser_candidates()

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["category"] == daemon.FAILURE_CATEGORY_FIX_CI_BLOCKED
        assert cand["tier"] == 3
        assert cand["issue_number"] == 3068


# ==========================================================================
# #3069 — _apply_fix_ci_patch cluster routes through _handle_agent_failure
# ==========================================================================


class TestFixCiApplyFailedTierRegistration:
    def test_fix_ci_apply_failed_is_tier_2_first_occurrence(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_FIX_CI_APPLY_FAILED
            in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_fix_ci_apply_failed_not_in_auto_retry(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_FIX_CI_APPLY_FAILED
            not in daemon.AUTO_RETRY_CATEGORIES
        )


class TestFixCiApplyFailedRouting:
    """Each sub-reason routes through ``_handle_agent_failure`` with the
    distinct ``sub_reason`` string in ``details``. The helper
    :meth:`_handle_fix_ci_apply_failure` is the single ingress point —
    testing that directly covers all eight terminal sites because every
    one of them calls the helper (verified by code review + the
    ``missing_commit_message`` end-to-end test below)."""

    def test_helper_routes_with_correct_category(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_apply_helper")
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]

        d._handle_fix_ci_apply_failure(
            agent_id="agent-x",
            sub_reason="git_push_nonzero_exit",
            stderr_tail="remote rejected",
            exit_code=1,
            pr_number=42,
            issue_number=3069,
            retries_used=2,
        )

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-x"
        assert kwargs["phase"] == "awaiting_ci"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_FIX_CI_APPLY_FAILED
        assert kwargs["issue_number"] == 3069
        assert kwargs["stderr_tail"] == "remote rejected"
        assert kwargs["exit_code"] == 1
        details = kwargs["details"]
        assert details["sub_reason"] == "git_push_nonzero_exit"
        assert details["pr_number"] == 42
        assert details["issue_number"] == 3069
        assert details["retries_used"] == 2

    def test_helper_populates_detail_field_when_provided(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_apply_detail")
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]

        d._handle_fix_ci_apply_failure(
            agent_id="agent-y",
            sub_reason="git_push_timeout",
            pr_number=43,
            issue_number=3069,
            retries_used=1,
            detail="subprocess.TimeoutExpired after 60s",
        )

        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["details"]["sub_reason"] == "git_push_timeout"
        assert "TimeoutExpired" in kwargs["details"]["detail"]

    def test_missing_commit_message_end_to_end(self, tmp_path: Path) -> None:
        """End-to-end cover for one of the eight sites — the
        ``missing_commit_message`` path fires before any subprocess
        runs, so the test doesn't need subprocess patching."""
        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_missing_msg")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-empty-msg",
            "worktree_path": str(worktree),
            "retries_used": 0,
            "issue_number": 3069,
            "pr_number": 5050,
        }
        fix_ci_output: dict[str, Any] = {
            "verdict": "PATCHED",
            "commit_message": "",  # MISSING
            "changed_files": ["foo.py"],
        }

        d._apply_fix_ci_patch(agent, fix_ci_output)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_FIX_CI_APPLY_FAILED
        assert kwargs["details"]["sub_reason"] == "missing_commit_message"
        assert kwargs["details"]["issue_number"] == 3069
        assert kwargs["details"]["pr_number"] == 5050
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]


class TestFixCiApplyFailedCandidatePickup:
    def test_row_is_tier_2_candidate(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path, scope="fix_ci_apply_scan")
        row = _make_failure_row(
            failure_id=9003,
            agent_id="agent-fca",
            category=daemon.FAILURE_CATEGORY_FIX_CI_APPLY_FAILED,
            details={
                "issue_number": 3069,
                "sub_reason": "git_push_nonzero_exit",
                "phase": "awaiting_ci",
                "exit_code": 1,
                "stderr_tail": "",
                "pr_number": 5051,
                "retries_used": 2,
            },
        )
        conn.cursor_instance.fetchall_queue = [[row]]

        candidates = d._find_diagnoser_candidates()

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["category"] == daemon.FAILURE_CATEGORY_FIX_CI_APPLY_FAILED
        assert cand["tier"] == 2
        assert cand["issue_number"] == 3069


# ==========================================================================
# #3070 — post-merge deploy_failed routes through _handle_agent_failure
# ==========================================================================


class TestDeployFailedTierRegistration:
    def test_deploy_failed_is_tier_2_first_occurrence(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_DEPLOY_FAILED
            in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_deploy_failed_not_in_auto_retry(self) -> None:
        assert daemon.FAILURE_CATEGORY_DEPLOY_FAILED not in daemon.AUTO_RETRY_CATEGORIES


class TestDeployFailedRouting:
    def test_deploy_failure_routes(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path, scope="deploy_failed")

        d._fetch_pr_status = MagicMock(  # type: ignore[method-assign]
            return_value={"mergeCommit": {"oid": "beefface"}}
        )
        d._extract_merge_sha = MagicMock(return_value="beefface")  # type: ignore[method-assign]
        deploy_runs = [
            {
                "databaseId": 111,
                "workflowName": "Deploy dispatcher",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "createdAt": "2026-04-23T12:00:00Z",
            }
        ]
        d._find_deploy_runs = MagicMock(return_value=deploy_runs)  # type: ignore[method-assign]
        d._classify_deploy_runs = MagicMock(return_value="failure")  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._run_verify_and_complete = MagicMock()  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-deploy-fail",
            "pr_number": 7070,
            "issue_number": 3070,
        }

        d._advance_awaiting_deploy(agent)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-deploy-fail"
        assert kwargs["phase"] == "awaiting_deploy"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_DEPLOY_FAILED
        assert kwargs["issue_number"] == 3070
        details = kwargs["details"]
        assert details["pr_number"] == 7070
        assert details["merge_sha"] == "beefface"
        assert details["deploy_runs"] == deploy_runs
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        d._run_verify_and_complete.assert_not_called()  # type: ignore[union-attr]

    def test_deploy_success_still_advances_to_verify(self, tmp_path: Path) -> None:
        """Regression — a successful deploy still reaches verify and
        does NOT touch the new failure-routing path."""
        d, _conn, _handler = _make_daemon(tmp_path, scope="deploy_success")

        d._fetch_pr_status = MagicMock(  # type: ignore[method-assign]
            return_value={"mergeCommit": {"oid": "cafed00d"}}
        )
        d._extract_merge_sha = MagicMock(return_value="cafed00d")  # type: ignore[method-assign]
        d._find_deploy_runs = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._classify_deploy_runs = MagicMock(return_value="success")  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._run_verify_and_complete = MagicMock()  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-deploy-ok",
            "pr_number": 7071,
            "issue_number": 3070,
        }

        d._advance_awaiting_deploy(agent)

        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        d._run_verify_and_complete.assert_called_once()  # type: ignore[union-attr]


class TestDeployFailedCandidatePickup:
    def test_row_is_tier_2_candidate(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path, scope="deploy_failed_scan")
        row = _make_failure_row(
            failure_id=9004,
            agent_id="agent-df",
            category=daemon.FAILURE_CATEGORY_DEPLOY_FAILED,
            details={
                "issue_number": 3070,
                "phase": "awaiting_deploy",
                "pr_number": 7070,
                "merge_sha": "beefface",
                "exit_code": None,
                "stderr_tail": "",
            },
        )
        conn.cursor_instance.fetchall_queue = [[row]]

        candidates = d._find_diagnoser_candidates()

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["category"] == daemon.FAILURE_CATEGORY_DEPLOY_FAILED
        assert cand["tier"] == 2
        assert cand["issue_number"] == 3070


# ==========================================================================
# #3071 — post-merge verify FAILED routes through _handle_agent_failure
# ==========================================================================


class TestVerifyFailedPostMergeTierRegistration:
    def test_verify_failed_post_merge_is_tier_3(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE in daemon.TIER_3_CATEGORIES
        )

    def test_verify_failed_post_merge_not_in_auto_retry(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE
            not in daemon.AUTO_RETRY_CATEGORIES
        )

    def test_verify_failed_post_merge_not_in_tier2_buckets(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE
            not in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
        assert (
            daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE
            not in daemon.TIER_2_RECURRENCE_CATEGORIES
        )


class TestVerifyFailedPostMergeSkillContract:
    """Issue #3071 AC #3: diagnose-failure skill must have a section
    for verify_failed_post_merge distinguishing post-merge from pre-
    merge failures."""

    def _skill_content(self) -> str:
        skill_path = (
            Path(__file__).resolve().parents[3]
            / ".claude"
            / "skills"
            / "diagnose-failure"
            / "SKILL.md"
        )
        return skill_path.read_text()

    def test_skill_has_verify_failed_post_merge_section(self) -> None:
        content = self._skill_content()
        assert "verify_failed_post_merge" in content, (
            "diagnose-failure/SKILL.md must have a verify_failed_post_merge "
            "section (issue #3071)"
        )

    def test_skill_discourages_retry_for_post_merge(self) -> None:
        """The post-merge section must explicitly tell the diagnoser
        not to pick ``retry`` (the daemon doesn't re-run verify from
        the retry-marker path, so a retry recommendation here would
        leave the agent stuck)."""
        content = self._skill_content().lower()
        # Either phrasing is acceptable — the contract is "don't pick retry".
        assert (
            "do not pick `retry`" in content
            or "do not pick retry" in content
            or "retry` or `retry_with_hint`" in content
        ), (
            "diagnose-failure/SKILL.md must discourage `retry` for "
            "verify_failed_post_merge (issue #3071)"
        )

    def test_skill_mentions_primary_actions(self) -> None:
        """The post-merge section must name ``file_prerequisite_task``
        and ``block_and_comment`` as primary actions — those are the
        diagnoser responses the issue explicitly calls out."""
        content = self._skill_content()
        # Both action names should appear in the new section.
        new_section_start = content.find("verify_failed_post_merge")
        assert new_section_start != -1
        # Look in roughly the next 3000 chars for both mentions.
        window = content[new_section_start : new_section_start + 4000]
        assert "file_prerequisite_task" in window, (
            "verify_failed_post_merge section must name `file_prerequisite_task`"
        )
        assert "block_and_comment" in window, (
            "verify_failed_post_merge section must name `block_and_comment`"
        )


class TestVerifyFailedPostMergeRouting:
    def test_failed_verdict_routes(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path, scope="verify_failed_pm")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "verdict": "FAILED",
                "failure_reason": "search endpoint returned 500 on sample ruling",
                "evidence_md": "## Verify\n\n- FAIL: AC#1 — got 500",
            }
        )
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._write_merged_at = MagicMock()  # type: ignore[method-assign]
        d._read_verify_skip_reason = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._post_evidence_comment = MagicMock()  # type: ignore[method-assign]
        d._write_verified_at = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._extract_acceptance_criteria = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._fetch_issue_body = MagicMock(return_value="")  # type: ignore[method-assign]
        d._fetch_pr_diff = MagicMock(return_value="")  # type: ignore[method-assign]
        d._list_committed_files_at_head = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._detect_verify_skip_reason = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._restore_succeeded_and_advance_done = MagicMock()  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-verify-fail",
            "issue_number": 3071,
            "pr_number": 8080,
            "worktree_path": str(worktree),
            "merged_at": datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC),
        }
        pr_status = {"mergeCommit": {"oid": "f00dbabe"}}

        d._run_verify_and_complete(agent, pr_status, "f00dbabe", [])

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-verify-fail"
        assert kwargs["phase"] == "done"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE
        assert kwargs["issue_number"] == 3071
        details = kwargs["details"]
        assert details["pr_number"] == 8080
        assert details["merge_sha"] == "f00dbabe"
        assert "returned 500" in details["failure_reason"]
        assert "FAIL: AC#1" in details["evidence_md"]
        assert details["issue_number"] == 3071
        # Direct _mark_agent_terminal NOT used (pre-#3071 bypass fixed).
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        # VERIFIED path not taken.
        d._write_verified_at.assert_not_called()  # type: ignore[union-attr]

    def test_verified_verdict_still_succeeds(self, tmp_path: Path) -> None:
        """Regression — VERIFIED must still stamp verified_at and
        advance phase without touching the new failure-routing path."""
        d, _conn, _handler = _make_daemon(tmp_path, scope="verify_verified")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "verdict": "VERIFIED",
                "evidence_md": "## Verify\n\n- PASS: AC#1",
            }
        )
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._write_merged_at = MagicMock()  # type: ignore[method-assign]
        d._read_verify_skip_reason = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._post_evidence_comment = MagicMock()  # type: ignore[method-assign]
        d._write_verified_at = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._extract_acceptance_criteria = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._fetch_issue_body = MagicMock(return_value="")  # type: ignore[method-assign]
        d._fetch_pr_diff = MagicMock(return_value="")  # type: ignore[method-assign]
        d._list_committed_files_at_head = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._detect_verify_skip_reason = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-verified",
            "issue_number": 3071,
            "pr_number": 8081,
            "worktree_path": str(worktree),
            "merged_at": datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC),
        }
        pr_status = {"mergeCommit": {"oid": "f00dbabe"}}

        d._run_verify_and_complete(agent, pr_status, "f00dbabe", [])

        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
        d._write_verified_at.assert_called_once()  # type: ignore[union-attr]
        d._update_agent_phase.assert_called_with("agent-verified", "done")  # type: ignore[union-attr]


class TestVerifyFailedPostMergeCandidatePickup:
    def test_row_is_tier_3_candidate(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path, scope="verify_failed_scan")
        row = _make_failure_row(
            failure_id=9005,
            agent_id="agent-vfpm",
            category=daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE,
            details={
                "issue_number": 3071,
                "phase": "done",
                "pr_number": 8080,
                "merge_sha": "f00dbabe",
                "failure_reason": "search endpoint returned 500",
                "evidence_md": "## Verify\n\n- FAIL: AC#1",
                "exit_code": 0,
                "stderr_tail": "",
            },
        )
        conn.cursor_instance.fetchall_queue = [[row]]

        candidates = d._find_diagnoser_candidates()

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["category"] == daemon.FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE
        assert cand["tier"] == 3
        assert cand["issue_number"] == 3071


# ==========================================================================
# #2966 — fix-ci force-with-lease and rebase_outcome log event
# ==========================================================================


class TestFixCiForceWithLease:
    """Verify that ``_apply_fix_ci_patch`` pushes with ``--force-with-lease``.

    After a rebase (Step 0 of the fix-ci skill), the branch's SHAs are
    rewritten so a bare ``git push`` would be rejected. The daemon must
    use ``--force-with-lease`` to allow the rebase-rewritten history
    through while still guarding against concurrent remote writes.
    """

    def test_push_uses_force_with_lease(self, tmp_path: Path) -> None:
        """The push command list in ``_apply_fix_ci_patch`` must include
        ``--force-with-lease`` (AC2, issue #2966)."""
        import subprocess as _subprocess  # noqa: PLC0415

        d, _conn, _handler = _make_daemon(tmp_path, scope="fix_ci_fwl")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "tmp").mkdir(parents=True, exist_ok=True)

        captured_cmds: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured_cmds.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        orig_run = _subprocess.run

        def fake_subprocess_with_retry(
            cmd: list[str], *, event_name: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_cmds.append(list(cmd))
            return {"ok": True}

        d._subprocess_with_retry = fake_subprocess_with_retry  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._increment_retries_used = MagicMock()  # type: ignore[method-assign]

        try:
            _subprocess.run = fake_run  # type: ignore[assignment]
            agent = {
                "agent_id": "agent-fwl-test",
                "worktree_path": str(worktree),
                "retries_used": 0,
                "issue_number": 2966,
                "pr_number": 9966,
            }
            fix_ci_output = {
                "verdict": "PATCHED",
                "commit_message": "fix(ci): test force-with-lease — CI (#9966)",
                "changed_files": ["foo.py"],
                "rebase_outcome": "clean",
            }
            d._apply_fix_ci_patch(agent, fix_ci_output)
        finally:
            _subprocess.run = orig_run  # type: ignore[assignment]

        # The git push command must contain --force-with-lease
        push_cmds = [cmd for cmd in captured_cmds if "push" in cmd]
        assert push_cmds, "Expected at least one push command to be issued"
        push_cmd = push_cmds[-1]  # last push is the actual remote push
        assert "--force-with-lease" in push_cmd, (
            f"Expected --force-with-lease in push cmd, got: {push_cmd}"
        )
        # Must NOT use bare --force
        assert "--force" not in [
            arg for arg in push_cmd if arg != "--force-with-lease"
        ], "push cmd must not use bare --force"


class TestFixCiRebaseOutcomeLogEvent:
    """Verify ``_run_fix_ci`` emits a ``daemon.fix_ci_rebase_outcome`` log
    event unconditionally after parsing the fix-ci output JSON (AC6, issue
    #2966).

    Three parametrized cases: clean / resolved / conflict_unresolvable.
    Also covers the None case (old skill payload without rebase_outcome).
    """

    def _patch(
        self,
        d: "daemon.DispatcherDaemon",
        *,
        fix_ci_output: dict[str, Any],
    ) -> None:
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=fix_ci_output)  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._extract_failing_jobs = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._fetch_pr_diff = MagicMock(return_value="")  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._apply_fix_ci_patch = MagicMock()  # type: ignore[method-assign]

    def _run(
        self, tmp_path: Path, *, scope: str, fix_ci_output: dict[str, Any]
    ) -> "_CapturingLogHandler":
        d, _conn, handler = _make_daemon(tmp_path, scope=scope)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        self._patch(d, fix_ci_output=fix_ci_output)
        agent = {
            "agent_id": f"agent-{scope}",
            "pr_number": 9966,
            "issue_number": 2966,
            "worktree_path": str(worktree),
            "retries_used": 0,
        }
        d._run_fix_ci(agent, {"statusCheckRollup": []})
        return handler

    def test_rebase_outcome_clean_logged(self, tmp_path: Path) -> None:
        handler = self._run(
            tmp_path,
            scope="fci_ro_clean",
            fix_ci_output={
                "verdict": "PATCHED",
                "commit_message": "fix(ci): clean rebase — CI (#9966)",
                "changed_files": ["a.py"],
                "rebase_outcome": "clean",
            },
        )
        events = handler.events("fix_ci_rebase_outcome")
        assert len(events) == 1, (
            f"Expected 1 fix_ci_rebase_outcome event, got {len(events)}"
        )
        assert getattr(events[0], "rebase_outcome", None) == "clean"
        assert getattr(events[0], "pr_number", None) == 9966

    def test_rebase_outcome_resolved_logged(self, tmp_path: Path) -> None:
        handler = self._run(
            tmp_path,
            scope="fci_ro_resolved",
            fix_ci_output={
                "verdict": "PATCHED",
                "commit_message": "fix(ci): resolved rebase — CI (#9966)",
                "changed_files": ["b.py"],
                "rebase_outcome": "resolved",
            },
        )
        events = handler.events("fix_ci_rebase_outcome")
        assert len(events) == 1
        assert getattr(events[0], "rebase_outcome", None) == "resolved"

    def test_rebase_outcome_conflict_unresolvable_logged(self, tmp_path: Path) -> None:
        handler = self._run(
            tmp_path,
            scope="fci_ro_unresolvable",
            fix_ci_output={
                "verdict": "BLOCKED",
                "block_reason": "Semantic collision in foo.py vs main commit abc123",
                "rebase_outcome": "conflict_unresolvable",
                "changed_files": [],
                "commit_message": "",
            },
        )
        events = handler.events("fix_ci_rebase_outcome")
        assert len(events) == 1
        assert getattr(events[0], "rebase_outcome", None) == "conflict_unresolvable"

    def test_rebase_outcome_none_logged_for_old_skills(self, tmp_path: Path) -> None:
        """Old skill payloads without rebase_outcome log rebase_outcome=None —
        they are trivially spottable in CloudWatch (issue #2966)."""
        handler = self._run(
            tmp_path,
            scope="fci_ro_none",
            fix_ci_output={
                "verdict": "PATCHED",
                "commit_message": "fix(ci): no rebase_outcome field — CI (#9966)",
                "changed_files": ["c.py"],
                # No rebase_outcome field
            },
        )
        events = handler.events("fix_ci_rebase_outcome")
        assert len(events) == 1
        assert getattr(events[0], "rebase_outcome", "MISSING") is None
