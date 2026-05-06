"""Unit tests for the dispatcher's queue-scan filter pipeline (#4211).

Specifically covers the shipped-zombie short-circuit added in
:meth:`DispatcherDaemon._pick_candidate_issue`:

* When ``scripts/check-shipped-pr.sh`` reports a high-confidence shipped
  match (exit 0 + JSON summary), the dispatcher must NOT spawn a /task
  agent on the candidate. Instead it inline-cleans up the zombie issue
  (post verification-evidence comment + close with --reason completed +
  strip queue-scan labels) and skips to the next candidate.
* On exit 1 (not-shipped) or exit 2 (script error), the dispatcher
  proceeds normally — no behaviour change for non-zombie issues.

Issue #4211. Companion to ``scripts/check-shipped-pr.sh`` (#4204) and
the agent-side /task §4a.2 pivot which remains as defense-in-depth.

All external calls — subprocess (``scripts/check-shipped-pr.sh``,
``gh``) and psycopg — are mocked. No real ``gh`` invocations leave the
test runner.

Test fixtures piggy-back on the ``_make_daemon`` / ``_FakeConnection``
helpers in ``test_daemon_phase3a.py`` to stay consistent with the rest
of the daemon test suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above
from dispatcher.tests.test_daemon_phase3a import _make_daemon  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _shipped_stdout(
    issue: int,
    pr: int,
    overlap_files: list[str] | None = None,
    added_files: list[str] | None = None,
) -> str:
    """Build a stdout payload matching what scripts/check-shipped-pr.sh
    emits on exit 0: a leading ``shipped:`` sentinel line followed by
    a pretty JSON object (per ``scripts/_check_shipped_pr_summary.py``).
    """
    overlap_files = overlap_files or []
    added_files = added_files or []
    summary = {
        "issue": issue,
        "shipped_pr": pr,
        "overlap_count": len(overlap_files),
        "overlap_files": overlap_files,
        "added_files": added_files,
        "candidate_files": overlap_files,
    }
    return (
        f"shipped: PR #{pr} merged to main with {len(overlap_files)} file "
        f"overlap(s) for issue #{issue} (exit 0)\n"
        f"{json.dumps(summary, indent=2, sort_keys=True)}\n"
    )


def _make_subprocess_runner(
    *,
    shipped_returncode: int,
    shipped_stdout: str = "",
    shipped_stderr: str = "",
) -> tuple[
    list[list[str]],
    list[list[str]],
    Any,
]:
    """Build a fake ``subprocess.run`` that:

    * Returns the configured exit code + stdout for any
      ``scripts/check-shipped-pr.sh ...`` invocation (the queue-scan
      filter).
    * Returns exit 0 with empty stdout for ``gh issue close``,
      ``gh issue comment``, ``gh issue edit`` (the cleanup path).
    * Captures every call so tests can inspect what the daemon sent.

    Returns ``(shipped_calls, gh_calls, fake_run)`` so the test can
    assert against both call lists.
    """
    shipped_calls: list[list[str]] = []
    gh_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        if cmd and cmd[0].endswith("check-shipped-pr.sh"):
            shipped_calls.append(cmd)
            result.returncode = shipped_returncode
            result.stdout = shipped_stdout
            result.stderr = shipped_stderr
            return result

        if cmd and cmd[0] == "gh":
            gh_calls.append(cmd)
            return result

        return result

    return shipped_calls, gh_calls, fake_run


# --------------------------------------------------------------------------
# AC #1 — shipped zombie is skipped (no /task spawn)
# --------------------------------------------------------------------------


class TestShippedZombieSkipped:
    """``_pick_candidate_issue`` MUST NOT return a candidate that
    ``check-shipped-pr.sh`` reports as shipped — the inline cleanup runs
    and the picker advances past the zombie. The downstream effect in
    :meth:`_claim_and_orchestrate_one` is that ``_atomic_claim`` /
    ``_create_worktree`` / ``_run_orchestration_phases`` are never
    invoked for this candidate (a "no /task spawn" outcome).
    """

    def test_shipped_zombie_skipped(self, monkeypatch: Any, tmp_path: Path) -> None:
        """The single-candidate case: ``check-shipped-pr.sh`` returns
        shipped → ``_pick_candidate_issue`` returns ``None`` and emits
        a ``candidate_skipped`` event with ``reason='shipped_zombie'``.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        # All upstream gates pass — the only thing that should drop the
        # candidate is the new shipped-zombie hook.
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        shipped_calls, gh_calls, fake_run = _make_subprocess_runner(
            shipped_returncode=0,
            shipped_stdout=_shipped_stdout(
                issue=2831,
                pr=3229,
                overlap_files=["scripts/check-issue-author.sh"],
                added_files=["scripts/check-issue-author.sh"],
            ),
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._pick_candidate_issue([2831])
        assert result is None, "shipped zombie must NOT be returned for /task spawn"

        # The shipped-pr check ran exactly once with the issue number.
        assert len(shipped_calls) == 1
        assert shipped_calls[0][0].endswith("check-shipped-pr.sh")
        assert "2831" in shipped_calls[0]

        # candidate_skipped event with the right reason.
        skipped = handler.events("candidate_skipped")
        assert len(skipped) == 1
        assert getattr(skipped[0], "reason", None) == "shipped_zombie"
        assert getattr(skipped[0], "issue_number", None) == 2831
        assert getattr(skipped[0], "shipped_pr", None) == 3229

    def test_shipped_zombie_then_eligible_returns_eligible(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """When the first candidate is a shipped zombie, the picker must
        advance to the next candidate and return it when the next one
        passes all gates (including the not-shipped check).
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        # Issue 2831 is shipped; issue 9999 is not. The fake_run
        # branches on the issue argument so the second
        # ``check-shipped-pr.sh`` call returns exit 1.
        shipped_calls: list[list[str]] = []
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd and cmd[0].endswith("check-shipped-pr.sh"):
                shipped_calls.append(cmd)
                if "2831" in cmd:
                    result.returncode = 0
                    result.stdout = _shipped_stdout(
                        issue=2831,
                        pr=3229,
                        overlap_files=["scripts/foo.sh"],
                        added_files=[],
                    )
                else:
                    result.returncode = 1
                    result.stdout = "not-shipped: ...\n"
                return result
            if cmd and cmd[0] == "gh":
                gh_calls.append(cmd)
                return result
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._pick_candidate_issue([2831, 9999])
        assert result == 9999, (
            "picker must advance past shipped zombie to next eligible candidate"
        )
        # Both candidates' shipped checks ran.
        assert len(shipped_calls) == 2


# --------------------------------------------------------------------------
# AC #2 — shipped zombie is closed inline (comment + close + label strip)
# --------------------------------------------------------------------------


class TestShippedZombieClosedInline:
    """When ``check-shipped-pr.sh`` reports shipped, the daemon must
    inline-cleanup the zombie:

    * Post a verification-evidence comment naming the shipped PR.
    * Close the issue with ``--reason completed``.
    * Remove ``agent/ready`` and ``status/in-progress`` labels
      (idempotent — both should be no-ops if already absent).

    All three GitHub mutations are observable as ``gh`` subprocess
    invocations.
    """

    def test_shipped_zombie_closed_inline(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        shipped_calls, gh_calls, fake_run = _make_subprocess_runner(
            shipped_returncode=0,
            shipped_stdout=_shipped_stdout(
                issue=2831,
                pr=3229,
                overlap_files=[
                    "scripts/check-issue-author.sh",
                    "docs/agent/issue-authoring.md",
                ],
                added_files=["scripts/check-issue-author.sh"],
            ),
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._pick_candidate_issue([2831])
        assert result is None

        # The cleanup made three classes of gh calls — comment, close,
        # remove-label. Order is deterministic in
        # ``_handle_shipped_zombie``.
        comment_calls = [c for c in gh_calls if c[1:3] == ["issue", "comment"]]
        close_calls = [c for c in gh_calls if c[1:3] == ["issue", "close"]]
        label_calls = [
            c for c in gh_calls if c[1:3] == ["issue", "edit"] and "--remove-label" in c
        ]

        assert len(comment_calls) == 1, "expected exactly one comment call"
        assert "2831" in comment_calls[0]

        assert len(close_calls) == 1, "expected exactly one close call"
        assert "2831" in close_calls[0]
        assert "--reason" in close_calls[0]
        # ``gh issue close --reason completed``.
        idx = close_calls[0].index("--reason")
        assert close_calls[0][idx + 1] == "completed"

        assert len(label_calls) == 1, "expected exactly one label-remove call"
        assert "2831" in label_calls[0]
        # The label CSV must contain BOTH ``agent/ready`` and
        # ``status/in-progress`` so the issue drops out of the
        # queue-scan filter and the (rare) post-claim race window
        # cannot leave the in-progress label dangling. ``gh`` accepts a
        # single comma-separated string after ``--remove-label``.
        idx = label_calls[0].index("--remove-label")
        label_csv = label_calls[0][idx + 1]
        assert "agent/ready" in label_csv
        assert daemon.STATUS_IN_PROGRESS_LABEL in label_csv

        # Lifecycle log events for the cleanup are present.
        assert handler.events("shipped_zombie_cleanup_begin") != []
        assert handler.events("shipped_zombie_cleanup_done") != []


# --------------------------------------------------------------------------
# AC #3 — not-shipped proceeds normally to /task spawn
# --------------------------------------------------------------------------


class TestNotShippedProceedsToTaskSpawn:
    """Exit 1 (not-shipped) and exit 2 (script error) are fail-open: the
    candidate is returned for the normal claim path. No GitHub mutations
    fire (no comment, no close, no label strip).
    """

    def test_not_shipped_proceeds_to_task_spawn(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Exit 1 → ``_pick_candidate_issue`` returns the candidate
        unchanged. The agent-spawn code path downstream is exactly what
        runs today — no behaviour change.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        shipped_calls, gh_calls, fake_run = _make_subprocess_runner(
            shipped_returncode=1,
            shipped_stdout=(
                "not-shipped: no candidate file paths in issue #4242 body (exit 1)\n"
            ),
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._pick_candidate_issue([4242])
        assert result == 4242, (
            "not-shipped candidate must proceed to the normal claim path"
        )
        # The shipped-pr check ran exactly once.
        assert len(shipped_calls) == 1
        # No GitHub mutations — the cleanup path must not have fired.
        assert gh_calls == []

    def test_script_error_proceeds_to_task_spawn(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Exit 2 (script-level error — ``gh`` flake, missing arg,
        etc.) MUST also proceed to the normal claim path. Fail-open by
        design: a transient gh failure must never permanently block a
        legitimate claim.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        shipped_calls, gh_calls, fake_run = _make_subprocess_runner(
            shipped_returncode=2,
            shipped_stderr="error: gh CLI auth failed (exit 2)\n",
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._pick_candidate_issue([4242])
        assert result == 4242
        assert gh_calls == []
        # Exit 2 must log a ``shipped_check_error`` event so operators
        # can see the script error trail in CloudWatch.
        assert handler.events("shipped_check_error") != []

    def test_subprocess_timeout_proceeds_to_task_spawn(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A subprocess timeout on ``check-shipped-pr.sh`` MUST be
        treated as fail-open (return None from
        :meth:`_issue_already_shipped` → candidate proceeds). Logging
        ``shipped_check_timeout`` makes the timeout observable.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            if cmd and cmd[0].endswith("check-shipped-pr.sh"):
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = d._pick_candidate_issue([4242])
        assert result == 4242
        assert handler.events("shipped_check_timeout") != []

    def test_missing_script_proceeds_to_task_spawn(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """If ``scripts/check-shipped-pr.sh`` is not on PATH (operator
        ran the daemon from outside the repo), the missing-script
        condition is treated as fail-open and the candidate proceeds.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            if cmd and cmd[0].endswith("check-shipped-pr.sh"):
                raise FileNotFoundError(2, "no such file", cmd[0])
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = d._pick_candidate_issue([4242])
        assert result == 4242
        assert handler.events("shipped_check_missing") != []

    def test_unparsable_stdout_proceeds_to_task_spawn(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Exit 0 but with malformed stdout (no JSON after the sentinel
        line) MUST be fail-open. Without this guard a one-off bug in
        the script could wedge the queue-scan filter on every candidate.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: False  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: False  # type: ignore[method-assign]
        d._orphan_pr_recovery_pending = lambda n: False  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: True  # type: ignore[method-assign]

        shipped_calls, gh_calls, fake_run = _make_subprocess_runner(
            shipped_returncode=0,
            # Looks like a shipped: line but no JSON follows.
            shipped_stdout="shipped: PR #1 garbage no json\n",
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._pick_candidate_issue([4242])
        assert result == 4242
        # No cleanup fired (no gh mutations).
        assert gh_calls == []
        assert handler.events("shipped_check_unparsable") != []


# --------------------------------------------------------------------------
# Sanity — the new helper methods exist and have the expected shape
# --------------------------------------------------------------------------


class TestHelperShape:
    """Lightweight schema tests so a typo in the daemon refactor
    surfaces here, rather than at runtime in production.
    """

    def test_issue_already_shipped_method_exists(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert callable(getattr(d, "_issue_already_shipped", None))

    def test_handle_shipped_zombie_method_exists(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert callable(getattr(d, "_handle_shipped_zombie", None))

    def test_shipped_check_subprocess_timeout_constant(self, tmp_path: Path) -> None:
        """The per-candidate timeout constant is exposed at module
        scope so audit / config tooling can read it without parsing
        the daemon source."""
        assert isinstance(daemon.SHIPPED_CHECK_SUBPROCESS_TIMEOUT_SECONDS, int)
        # AC #4 budget: ≤2s wall-clock per candidate. The configured
        # ceiling is the SAFETY cap — typical responses come back in
        # <1s — but it must not exceed the queue-scan timeout
        # (otherwise a single stuck call could leak ticks).
        assert (
            daemon.SHIPPED_CHECK_SUBPROCESS_TIMEOUT_SECONDS
            <= daemon.QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS
        )
