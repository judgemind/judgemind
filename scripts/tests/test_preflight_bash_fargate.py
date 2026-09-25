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
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
HOOK = REPO_ROOT / "scripts" / "preflight-bash-fargate.sh"
HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
OPERATOR_HOOK = HOOKS_DIR / "preflight-bash.sh"
SHARED_LIB = HOOKS_DIR / "preflight_shared_checks.sh"
CROSS_WT_HELPER = HOOKS_DIR / "preflight_cross_worktree.py"

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
    hook: Path = HOOK,
) -> subprocess.CompletedProcess[str]:
    """Feed the hook a JSON payload on stdin and return the completed process.

    Mirrors the invocation shape used by the real PreToolUse hook.
    Returncode 0 = allow, 2 = block. ``hook`` defaults to the Fargate hook;
    the parity tests also pass the operator-local hook.
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
        ["bash", str(hook)],
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

    @pytest.mark.parametrize(
        "command",
        [
            "git stash apply 36f0f2621a0b3c4d5e6f708192a3b4c5d6e7f809",
            "git stash pop 36f0f2621a0b3c4d5e6f708192a3b4c5d6e7f809",
            "git stash apply 36f0f26",
            "git -C /some/worktree stash apply 36f0f2621a0b3c4d5e6f708192a3b4c5d6e7f809",
            "git stash apply --index 36f0f2621a0b3c4d5e6f708192a3b4c5d6e7f809",
            "git stash apply stash@{0}; git stash drop stash@{0}",
        ],
    )
    def test_git_stash_apply_with_explicit_sha_allowed(self, command: str) -> None:
        """An explicit commit SHA is as unambiguous as stash@{N} (#4683)."""
        result = run_hook(command)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "git stash apply mybranch",
            "git stash apply abc12",
            "git stash pop; git stash drop stash@{0}",
            "git stash apply && git log stash@{0}",
            "git stash apply stash@{1}; git stash pop",
        ],
    )
    def test_git_stash_non_ref_or_bare_invocation_blocked(self, command: str) -> None:
        """Every pop/apply invocation needs its own explicit ref (#4683)."""
        result = run_hook(command)
        assert result.returncode == 2, result.stderr

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
# Parity with the operator-local hook (#4703)
#
# The four safety checks live in ONE file, .claude/hooks/
# preflight_shared_checks.sh, which both hooks source. These tests fail if
# a hook stops sourcing it, stops calling one of its checks, grows its own
# inline copy of a shared check, or if the two hooks disagree on any
# shared-check command. Run: pytest scripts/tests/test_preflight_bash_fargate.py -k parity
# ---------------------------------------------------------------------------

SHARED_CHECK_DEF_RE = re.compile(r"^(preflight_check_\w+)\(\)", re.MULTILINE)

# Text that only appears in the implementation of a shared check. If either
# hook contains one of these, someone re-inlined (or half-edited) a shared
# check in that hook instead of editing the library.
INLINE_SHARED_CHECK_FINGERPRINTS = {
    "push-to-main message": "Pushing directly to main/master is not allowed",
    "worktree-add message": "git worktree add is not allowed inside an existing worktree",
    "stash message": "Bare 'git stash pop' / 'git stash apply' is not allowed",
    "stash verb regex": r"stash\s+(pop|apply)",
    "stash ref regex": r"stash@\{[0-9]+\}|[0-9a-fA-F]{7,40}",
    "cross-worktree helper call": 'preflight_cross_worktree.py" 2>/dev/null',
}

# Commands that exercise only the shared checks (no operator-only rule such
# as $(), quotes + ;, or long-running-without-timeout fires on them), so both
# hooks must return the same exit code AND the same stderr.
PARITY_CASES: list[tuple[str, str | None]] = [
    ("git push origin main", SYNTHETIC_MAIN_REPO),
    ("git push -u origin master", SYNTHETIC_MAIN_REPO),
    ("git -C /some/path push origin main", SYNTHETIC_MAIN_REPO),
    ("git push -u origin feature-branch", SYNTHETIC_MAIN_REPO),
    ("git add .githooks/pre-push", SYNTHETIC_MAIN_REPO),
    ("git worktree add ../other feature-branch", SYNTHETIC_WORKTREE),
    ("git worktree add ../foo feature-branch", SYNTHETIC_MAIN_REPO),
    ("git worktree list", SYNTHETIC_WORKTREE),
    (
        "cp foo.txt /Users/test/judgemind/judgemind-bootstrap/docs/foo.txt",
        SYNTHETIC_WORKTREE,
    ),
    (
        "cp foo.txt /Users/test/judgemind/judgemind-bootstrap/tmp/foo.txt",
        SYNTHETIC_WORKTREE,
    ),
    (
        "echo hello > /Users/test/judgemind/judgemind-bootstrap/docs/foo.txt",
        SYNTHETIC_WORKTREE,
    ),
    ("cp foo.txt /tmp/foo.txt", SYNTHETIC_WORKTREE),
    ("git stash pop", None),
    ("git stash apply", None),
    ("git -C /some/worktree stash apply", None),
    ("git stash pop stash@{0}", None),
    ("git stash apply 36f0f26", None),
    ("git stash apply --index 36f0f2621a0b3c4d5e6f708192a3b4c5d6e7f809", None),
    ("git stash apply mybranch", None),
    ("git stash pop; git stash drop stash@{0}", None),
    ("git stash apply stash@{1}; git stash pop", None),
    ("git stash list", None),
    ("git stash push -m WIP", None),
]


class TestSharedCheckParity:
    """Both hooks run the same shared checks from the same file."""

    def _defined_checks(self) -> list[str]:
        names = SHARED_CHECK_DEF_RE.findall(SHARED_LIB.read_text(encoding="utf-8"))
        assert len(names) >= 4, f"expected >= 4 shared checks, found {names}"
        return names

    @pytest.mark.parametrize("hook", [HOOK, OPERATOR_HOOK], ids=["fargate", "operator"])
    def test_parity_hook_sources_shared_library(self, hook: Path) -> None:
        text = hook.read_text(encoding="utf-8")
        assert "preflight_shared_checks.sh" in text, hook
        assert 'source "$PREFLIGHT_SHARED_LIB"' in text, hook

    def test_parity_operator_hook_calls_every_shared_check(self) -> None:
        text = OPERATOR_HOOK.read_text(encoding="utf-8")
        for name in self._defined_checks():
            assert re.search(rf"^{name} \|\| exit \$\?$", text, re.MULTILINE), (
                f"{OPERATOR_HOOK} does not call {name}"
            )

    def test_parity_fargate_hook_runs_all_shared_checks(self) -> None:
        text = HOOK.read_text(encoding="utf-8")
        assert re.search(
            r"^preflight_run_shared_checks \|\| exit \$\?$", text, re.MULTILINE
        ), f"{HOOK} does not call preflight_run_shared_checks"
        lib = SHARED_LIB.read_text(encoding="utf-8")
        body = lib[lib.index("preflight_run_shared_checks() {") :]
        for name in self._defined_checks():
            assert f"{name} || return $?" in body, (
                f"preflight_run_shared_checks does not call {name}"
            )

    @pytest.mark.parametrize("hook", [HOOK, OPERATOR_HOOK], ids=["fargate", "operator"])
    @pytest.mark.parametrize(
        "fingerprint",
        list(INLINE_SHARED_CHECK_FINGERPRINTS.values()),
        ids=list(INLINE_SHARED_CHECK_FINGERPRINTS.keys()),
    )
    def test_parity_no_inline_copy_of_shared_check(
        self, hook: Path, fingerprint: str
    ) -> None:
        """A shared check edited (or pasted) into one hook alone fails here."""
        assert fingerprint in SHARED_LIB.read_text(encoding="utf-8"), (
            f"fingerprint {fingerprint!r} no longer appears in {SHARED_LIB} — "
            "update INLINE_SHARED_CHECK_FINGERPRINTS"
        )
        assert fingerprint not in hook.read_text(encoding="utf-8"), (
            f"{hook} contains {fingerprint!r}: shared checks belong in "
            f"{SHARED_LIB}, not inline in a hook (#4703)"
        )

    @pytest.mark.parametrize(("command", "cwd"), PARITY_CASES)
    def test_parity_hooks_agree_on_shared_check_commands(
        self, command: str, cwd: str | None
    ) -> None:
        fargate = run_hook(command, cwd_override=cwd, hook=HOOK)
        operator = run_hook(command, cwd_override=cwd, hook=OPERATOR_HOOK)
        assert fargate.returncode in (0, 2), fargate.stderr
        assert (fargate.returncode, fargate.stderr) == (
            operator.returncode,
            operator.stderr,
        )

    @pytest.mark.parametrize("cwd", [SYNTHETIC_MAIN_REPO, SYNTHETIC_WORKTREE])
    def test_parity_fargate_hook_works_in_swapped_layout(
        self, tmp_path: Path, cwd: str
    ) -> None:
        """The layout daemon.py / the agent-runner swap produce: the Fargate
        hook installed as .claude/hooks/preflight-bash.sh next to the library
        and helper, with no repo checkout around it to fall back to."""
        hooks = tmp_path / "wt" / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        installed = hooks / "preflight-bash.sh"
        shutil.copy2(HOOK, installed)
        shutil.copy2(SHARED_LIB, hooks / SHARED_LIB.name)
        shutil.copy2(CROSS_WT_HELPER, hooks / CROSS_WT_HELPER.name)

        assert run_hook("git stash pop", cwd, hook=installed).returncode == 2
        assert run_hook("git stash pop stash@{0}", cwd, hook=installed).returncode == 0
        assert run_hook("git push origin main", cwd, hook=installed).returncode == 2
        blocked = run_hook(
            "cp foo.txt /Users/test/judgemind/judgemind-bootstrap/docs/foo.txt",
            SYNTHETIC_WORKTREE,
            hook=installed,
        )
        assert blocked.returncode == 2, blocked.stderr

    @pytest.mark.parametrize("hook", [HOOK, OPERATOR_HOOK], ids=["fargate", "operator"])
    def test_parity_hook_fails_closed_without_library(
        self, tmp_path: Path, hook: Path
    ) -> None:
        """No library, no safety checks — so block everything, loudly."""
        lone = tmp_path / "hooks" / "preflight-bash.sh"
        lone.parent.mkdir()
        shutil.copy2(hook, lone)
        result = run_hook("echo hello", hook=lone)
        assert result.returncode == 2
        assert "preflight hook library missing" in result.stderr

    @pytest.mark.parametrize(
        "dockerfile",
        [
            "Dockerfile.dispatcher",
            "Dockerfile.dispatcher-v3",
            "Dockerfile.dispatcher-agent-runner",
        ],
    )
    def test_parity_every_fargate_image_stages_shared_library(
        self, dockerfile: str
    ) -> None:
        text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        assert "/app/fargate-hooks/preflight-bash.sh" in text
        assert (
            "COPY .claude/hooks/preflight_shared_checks.sh "
            "/app/fargate-hooks/preflight_shared_checks.sh"
        ) in text, f"{dockerfile} stages the Fargate hook without its library"

    @pytest.mark.parametrize(
        "workflow",
        [
            "deploy-dispatcher.yml",
            "deploy-dispatcher-v3.yml",
            "deploy-agent-runner.yml",
        ],
    )
    def test_parity_image_deploys_rebuild_on_shared_library_change(
        self, workflow: str
    ) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(
            encoding="utf-8"
        )
        assert '- ".claude/hooks/preflight_shared_checks.sh"' in text, workflow
        assert '- "scripts/preflight-bash-fargate.sh"' in text, workflow


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
