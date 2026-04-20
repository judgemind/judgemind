"""Unit tests for the deprecated-stub ``scripts.dispatcher.task_claim``.

Issue #2927 ripped out the DB-row interlock between the ``/task`` skill
and the Fargate dispatcher daemon. The helper is now a no-op stub that
preserves the old CLI signature so any lingering caller (stale CLAUDE.md
copy, older agent transcript, retrospective script) does not crash
mid-task. These tests lock in the stub contract:

* both subcommands exit 0,
* both emit the one-line deprecation notice on stderr,
* both emit a minimal JSON payload on stdout,
* neither touches the database or any subprocess (no psycopg import, no
  ``dev-db-query.sh`` shell-out).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import task_claim  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# ``claim`` stub contract
# --------------------------------------------------------------------------


class TestClaimSubcommand:
    """``claim`` accepts every historical flag, returns 0, touches nothing."""

    def test_happy_path_returns_zero_and_emits_noop_payload(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = task_claim.main(
            [
                "claim",
                "--issue",
                "2927",
                "--agent-id",
                "agent-deadbeef",
                "--worktree-path",
                "/tmp/wt",
            ]
        )
        assert rc == 0
        out = capsys.readouterr()
        payload = json.loads(out.out.strip())
        assert payload == {
            "status": "deprecated_noop",
            "subcommand": "claim",
            "issue_number": 2927,
        }
        assert "deprecated no-op" in out.err
        assert "#2927" in out.err

    def test_accepts_optional_issue_title_flag(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = task_claim.main(
            [
                "claim",
                "--issue",
                "2927",
                "--agent-id",
                "agent-deadbeef",
                "--worktree-path",
                "/tmp/wt",
                "--issue-title",
                "historical invocation",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["subcommand"] == "claim"

    def test_does_not_import_psycopg_or_shell_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Stub must not touch the database under any code path.

        Install a boobytrap on ``subprocess.run`` so the test fails
        immediately if the stub ever shells out (e.g. to
        ``dev-db-query.sh``). The psycopg import likewise should never
        happen — a fresh ``sys.modules`` lookup after the call confirms
        no import was forced.
        """

        def boobytrap(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError(
                "stub must not call subprocess.run (#2927 no-op contract)"
            )

        monkeypatch.setattr(subprocess, "run", boobytrap)

        # Force the import guard: remove any pre-existing psycopg binding
        # so the stub would fail on import if it ever tried to use it.
        monkeypatch.setitem(sys.modules, "psycopg", None)

        rc = task_claim.main(
            [
                "claim",
                "--issue",
                "2927",
                "--agent-id",
                "agent-deadbeef",
                "--worktree-path",
                "/tmp/wt",
            ]
        )
        assert rc == 0
        # Sanity check: capsys still sees the deprecation notice.
        assert "deprecated no-op" in capsys.readouterr().err


# --------------------------------------------------------------------------
# ``terminal`` stub contract
# --------------------------------------------------------------------------


class TestTerminalSubcommand:
    """``terminal`` accepts every historical flag, returns 0, touches nothing."""

    def test_happy_path_returns_zero_and_emits_noop_payload(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = task_claim.main(
            [
                "terminal",
                "--agent-id",
                "agent-deadbeef",
                "--status",
                "succeeded",
            ]
        )
        assert rc == 0
        out = capsys.readouterr()
        payload = json.loads(out.out.strip())
        assert payload == {
            "status": "deprecated_noop",
            "subcommand": "terminal",
            "agent_id": "agent-deadbeef",
        }
        assert "deprecated no-op" in out.err

    def test_accepts_optional_pr_number_flag(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = task_claim.main(
            [
                "terminal",
                "--agent-id",
                "agent-deadbeef",
                "--status",
                "succeeded",
                "--pr-number",
                "4242",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["subcommand"] == "terminal"

    def test_accepts_failed_status(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Historical callers pass ``failed`` on STUCK-exit paths.

        The pre-#2927 helper rejected statuses outside
        ``{succeeded, failed}``; the stub no longer validates because a
        historical caller using the old contract should never be
        turned into a crash mid-cleanup. Any string is accepted.
        """
        rc = task_claim.main(
            [
                "terminal",
                "--agent-id",
                "agent-deadbeef",
                "--status",
                "failed",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["subcommand"] == "terminal"

    def test_does_not_import_psycopg_or_shell_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Terminal path has the same no-DB-access contract as claim."""

        def boobytrap(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError(
                "stub must not call subprocess.run (#2927 no-op contract)"
            )

        monkeypatch.setattr(subprocess, "run", boobytrap)
        monkeypatch.setitem(sys.modules, "psycopg", None)

        rc = task_claim.main(
            [
                "terminal",
                "--agent-id",
                "agent-deadbeef",
                "--status",
                "succeeded",
            ]
        )
        assert rc == 0


# --------------------------------------------------------------------------
# argparse contract — historical flag compatibility
# --------------------------------------------------------------------------


class TestArgparseContract:
    """The stub must not regress the pre-#2927 argparse surface.

    Older agent transcripts and stale retrospective scripts may still
    issue the full historical command line. argparse must accept each
    required flag; missing a required flag still exits non-zero (via
    :class:`SystemExit`) so callers can detect a genuinely malformed
    invocation without the stub swallowing it.
    """

    def test_claim_requires_issue_flag(self) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "claim",
                    # missing --issue
                    "--agent-id",
                    "agent-deadbeef",
                    "--worktree-path",
                    "/tmp/wt",
                ]
            )

    def test_claim_requires_agent_id_flag(self) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "claim",
                    "--issue",
                    "2927",
                    # missing --agent-id
                    "--worktree-path",
                    "/tmp/wt",
                ]
            )

    def test_claim_requires_worktree_path_flag(self) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "claim",
                    "--issue",
                    "2927",
                    "--agent-id",
                    "agent-deadbeef",
                    # missing --worktree-path
                ]
            )

    def test_terminal_requires_agent_id_flag(self) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "terminal",
                    # missing --agent-id
                    "--status",
                    "succeeded",
                ]
            )

    def test_terminal_requires_status_flag(self) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "terminal",
                    "--agent-id",
                    "agent-deadbeef",
                    # missing --status
                ]
            )

    def test_no_subcommand_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            task_claim.main([])


# --------------------------------------------------------------------------
# Deprecation notice content
# --------------------------------------------------------------------------


class TestDeprecationNotice:
    """The module-level notice constant must name the replacement protocol.

    Sanity test: a developer reading the stderr output after a stale
    call should get a clear pointer to the new label-only flow, the
    issue number that made the change, and the authoritative docs.
    """

    def test_notice_mentions_issue_number(self) -> None:
        assert "#2927" in task_claim.DEPRECATION_NOTICE

    def test_notice_points_to_skill_md(self) -> None:
        assert "SKILL.md" in task_claim.DEPRECATION_NOTICE

    def test_notice_points_to_claude_md(self) -> None:
        assert "CLAUDE.md" in task_claim.DEPRECATION_NOTICE

    def test_notice_mentions_label_only_flow(self) -> None:
        assert "label-only" in task_claim.DEPRECATION_NOTICE
        assert "status/in-progress" in task_claim.DEPRECATION_NOTICE
