#!/usr/bin/env python3
# venv: /Users/drewthaler/judgemind/judgemind-bootstrap/.venv
# permanent: true
"""Tests for scripts/preflight-bash-fargate.sh

The Fargate container preflight hook is a narrow replacement for the
operator-local `.claude/hooks/preflight-bash.sh`. It ONLY blocks the 4
safety-critical rules:

    - git push to main/master
    - git worktree add inside an existing worktree
    - cross-worktree writes (cp/mv/tar/redirect into the main repo)
    - bare git stash pop/apply

Every rule the operator-local hook blocks purely to prevent interactive
permission prompts (``$(...)``, heredocs, ``python -c``, quoted strings
with ``&&``/``;``, long-running-without-timeout, etc.) MUST be allowed
here — the subagent Claude CLI in the Fargate container runs with
``--dangerously-skip-permissions`` and does not stall on prompts.

Run from the repo root:
    pytest scripts/tests/test_preflight_bash_fargate.py -v

This file is pytest-native so it plugs into the existing
``scripts-tests`` CI job that already runs ``pytest scripts/tests/``.

See issue #2982 for the full motivation.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
HOOK = REPO_ROOT / "scripts" / "preflight-bash-fargate.sh"

# Synthetic paths used to simulate a worktree cwd from inside a test
# environment. The hook's worktree-aware checks look for
# ``.claude/worktrees/`` in the cwd path, so we need a cwd override that
# predictably contains (or doesn't contain) that substring regardless of
# where the test is actually running from.
SYNTHETIC_MAIN_REPO = "/Users/test/judgemind/judgemind-bootstrap"
SYNTHETIC_WORKTREE = (
    "/Users/test/judgemind/judgemind-bootstrap/.claude/worktrees/agent-abc123"
)


def run_hook(
    command: str,
    cwd_override: str | None = None,
    timeout: int | None = None,
    run_in_background: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Feed the hook a JSON payload on stdin and return the completed process.

    Mirrors the invocation shape used by the real PreToolUse hook.
    Returncode 0 = allow, 2 = block.
    """
    tool_input: dict = {"command": command}
    if timeout is not None:
        tool_input["timeout"] = timeout
    if run_in_background:
        tool_input["run_in_background"] = True
    input_json = json.dumps({"tool_input": tool_input})
    env = os.environ.copy()
    if cwd_override is not None:
        env["PREFLIGHT_CWD"] = cwd_override
    return subprocess.run(
        ["bash", str(HOOK)],
        input=input_json,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# The 4 safety-critical rules — must BLOCK
# ---------------------------------------------------------------------------


class TestSafetyRulesKeepBlocking:
    """The 4 kept rules must still block in the Fargate hook."""

    # --- Check 0: git push to main/master ---

    def test_git_push_origin_main_blocked(self) -> None:
        result = run_hook("git push origin main", cwd_override=SYNTHETIC_MAIN_REPO)
        assert result.returncode == 2, result.stderr

    def test_git_push_u_origin_main_blocked(self) -> None:
        result = run_hook("git push -u origin main", cwd_override=SYNTHETIC_MAIN_REPO)
        assert result.returncode == 2, result.stderr

    def test_git_push_origin_master_blocked(self) -> None:
        result = run_hook("git push origin master", cwd_override=SYNTHETIC_MAIN_REPO)
        assert result.returncode == 2, result.stderr

    def test_git_push_c_flag_origin_main_blocked(self) -> None:
        result = run_hook(
            "git -C /some/path push origin main",
            cwd_override=SYNTHETIC_MAIN_REPO,
        )
        assert result.returncode == 2, result.stderr

    def test_git_push_feature_branch_allowed(self) -> None:
        result = run_hook(
            "git push -u origin feature-branch",
            cwd_override=SYNTHETIC_MAIN_REPO,
        )
        assert result.returncode == 0, result.stderr

    def test_git_add_pre_push_file_allowed(self) -> None:
        """Filename contains 'push' but is not a git push subcommand."""
        result = run_hook(
            "git add .githooks/pre-push", cwd_override=SYNTHETIC_MAIN_REPO
        )
        assert result.returncode == 0, result.stderr

    # --- Check 10: git worktree add inside a worktree ---

    def test_git_worktree_add_inside_worktree_blocked(self) -> None:
        result = run_hook(
            "git worktree add ../other feature-branch",
            cwd_override=SYNTHETIC_WORKTREE,
        )
        assert result.returncode == 2, result.stderr

    def test_git_worktree_add_outside_worktree_allowed(self) -> None:
        """Outside a worktree (e.g. the main repo or the baseline clone), ``git
        worktree add`` is the correct way to carve a new worktree."""
        result = run_hook(
            "git worktree add ../foo feature-branch",
            cwd_override=SYNTHETIC_MAIN_REPO,
        )
        assert result.returncode == 0, result.stderr

    def test_git_worktree_list_inside_worktree_allowed(self) -> None:
        result = run_hook("git worktree list", cwd_override=SYNTHETIC_WORKTREE)
        assert result.returncode == 0, result.stderr

    # --- Check 11: cross-worktree writes ---

    def test_cp_into_main_repo_from_worktree_blocked(self) -> None:
        """Writing into /Users/test/.../docs/ from a worktree is forbidden."""
        result = run_hook(
            "cp foo.txt /Users/test/judgemind/judgemind-bootstrap/docs/foo.txt",
            cwd_override=SYNTHETIC_WORKTREE,
        )
        assert result.returncode == 2, result.stderr

    def test_cp_into_worktree_root_allowed(self) -> None:
        result = run_hook(
            "cp foo.txt /Users/test/judgemind/judgemind-bootstrap/.claude/worktrees/agent-abc123/tmp/foo.txt",
            cwd_override=SYNTHETIC_WORKTREE,
        )
        assert result.returncode == 0, result.stderr

    def test_cp_into_repo_tmp_allowed(self) -> None:
        """$REPO_ROOT/tmp/ is the allowlisted cross-worktree drop path."""
        result = run_hook(
            "cp foo.txt /Users/test/judgemind/judgemind-bootstrap/tmp/foo.txt",
            cwd_override=SYNTHETIC_WORKTREE,
        )
        assert result.returncode == 0, result.stderr

    def test_cp_outside_repo_entirely_allowed(self) -> None:
        result = run_hook("cp foo.txt /tmp/foo.txt", cwd_override=SYNTHETIC_WORKTREE)
        assert result.returncode == 0, result.stderr

    def test_redirect_into_main_repo_from_worktree_blocked(self) -> None:
        result = run_hook(
            "echo hello > /Users/test/judgemind/judgemind-bootstrap/docs/foo.txt",
            cwd_override=SYNTHETIC_WORKTREE,
        )
        assert result.returncode == 2, result.stderr

    # --- Check 12: bare git stash pop/apply ---

    def test_bare_git_stash_pop_blocked(self) -> None:
        result = run_hook("git stash pop")
        assert result.returncode == 2, result.stderr

    def test_bare_git_stash_apply_blocked(self) -> None:
        result = run_hook("git stash apply")
        assert result.returncode == 2, result.stderr

    def test_git_stash_pop_with_explicit_ref_allowed(self) -> None:
        result = run_hook("git stash pop stash@{0}")
        assert result.returncode == 0, result.stderr

    def test_git_stash_apply_with_explicit_ref_allowed(self) -> None:
        result = run_hook("git stash apply stash@{2}")
        assert result.returncode == 0, result.stderr

    def test_git_stash_list_allowed(self) -> None:
        result = run_hook("git stash list")
        assert result.returncode == 0, result.stderr

    def test_git_stash_push_allowed(self) -> None:
        result = run_hook("git stash push -m WIP")
        assert result.returncode == 0, result.stderr

    def test_git_stash_pop_in_quoted_arg_allowed(self) -> None:
        """The literal string in a quoted PR title must not trigger."""
        result = run_hook(
            'gh pr create --title "block bare git stash pop / apply" --body-file tmp/b.txt'
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The 6 operator-local rules — must be ALLOWED in the Fargate hook
#
# These are the rules the operator-local hook blocks to prevent the
# interactive Claude CLI from stalling on permission prompts. In the Fargate
# container the subagent runs with --dangerously-skip-permissions, so the
# prompts do not exist and these patterns are safe.
# ---------------------------------------------------------------------------


class TestInteractivePromptRulesDropped:
    """Rules blocked by the operator-local hook, allowed here."""

    # --- Operator check 1: $() command substitution ---

    def test_dollar_paren_allowed(self) -> None:
        result = run_hook("echo $(whoami)")
        assert result.returncode == 0, result.stderr

    def test_dollar_paren_nested_allowed(self) -> None:
        result = run_hook("RUN_ID=$(gh run list --limit 1 --json databaseId)")
        assert result.returncode == 0, result.stderr

    # --- Operator check 2: heredocs ---

    def test_heredoc_allowed(self) -> None:
        result = run_hook("cat <<EOF\nhello\nEOF")
        assert result.returncode == 0, result.stderr

    def test_heredoc_with_dash_allowed(self) -> None:
        result = run_hook("cat <<-EOF\n\thello\nEOF")
        assert result.returncode == 0, result.stderr

    # --- Operator check 3: inline python -c ---

    def test_python_c_allowed(self) -> None:
        result = run_hook('python3 -c "import os; print(os.getcwd())"')
        assert result.returncode == 0, result.stderr

    def test_python_c_short_flag_allowed(self) -> None:
        result = run_hook("python -c import os")
        assert result.returncode == 0, result.stderr

    # --- Operator check 4: quoted strings + && or ; ---

    def test_quoted_string_with_double_amp_allowed(self) -> None:
        result = run_hook("echo 'hello' && echo world")
        assert result.returncode == 0, result.stderr

    def test_quoted_string_with_semicolon_allowed(self) -> None:
        result = run_hook("bash .githooks/pre-push 2>stderr.txt; echo EXIT:$?")
        assert result.returncode == 0, result.stderr

    # --- Operator check 5: cd in compound commands ---

    def test_cd_in_compound_command_allowed(self) -> None:
        result = run_hook("cd /tmp && ls")
        assert result.returncode == 0, result.stderr

    # --- Operator check 6: empty-quotes bypass ---

    def test_empty_single_quotes_before_flag_allowed(self) -> None:
        result = run_hook("rm '' -rf /tmp/foo")
        # Note: this is allowed because the empty-quotes-bypass rule is an
        # operator-interactive protection. Real destructive rm -rf paths are
        # still filtered by the surrounding harness policy.
        assert result.returncode == 0, result.stderr

    # --- Operator check 7: terraform apply from root infra path ---

    def test_terraform_apply_from_root_infra_allowed(self) -> None:
        """Operator-local hook blocks this to nudge toward env-specific paths,
        but the Fargate daemon does not run interactive terraform — if a
        subagent somehow does, no interactive session rule should hold it up."""
        result = run_hook("terraform -chdir=infra/terraform apply -auto-approve")
        assert result.returncode == 0, result.stderr

    # --- Operator check 8: long-running without timeout ---

    def test_pytest_without_timeout_allowed(self) -> None:
        """The operator-local hook demands timeout >= 300000 on pytest to
        avoid auto-backgrounding. In the Fargate container the subagent runs
        the command synchronously via subprocess.run with its own timeout, so
        the harness auto-background knob does not apply."""
        result = run_hook("pytest packages/api/tests/")
        assert result.returncode == 0, result.stderr

    # --- Operator check 9: run_in_background in worktree ---

    def test_run_in_background_in_worktree_allowed(self) -> None:
        result = run_hook(
            "gh run watch 12345",
            cwd_override=SYNTHETIC_WORKTREE,
            run_in_background=True,
        )
        assert result.returncode == 0, result.stderr

    # --- The canonical #2982 smoke case: ralph Step 2.5 invocations ---

    def test_ralph_step_2_5_pre_push_invocation_allowed(self) -> None:
        """The literal invocation ralph's Step 2.5 tried and had rejected on
        #2960 must work in the Fargate hook."""
        result = run_hook("bash .githooks/pre-push 2> stderr.txt; echo EXIT:$?")
        assert result.returncode == 0, result.stderr

    def test_ralph_step_2_5_bash_script_prefix_allowed(self) -> None:
        """`bash script.sh` prefix was rejected by the operator-local hook's
        permission policy; the Fargate hook allows it."""
        result = run_hook("bash /tmp/run_prepush.sh 2>&1")
        assert result.returncode == 0, result.stderr

    def test_ralph_step_2_5_direct_hook_invocation_allowed(self) -> None:
        """`.githooks/pre-push < stdin.txt` was rejected by the allowlist;
        the Fargate hook allows it."""
        result = run_hook(".githooks/pre-push < stdin.txt")
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Harness entrypoint sanity
# ---------------------------------------------------------------------------


def test_hook_exists_and_is_executable() -> None:
    """Regression: the hook file is present and the mode bit is on.

    Dockerfile.dispatcher relies on the hook being executable when it COPYs
    it into ``.claude/hooks/preflight-bash.sh`` inside the image.
    """
    assert HOOK.exists(), f"missing: {HOOK}"
    assert os.access(HOOK, os.X_OK), f"not executable: {HOOK}"


def test_hook_handles_malformed_json_gracefully() -> None:
    """If the JSON payload can't be parsed, the hook must exit 0 (allow) so
    malformed input never bricks tool execution."""
    result = subprocess.run(
        ["bash", str(HOOK)],
        input="not valid json",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
