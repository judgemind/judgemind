#!/usr/bin/env python3
"""Tests for .claude/hooks/preflight-bash.sh

Run from the repo root:
    python3 .claude/hooks/test_preflight_hook.py

Each test feeds a JSON payload to the hook's stdin and checks the exit code.
Exit 0 = allowed, exit 2 = blocked.
"""

import json
import os
import subprocess
import sys

# Resolve paths relative to this test file's location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(SCRIPT_DIR, "preflight-bash.sh")
# Derive the repo root (two levels up from .claude/hooks/)
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

# Synthetic path for "main repo" cwd overrides.  When this test file lives
# inside a worktree (e.g., .claude/worktrees/agent-abc123/.claude/hooks/),
# REPO_ROOT itself contains ".claude/worktrees/" which makes checks 9 and 10
# falsely trigger.  Using a synthetic path that never contains that substring
# ensures the tests validate the hook's pattern matching correctly regardless
# of where the test is run from.
SYNTHETIC_MAIN_REPO = "/Users/test/judgemind/judgemind-bootstrap"

passed = 0
failed = 0


def run_test(
    description: str,
    command: str,
    expect_exit: int,
    timeout: int | None = None,
    run_in_background: bool = False,
    cwd_override: str | None = None,
) -> None:
    """Run the hook with a given command and assert the exit code."""
    global passed, failed
    tool_input: dict = {"command": command}
    if timeout is not None:
        tool_input["timeout"] = timeout
    if run_in_background:
        tool_input["run_in_background"] = True
    input_json = json.dumps({"tool_input": tool_input})
    env = os.environ.copy()
    if cwd_override is not None:
        env["PREFLIGHT_CWD"] = cwd_override
    result = subprocess.run(
        ["bash", HOOK],
        input=input_json,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode == expect_exit:
        print(f"  PASS: {description}")
        passed += 1
    else:
        print(f"  FAIL: {description} (expected exit={expect_exit}, got exit={result.returncode})")
        if result.stderr:
            print(f"        stderr: {result.stderr.strip()}")
        failed += 1


# --- Check 0: push to main ---
print("Check 0: Push to main/master")
run_test("git push origin main blocked", "git push origin main", 2)
run_test("git push -u origin main blocked", "git push -u origin main", 2)
run_test("git push origin feature-branch allowed", "git push -u origin feature-branch", 0)
run_test("git add pre-push file allowed", "git add .githooks/pre-push", 0)

# --- Check 1: $() command substitution ---
print("\nCheck 1: Dollar-paren command substitution")
run_test("$(whoami) blocked", "echo $(whoami)", 2)
run_test("simple echo allowed", "echo hello", 0)

# --- Check 2: heredocs ---
print("\nCheck 2: Heredocs")
run_test("<<EOF blocked", "cat <<EOF\nhello\nEOF", 2)
run_test("<<-EOF blocked", "cat <<-EOF\nhello\nEOF", 2)

# --- Check 3: inline python -c ---
print("\nCheck 3: Inline python -c")
run_test("python3 -c blocked", "python3 -c import os", 2)
run_test("python -c blocked", "python -c import os", 2)
run_test("python3 script.py allowed", "python3 /tmp/script.py", 0)

# --- Check 4: quoted strings with && or ; ---
print("\nCheck 4: Quoted strings with compound operators")
SQ = chr(39)  # single quote
DQ = chr(34)  # double quote
run_test(
    "single-quoted string with && blocked",
    f"echo {SQ}hello{SQ} && echo world",
    2,
)
run_test("no quotes with && allowed", "ls && pwd", 0)

# --- Check 5: cd in compound commands ---
print("\nCheck 5: cd in compound commands")
run_test("cd && cmd blocked", "cd /tmp && ls", 2)

# --- Check 6: empty quotes before flags (bypass attempts) ---
print("\nCheck 6: Empty quotes before flags")

# Bypass attempts — should be BLOCKED
run_test(
    "empty single quotes before -rf blocked",
    f"rm {SQ}{SQ} -rf /important",
    2,
)
run_test(
    "empty double quotes before --force blocked",
    f"git checkout {DQ}{DQ} --force",
    2,
)
run_test(
    "empty single quotes before --delete blocked",
    f"cmd {SQ}{SQ} --delete",
    2,
)

# Legitimate uses — should be ALLOWED
run_test(
    "jq expression with dash in string allowed",
    f"gh issue list --json number,title --jq {SQ}.[] | .number | tostring + {DQ} - {DQ} + .title{SQ}",
    0,
)
run_test(
    "SQL != empty string allowed",
    f"scripts/dev-db-query.sh SELECT * FROM rulings WHERE title != {SQ}{SQ}",
    0,
)
run_test(
    "empty double quotes as argument allowed",
    f"echo test {DQ}{DQ} foo",
    0,
)
run_test(
    "jq with quoted dash separator allowed",
    f"gh pr view 123 --json state,title --jq {SQ}.state + {DQ} - {DQ} + .title{SQ}",
    0,
)
run_test(
    "empty string at end of command allowed",
    f"echo {SQ}{SQ}",
    0,
)

# Normal usage should pass
run_test("normal command no quotes", "git status", 0)
run_test("normal command with path", "git -C /path/to/repo status", 0)
run_test("git commit with -F flag", "git commit -F /tmp/msg.txt", 0)

# --- Check 8: long-running commands without timeout ---
print("\nCheck 8: Long-running commands without timeout")

# Commands that SHOULD be blocked without timeout
run_test(
    "pytest without timeout blocked",
    ".venv/bin/pytest tests/ -v",
    2,
)
run_test(
    "gh run watch without timeout blocked",
    "gh run watch 12345 --repo judgemind/judgemind --interval 60 --exit-status",
    2,
)
run_test(
    "pip install without timeout blocked",
    ".venv/bin/pip install -e .[dev] --quiet",
    2,
)
run_test(
    "npm install without timeout blocked",
    "npm install",
    2,
)
run_test(
    "npm run build without timeout blocked",
    "npm run build",
    2,
)
run_test(
    "ruff check on src/ without timeout blocked",
    ".venv/bin/ruff check src/ tests/",
    2,
)
run_test(
    "terraform apply without timeout blocked",
    "terraform -chdir=infra/terraform/environments/dev apply -auto-approve",
    2,
)

# Commands that SHOULD be blocked with too-low timeout
run_test(
    "pytest with timeout 120000 blocked (too low)",
    ".venv/bin/pytest tests/ -v",
    2,
    timeout=120000,
)
run_test(
    "pip install with timeout 60000 blocked (too low)",
    ".venv/bin/pip install -e .[dev]",
    2,
    timeout=60000,
)

# Commands that SHOULD be allowed with sufficient timeout
run_test(
    "pytest with timeout 1200000 allowed",
    ".venv/bin/pytest tests/ -v",
    0,
    timeout=1200000,
)
run_test(
    "gh run watch with timeout 1200000 allowed",
    "gh run watch 12345 --repo judgemind/judgemind --interval 60",
    0,
    timeout=1200000,
)
run_test(
    "pip install with timeout 300000 allowed (at minimum)",
    ".venv/bin/pip install -e .[dev]",
    0,
    timeout=300000,
)
run_test(
    "npm install with timeout 1200000 allowed",
    "npm install",
    0,
    timeout=1200000,
)
run_test(
    "npm run build with timeout 1200000 allowed",
    "npm run build",
    0,
    timeout=1200000,
)
run_test(
    "ruff check src/ with timeout 1200000 allowed",
    ".venv/bin/ruff check src/",
    0,
    timeout=1200000,
)
run_test(
    "terraform apply with timeout 1200000 allowed",
    "terraform -chdir=infra/terraform/environments/dev apply -auto-approve",
    0,
    timeout=1200000,
)

# Commands that SHOULD be allowed with run_in_background=true even without timeout
# (only when NOT in a worktree — see check 9)
run_test(
    "pytest with run_in_background=true allowed (no timeout needed, main repo)",
    ".venv/bin/pytest tests/ -v",
    0,
    run_in_background=True,
    cwd_override=SYNTHETIC_MAIN_REPO,
)
run_test(
    "gh run watch with run_in_background=true allowed (main repo)",
    "gh run watch 12345 --interval 60",
    0,
    run_in_background=True,
    cwd_override=SYNTHETIC_MAIN_REPO,
)

# Commands that should NOT trigger the timeout check
run_test(
    "ruff check on a single file (no large path) allowed without timeout",
    ".venv/bin/ruff check src/courts/ca/la.py",
    0,
)
run_test(
    "npm run lint allowed without timeout",
    "npm run lint",
    0,
)
run_test(
    "npm test allowed without timeout",
    "npm test",
    0,
)
run_test(
    "git status allowed without timeout",
    "git status",
    0,
)
run_test(
    "ruff format allowed without timeout",
    ".venv/bin/ruff format --check src/ tests/",
    0,
)

# --- Check 9: run_in_background inside worktree subagents ---
print("\nCheck 9: run_in_background inside worktree subagents")

# Use synthetic paths so the tests work regardless of whether this test file
# is run from the main repo or from inside a worktree.  The hook checks for
# ".claude/worktrees/" in the cwd path — it doesn't need real directories.
MAIN_REPO_CWD = SYNTHETIC_MAIN_REPO
WORKTREE_CWD = SYNTHETIC_MAIN_REPO + "/.claude/worktrees/agent-abc123"

# Commands with run_in_background=true inside a worktree — should be BLOCKED
run_test(
    "gh run watch with run_in_background in worktree blocked",
    "gh run watch 12345 --repo judgemind/judgemind --interval 60",
    2,
    timeout=1200000,
    run_in_background=True,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "pytest with run_in_background in worktree blocked",
    ".venv/bin/pytest tests/ -v",
    2,
    timeout=1200000,
    run_in_background=True,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "simple command with run_in_background in worktree blocked",
    "echo hello",
    2,
    run_in_background=True,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git status with run_in_background in worktree blocked",
    "git status",
    2,
    run_in_background=True,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "run_in_background in worker-N worktree blocked",
    "echo test",
    2,
    run_in_background=True,
    cwd_override=SYNTHETIC_MAIN_REPO + "/.claude/worktrees/worker-3",
)

# Commands with run_in_background=true from main repo — should be ALLOWED
run_test(
    "gh run watch with run_in_background from main repo allowed",
    "gh run watch 12345 --interval 60",
    0,
    run_in_background=True,
    cwd_override=MAIN_REPO_CWD,
)

# Commands WITHOUT run_in_background inside a worktree — should be ALLOWED
run_test(
    "gh run watch without run_in_background in worktree allowed (with timeout)",
    "gh run watch 12345 --repo judgemind/judgemind --interval 60",
    0,
    timeout=1200000,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "simple command without run_in_background in worktree allowed",
    "echo hello",
    0,
    cwd_override=WORKTREE_CWD,
)

# --- Check 10: git worktree add inside an existing worktree ---
print("\nCheck 10: git worktree add inside worktree subagents")

# Commands that SHOULD be blocked — git worktree add from inside a worktree
run_test(
    "git worktree add blocked inside worktree",
    "git worktree add /path/to/new-worktree feature-branch",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git worktree add with -b flag blocked inside worktree",
    "git worktree add -b new-branch /path/to/new-worktree",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git -C <path> worktree add blocked inside worktree",
    "git -C /some/repo worktree add /path/to/new-worktree",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git worktree add blocked inside nested worktree",
    "git worktree add ../agent-new-worktree new-branch",
    2,
    cwd_override=SYNTHETIC_MAIN_REPO + "/.claude/worktrees/agent-abc123/.claude/worktrees/agent-def456",
)

# Commands that SHOULD be allowed — git worktree add from main repo
run_test(
    "git worktree add allowed from main repo",
    "git worktree add /path/to/new-worktree feature-branch",
    0,
    cwd_override=MAIN_REPO_CWD,
)
run_test(
    "git worktree add with -b flag allowed from main repo",
    "git worktree add -b new-branch /path/to/new-worktree",
    0,
    cwd_override=MAIN_REPO_CWD,
)

# Commands that SHOULD be allowed — git worktree list/remove/prune from worktree
run_test(
    "git worktree list allowed inside worktree",
    "git worktree list",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git worktree remove allowed inside worktree",
    "git worktree remove /path/to/old-worktree",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git worktree prune allowed inside worktree",
    "git worktree prune",
    0,
    cwd_override=WORKTREE_CWD,
)

# --- Summary ---
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
