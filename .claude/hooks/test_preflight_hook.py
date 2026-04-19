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

# Issue #2589 regression tests: semicolons / && inside quoted arguments must
# NOT trigger the check. The check exists to prevent shell-level compound
# commands that mix quoted strings with &&/;, not to block a `;` that is part
# of a single argument value.
run_test(
    "semicolon inside double-quoted --title arg allowed (#2589 AC1)",
    f"gh pr create --title {DQ}feat(infra): extend apply_immediately to ElastiCache; document OpenSearch (#2581){DQ} --body-file tmp/pr_body.txt",
    0,
)
run_test(
    "semicolon inside single-quoted arg allowed (#2589)",
    f"gh pr create --title {SQ}feat: foo; bar{SQ} --body-file tmp/b.txt",
    0,
)
run_test(
    "&& inside double-quoted arg allowed (#2589)",
    f"echo {DQ}x && y{DQ}",
    0,
)
run_test(
    "&& inside single-quoted arg allowed (#2589)",
    f"echo {SQ}x && y{SQ}",
    0,
)
run_test(
    "quoted args on both sides of real && still blocked (#2589 AC2)",
    f"echo {DQ}quoted{DQ} && echo {DQ}more{DQ}",
    2,
)
run_test(
    "semicolon after quoted arg is still a real compound-command (#2589 AC2)",
    f"echo {DQ}hi{DQ}; echo bye",
    2,
)
run_test(
    "unquoted cmd1 ; cmd2 still treated as compound (not blocked by check 4, quotes absent)",
    "echo hi ; echo bye",
    0,
)
run_test(
    "real semicolon-separated with quotes on one side still blocked (#2589 AC2)",
    f"echo hi; echo {SQ}bye{SQ}",
    2,
)

# --- Check 5: cd in compound commands ---
print("\nCheck 5: cd in compound commands")
run_test("cd && cmd blocked", "cd /tmp && ls", 2)

# Issue #2589 regression tests for check 5: a literal `cd` inside a quoted
# argument is not an actual `cd` shell command, and &&/; inside a quoted
# argument are not compound-command operators.
run_test(
    "cd inside double-quoted arg with && elsewhere in quoted arg allowed (#2589 AC3)",
    f"gh issue create --title {DQ}docs: explain how to cd into foo && bar{DQ}",
    0,
)
run_test(
    "cd inside single-quoted arg allowed (#2589 AC3)",
    f"some_cmd --name {SQ}cd into foo{SQ}",
    0,
)
run_test(
    "cd inside quoted arg with unquoted && still blocked by cd check (#2589)",
    f"cd /tmp && some_cmd --name {SQ}foo{SQ}",
    2,
)

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

# Issue #2049: SQL empty-string patterns passed to dev-db-query.sh must be allowed.
# These are regression tests for the specific patterns called out in the issue's
# acceptance criteria.  Empty-string comparisons (`= ''`) and escaped-quote
# patterns (`''''`) are legitimate PostgreSQL syntax.  The check-6 regex only
# triggers on `'' -` (empty quotes followed by whitespace then a dash), so these
# SQL patterns are outside its scope — but explicit tests guard against
# regressions if the regex is ever widened.
run_test(
    "SQL = '' inside dev-db-query.sh allowed (#2049)",
    f"scripts/dev-db-query.sh {DQ}SELECT CASE WHEN col = {SQ}{SQ} THEN 1 END FROM t{DQ}",
    0,
)
run_test(
    "SQL = '' with COUNT inside dev-db-query.sh allowed (#2049)",
    f"scripts/dev-db-query.sh {DQ}SELECT COUNT(*) FROM rulings WHERE ruling_text = {SQ}{SQ}{DQ}",
    0,
)
run_test(
    "SQL escaped quote '''' inside dev-db-query.sh allowed (#2049)",
    f"scripts/dev-db-query.sh {DQ}SELECT CASE WHEN col = {SQ}{SQ}{SQ}{SQ} THEN 1 END FROM t{DQ}",
    0,
)
run_test(
    "SQL CASE WHEN col = '' THEN 1 via dev-db-query.sh allowed (#2049)",
    (
        f"scripts/dev-db-query.sh "
        f"{DQ}SELECT id, CASE WHEN ruling_text = {SQ}{SQ} THEN 1 ELSE 0 END FROM rulings{DQ}"
    ),
    0,
)
run_test(
    "SQL LENGTH(col) comparison allowed (unrelated but common pattern)",
    f"scripts/dev-db-query.sh {DQ}SELECT COUNT(*) FROM t WHERE LENGTH(col) = 0{DQ}",
    0,
)

# Regression guard: truly obfuscated patterns outside a SQL context must still
# be blocked, per AC3 of issue #2049.
run_test(
    "obfuscated '' --force outside SQL still blocked (#2049)",
    f"somecmd {SQ}{SQ} --force",
    2,
)
run_test(
    "obfuscated '' -rf outside SQL still blocked (#2049)",
    f"rm {SQ}{SQ} -rf /tmp/important",
    2,
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

# --- Check 11: cross-worktree writes via Bash (cp/mv/tar/redirection) ---
# Extends the Edit/Write-focused worktree-write-guard.sh (#2440) to the Bash
# tool. See issue #2455.
print("\nCheck 11: Cross-worktree writes via Bash")

# cp: destination inside main repo but outside worktree — should be BLOCKED
run_test(
    "cp to main repo CLAUDE.md from worktree blocked (#2455 AC1)",
    f"cp tmp/x.txt {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "cp -r to main repo docs/ from worktree blocked",
    f"cp -r tmp/out {SYNTHETIC_MAIN_REPO}/docs",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "cp to main repo nested path from worktree blocked",
    f"cp tmp/x.txt {SYNTHETIC_MAIN_REPO}/packages/api/src/foo.py",
    2,
    cwd_override=WORKTREE_CWD,
)

# cp: destination inside current worktree — should be ALLOWED
run_test(
    "cp to within worktree allowed (#2455 AC2)",
    f"cp tmp/x.txt {WORKTREE_CWD}/CLAUDE.md",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "cp to deep path inside worktree allowed",
    f"cp tmp/x.txt {WORKTREE_CWD}/packages/api/src/foo.py",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "cp with relative destination from worktree allowed",
    "cp tmp/x.txt docs/x.txt",
    0,
    cwd_override=WORKTREE_CWD,
)

# cp: destination inside {repo_root}/tmp/ — should be ALLOWED
run_test(
    "cp to repo_root/tmp/agent-status/ from worktree allowed (#2455 AC3)",
    f"cp tmp/x.txt {SYNTHETIC_MAIN_REPO}/tmp/agent-status/foo.txt",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "cp to repo_root/tmp/task-timings.jsonl from worktree allowed",
    f"cp tmp/x.txt {SYNTHETIC_MAIN_REPO}/tmp/task-timings.jsonl",
    0,
    cwd_override=WORKTREE_CWD,
)

# cp: destination outside repo entirely — should be ALLOWED
run_test(
    "cp to /tmp/foo.txt from worktree allowed",
    "cp src.txt /tmp/foo.txt",
    0,
    cwd_override=WORKTREE_CWD,
)

# cp: interactive session (cwd NOT in a worktree) — should be ALLOWED
run_test(
    "cp to main repo CLAUDE.md from main repo cwd allowed (#2455 AC4)",
    f"cp tmp/x.txt {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    0,
    cwd_override=MAIN_REPO_CWD,
)
run_test(
    "cp to main repo CLAUDE.md from /tmp cwd allowed",
    f"cp src {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    0,
    cwd_override="/tmp",
)

# mv: same rules as cp (#2455 AC5)
run_test(
    "mv to main repo from worktree blocked (#2455 AC5)",
    f"mv foo.txt {SYNTHETIC_MAIN_REPO}/foo.txt",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "mv to within worktree allowed",
    f"mv foo.txt {WORKTREE_CWD}/foo.txt",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "mv to repo_root/tmp allowed",
    f"mv foo.txt {SYNTHETIC_MAIN_REPO}/tmp/foo.txt",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "mv to main repo from main repo cwd allowed",
    f"mv foo.txt {SYNTHETIC_MAIN_REPO}/foo.txt",
    0,
    cwd_override=MAIN_REPO_CWD,
)

# tar -C / --directory=: destination directory inside main repo — BLOCKED
run_test(
    "tar -xf -C into main repo from worktree blocked",
    f"tar -xf archive.tgz -C {SYNTHETIC_MAIN_REPO}",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "tar -xf -C into main repo subdir from worktree blocked",
    f"tar -xf archive.tgz -C {SYNTHETIC_MAIN_REPO}/docs",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "tar -xf --directory= into main repo from worktree blocked",
    f"tar -xf archive.tgz --directory={SYNTHETIC_MAIN_REPO}",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "tar -xf -C into worktree allowed",
    f"tar -xf archive.tgz -C {WORKTREE_CWD}/out",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "tar -xf -C into /tmp allowed (outside repo)",
    "tar -xf archive.tgz -C /tmp",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "tar -xf -C into main repo from main repo cwd allowed",
    f"tar -xf archive.tgz -C {SYNTHETIC_MAIN_REPO}/docs",
    0,
    cwd_override=MAIN_REPO_CWD,
)

# Shell redirection > and >>: destination inside main repo — BLOCKED (#2455 AC5)
run_test(
    "echo > main repo file from worktree blocked (#2455 AC5)",
    f"echo content > {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "echo >> main repo file from worktree blocked (#2455 AC5)",
    f"echo content >> {SYNTHETIC_MAIN_REPO}/CHANGELOG.md",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "echo > worktree file allowed",
    f"echo content > {WORKTREE_CWD}/tmp/file.txt",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "echo > /tmp file allowed (outside repo)",
    "echo content > /tmp/file.txt",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "echo > repo_root/tmp file allowed",
    f"echo content > {SYNTHETIC_MAIN_REPO}/tmp/agent-status/s.txt",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "echo > main repo from main repo cwd allowed",
    f"echo content > {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    0,
    cwd_override=MAIN_REPO_CWD,
)

# Cross-worktree writes (into another worktree) — should be BLOCKED
run_test(
    "cp to another worktree's file from current worktree blocked",
    f"cp tmp/x.txt {SYNTHETIC_MAIN_REPO}/.claude/worktrees/agent-other/CLAUDE.md",
    2,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "echo > another worktree's file from current worktree blocked",
    f"echo x > {SYNTHETIC_MAIN_REPO}/.claude/worktrees/agent-other/file",
    2,
    cwd_override=WORKTREE_CWD,
)

# Read-only / non-write commands that include main repo paths — should be ALLOWED
# (Check 11 must not block these — they are not write verbs.)
run_test(
    "ls main repo file from worktree allowed (not a write verb)",
    f"ls {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "cat main repo file from worktree allowed (not a write verb)",
    f"cat {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    0,
    cwd_override=WORKTREE_CWD,
)
run_test(
    "git log with main repo path allowed",
    f"git log {SYNTHETIC_MAIN_REPO}/CLAUDE.md",
    0,
    cwd_override=WORKTREE_CWD,
)

# --- Check 12: bare git stash pop / git stash apply (#2749) ---
# The stash list is shared across all worktrees in a clone, so a bare `git
# stash pop` can silently apply a stash created by a different worktree — see
# #2749 for the incident. Require an explicit stash@{N} reference.
print("\nCheck 12: bare git stash pop / git stash apply (#2749)")

# Bare pop / apply — should be BLOCKED
run_test(
    "bare 'git stash pop' blocked (#2749 AC1)",
    "git stash pop",
    2,
)
run_test(
    "bare 'git stash apply' blocked (#2749 AC1)",
    "git stash apply",
    2,
)
run_test(
    "'git stash pop --quiet' without ref blocked (flags don't count as refs)",
    "git stash pop --quiet",
    2,
)
run_test(
    "'git stash apply --index' without ref blocked",
    "git stash apply --index",
    2,
)
run_test(
    "'git -C /path stash pop' without ref blocked",
    "git -C /some/worktree stash pop",
    2,
)
run_test(
    "'git -C /path stash apply' without ref blocked",
    "git -C /some/worktree stash apply",
    2,
)

# Explicit stash@{N} reference — should be ALLOWED
run_test(
    "'git stash pop stash@{0}' with explicit ref allowed (#2749 AC2)",
    "git stash pop stash@{0}",
    0,
)
run_test(
    "'git stash pop stash@{2}' with higher index allowed",
    "git stash pop stash@{2}",
    0,
)
run_test(
    "'git stash apply stash@{0}' with explicit ref allowed",
    "git stash apply stash@{0}",
    0,
)
run_test(
    "'git stash pop --quiet stash@{0}' with ref + flag allowed",
    "git stash pop --quiet stash@{0}",
    0,
)
run_test(
    "'git -C /path stash pop stash@{0}' with -C + ref allowed",
    "git -C /some/worktree stash pop stash@{0}",
    0,
)

# Non-pop/apply stash subcommands — should be ALLOWED even without a ref
run_test(
    "'git stash list' allowed (not a pop/apply)",
    "git stash list",
    0,
)
run_test(
    "'git stash show' allowed",
    "git stash show",
    0,
)
run_test(
    "'git stash push' allowed (creates, does not apply)",
    "git stash push -m 'WIP on my-branch'",
    0,
)
run_test(
    "'git stash push --include-untracked' allowed",
    "git stash push --include-untracked -m 'WIP'",
    0,
)
run_test(
    "'git stash drop stash@{0}' allowed (drop is not apply)",
    "git stash drop stash@{0}",
    0,
)
run_test(
    "'git stash drop' without ref allowed (drops top of stack, not apply)",
    "git stash drop",
    0,
)
run_test(
    "'git stash clear' allowed",
    "git stash clear",
    0,
)
# Unrelated commands that happen to contain the word 'stash' in a filename or
# string must NOT trigger the check.
run_test(
    "'git add stash-helper.sh' allowed (filename contains stash)",
    "git add scripts/stash-helper.sh",
    0,
)
run_test(
    "'git log --grep=stash' allowed (flag value contains stash)",
    "git log --grep=stash",
    0,
)
# The string "git stash pop" inside a quoted argument (e.g. PR title,
# commit message) must not falsely trigger the check. Check 12 uses
# $STRIPPED_COMMAND to exclude quoted content.
run_test(
    "'git stash pop' inside double-quoted PR title allowed (#2749)",
    f"gh pr create --title {DQ}block bare git stash pop / apply{DQ} --body-file tmp/b.txt",
    0,
)
run_test(
    "'git stash apply' inside single-quoted commit message allowed (#2749)",
    f"git commit -m {SQ}docs: mention git stash apply in note{SQ}",
    0,
)
run_test(
    "'git stash pop' inside body-file arg allowed (#2749)",
    f"echo {DQ}description of git stash pop{DQ} > tmp/body.txt",
    0,
)

# --- Summary ---
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
