#!/usr/bin/env python3
"""Tests for .claude/hooks/worktree-write-guard.sh

Run from the repo root:
    python3 .claude/hooks/test_worktree_write_guard.py

Each test feeds a JSON payload to the hook's stdin and checks the exit code.
Exit 0 = allowed, exit 2 = blocked.
"""

import json
import os
import subprocess
import sys

# Resolve paths relative to this test file's location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(SCRIPT_DIR, "worktree-write-guard.sh")

# Use a synthetic repo root for tests to avoid depending on whether
# this test file is running from a worktree or the main repo.
SYNTHETIC_REPO = "/fake/project/repo"
SYNTHETIC_WORKTREE_ID = "agent-abc123"
SYNTHETIC_WORKTREE = f"{SYNTHETIC_REPO}/.claude/worktrees/{SYNTHETIC_WORKTREE_ID}"
SYNTHETIC_WORKER = f"{SYNTHETIC_REPO}/.claude/worktrees/worker-3"

passed = 0
failed = 0


def run_test(
    description: str,
    file_path: str,
    expect_exit: int,
    cwd_override: str | None = None,
    expect_stderr_contains: str | None = None,
    tool_input_extra: dict | None = None,
) -> None:
    """Run the hook with given tool input and assert the exit code."""
    global passed, failed
    tool_input: dict = {"file_path": file_path}
    if tool_input_extra:
        tool_input.update(tool_input_extra)
    input_json = json.dumps({"tool_input": tool_input})
    env = os.environ.copy()
    if cwd_override is not None:
        env["WORKTREE_GUARD_CWD"] = cwd_override
    result = subprocess.run(
        ["bash", HOOK],
        input=input_json,
        capture_output=True,
        text=True,
        env=env,
    )
    ok = True
    if result.returncode != expect_exit:
        ok = False
    if expect_stderr_contains and expect_stderr_contains not in result.stderr:
        ok = False

    if ok:
        print(f"  PASS: {description}")
        passed += 1
    else:
        print(f"  FAIL: {description}")
        print(f"        expected exit={expect_exit}, got exit={result.returncode}")
        if result.stderr:
            print(f"        stderr: {result.stderr.strip()}")
        if expect_stderr_contains:
            print(f"        expected stderr to contain: {expect_stderr_contains!r}")
        failed += 1


# ---------------------------------------------------------------
# Block cases: agent is inside a worktree but writing to main repo
# ---------------------------------------------------------------
print("Block cases: worktree CWD writing into main repo")

run_test(
    "Write to main repo CLAUDE.md from worktree is blocked",
    f"{SYNTHETIC_REPO}/CLAUDE.md",
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains="BLOCKED:",
)

run_test(
    "Blocking message suggests the worktree-prefixed path",
    f"{SYNTHETIC_REPO}/CLAUDE.md",
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains=f"{SYNTHETIC_WORKTREE}/CLAUDE.md",
)

run_test(
    "Write to main repo packages/api/src/foo.py from worktree is blocked",
    f"{SYNTHETIC_REPO}/packages/api/src/foo.py",
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains="BLOCKED:",
)

run_test(
    "Write to main repo docs/BRAND.md from worktree is blocked",
    f"{SYNTHETIC_REPO}/docs/BRAND.md",
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains="BLOCKED:",
)

run_test(
    "Write from a nested worktree subdirectory (packages/api) still blocks",
    f"{SYNTHETIC_REPO}/packages/api/src/foo.py",
    2,
    cwd_override=f"{SYNTHETIC_WORKTREE}/packages/api",
    expect_stderr_contains="BLOCKED:",
)

run_test(
    "Write to another worktree's path is blocked",
    f"{SYNTHETIC_REPO}/.claude/worktrees/agent-other/CLAUDE.md",
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains="BLOCKED:",
)


# ---------------------------------------------------------------
# Allow cases: target inside current worktree
# ---------------------------------------------------------------
print("\nAllow cases: target inside current worktree")

run_test(
    "Write to {worktree}/CLAUDE.md allowed",
    f"{SYNTHETIC_WORKTREE}/CLAUDE.md",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Write to {worktree}/tmp/file.txt allowed",
    f"{SYNTHETIC_WORKTREE}/tmp/pr_body.txt",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Write to {worktree}/packages/api/src/foo.py allowed",
    f"{SYNTHETIC_WORKTREE}/packages/api/src/foo.py",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Write to {worktree}/.claude/hooks/foo.sh allowed (in-worktree .claude/)",
    f"{SYNTHETIC_WORKTREE}/.claude/hooks/foo.sh",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Relative file_path resolves against worktree root (allowed)",
    "packages/api/src/foo.py",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Relative file_path with ./ prefix resolves against worktree root (allowed)",
    "./tmp/pr_body.txt",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)


# ---------------------------------------------------------------
# Allow cases: target inside {repo_root}/tmp/ (cross-worktree legit)
# ---------------------------------------------------------------
print("\nAllow cases: target inside {repo_root}/tmp/")

run_test(
    "Write to {repo_root}/tmp/agent-status/agent-foo.txt allowed",
    f"{SYNTHETIC_REPO}/tmp/agent-status/agent-foo.txt",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Write to {repo_root}/tmp/some-other.txt allowed",
    f"{SYNTHETIC_REPO}/tmp/task-timings.jsonl",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)


# ---------------------------------------------------------------
# Allow cases: target outside repo entirely
# ---------------------------------------------------------------
print("\nAllow cases: target outside the repo")

run_test(
    "Write to /tmp/foo.txt allowed",
    "/tmp/foo.txt",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Write to ~/.claude/something allowed (outside repo)",
    os.path.expanduser("~/.claude/projects/x.md"),
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Write to /var/log/x.log allowed",
    "/var/log/x.log",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)


# ---------------------------------------------------------------
# Allow cases: interactive session (cwd NOT inside a worktree)
# ---------------------------------------------------------------
print("\nAllow cases: interactive session (cwd NOT in worktree)")

run_test(
    "Write to main repo CLAUDE.md from repo-root CWD allowed",
    f"{SYNTHETIC_REPO}/CLAUDE.md",
    0,
    cwd_override=SYNTHETIC_REPO,
)

run_test(
    "Write to main repo file from home dir allowed",
    f"{SYNTHETIC_REPO}/CLAUDE.md",
    0,
    cwd_override=os.path.expanduser("~"),
)

run_test(
    "Write to main repo file from /tmp allowed",
    f"{SYNTHETIC_REPO}/CLAUDE.md",
    0,
    cwd_override="/tmp",
)


# ---------------------------------------------------------------
# Allow cases from a worker-style worktree (non agent- prefix)
# ---------------------------------------------------------------
print("\nAllow cases: worker-N style worktree")

run_test(
    "Worker worktree: write to its own file allowed",
    f"{SYNTHETIC_WORKER}/src/main.py",
    0,
    cwd_override=SYNTHETIC_WORKER,
)

run_test(
    "Worker worktree: write to main repo is blocked",
    f"{SYNTHETIC_REPO}/CLAUDE.md",
    2,
    cwd_override=SYNTHETIC_WORKER,
    expect_stderr_contains="BLOCKED:",
)


# ---------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------
print("\nEdge cases")

run_test(
    "Empty file_path allowed (fail-open)",
    "",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "file_path containing /../ that escapes to main repo is blocked",
    f"{SYNTHETIC_WORKTREE}/../../../CLAUDE.md",
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains="BLOCKED:",
)

run_test(
    "file_path containing /./ inside worktree is allowed",
    f"{SYNTHETIC_WORKTREE}/./packages/./api/src/foo.py",
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)


# ---------------------------------------------------------------
# Performance
# ---------------------------------------------------------------
print("\nPerformance")


def test_performance() -> None:
    """Hook completes in under 200ms on average."""
    import time

    iterations = 10
    input_json = json.dumps(
        {"tool_input": {"file_path": f"{SYNTHETIC_WORKTREE}/src/foo.py"}}
    )
    env = os.environ.copy()
    env["WORKTREE_GUARD_CWD"] = SYNTHETIC_WORKTREE

    start = time.monotonic()
    for _ in range(iterations):
        subprocess.run(
            ["bash", HOOK],
            input=input_json,
            capture_output=True,
            text=True,
            env=env,
        )
    elapsed = time.monotonic() - start
    avg_ms = (elapsed / iterations) * 1000
    assert avg_ms < 200, (
        f"Average execution time {avg_ms:.1f}ms exceeds 200ms limit"
    )


try:
    test_performance()
    print("  PASS: Performance — under 200ms average")
    passed += 1
except AssertionError as e:
    print(f"  FAIL: Performance — {e}")
    failed += 1


# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
print(f"\n{'=' * 50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
