#!/usr/bin/env python3
"""Tests for .claude/hooks/agent-worktree-guard.sh

Run from the repo root:
    python3 .claude/hooks/test_agent_worktree_guard.py

Each test feeds a JSON payload to the hook's stdin and checks the exit code.
Exit 0 = allowed, exit 2 = blocked.
"""

import json
import os
import subprocess
import sys

# Resolve paths relative to this test file's location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(SCRIPT_DIR, "agent-worktree-guard.sh")

# Use a synthetic repo root for tests to avoid depending on whether
# this test file is running from a worktree or the main repo.
SYNTHETIC_REPO = "/fake/project/repo"
SYNTHETIC_WORKTREE = f"{SYNTHETIC_REPO}/.claude/worktrees/agent-abc123"
SYNTHETIC_WORKER = f"{SYNTHETIC_REPO}/.claude/worktrees/worker-3"
SYNTHETIC_NESTED = f"{SYNTHETIC_REPO}/.claude/worktrees/agent-abc123/.claude/worktrees/agent-def456"

passed = 0
failed = 0


def run_test(
    description: str,
    tool_input: dict,
    expect_exit: int,
    cwd_override: str | None = None,
    expect_stderr_contains: str | None = None,
) -> None:
    """Run the hook with given tool input and assert the exit code."""
    global passed, failed
    input_json = json.dumps({"tool_input": tool_input})
    env = os.environ.copy()
    if cwd_override is not None:
        env["AGENT_GUARD_CWD"] = cwd_override
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


# --- Block cases: cwd inside worktree AND isolation: worktree ---
print("Block cases: cwd in worktree + isolation: worktree")

run_test(
    "Agent with isolation:worktree from agent worktree blocked",
    {"prompt": "/task #42", "isolation": "worktree"},
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains="BLOCKED: cwd is inside a worktree",
)

run_test(
    "Agent with isolation:worktree from worker worktree blocked",
    {"prompt": "/task #42", "isolation": "worktree"},
    2,
    cwd_override=SYNTHETIC_WORKER,
    expect_stderr_contains="BLOCKED: cwd is inside a worktree",
)

run_test(
    "Agent with isolation:worktree from nested worktree blocked",
    {"prompt": "/task #42", "isolation": "worktree"},
    2,
    cwd_override=SYNTHETIC_NESTED,
    expect_stderr_contains="BLOCKED: cwd is inside a worktree",
)

run_test(
    "Agent with isolation:worktree from worktree subdirectory blocked",
    {"prompt": "/task #42", "isolation": "worktree"},
    2,
    cwd_override=f"{SYNTHETIC_WORKTREE}/packages/api",
    expect_stderr_contains="BLOCKED: cwd is inside a worktree",
)

run_test(
    "Block message includes repo root path for cd hint",
    {"prompt": "/task #99", "isolation": "worktree"},
    2,
    cwd_override=SYNTHETIC_WORKTREE,
    expect_stderr_contains=f"Run cd {SYNTHETIC_REPO}",
)


# --- Allow cases: cwd NOT in worktree ---
print("\nAllow cases: cwd NOT in worktree")

run_test(
    "Agent with isolation:worktree from main repo allowed",
    {"prompt": "/task #42", "isolation": "worktree"},
    0,
    cwd_override=SYNTHETIC_REPO,
)

run_test(
    "Agent with isolation:worktree from home directory allowed",
    {"prompt": "/task #42", "isolation": "worktree"},
    0,
    cwd_override=os.path.expanduser("~"),
)

run_test(
    "Agent with isolation:worktree from /tmp allowed",
    {"prompt": "/task #42", "isolation": "worktree"},
    0,
    cwd_override="/tmp",
)


# --- Allow cases: no isolation or different isolation ---
print("\nAllow cases: no isolation or different isolation value")

run_test(
    "Agent without isolation from worktree allowed",
    {"prompt": "/ralph"},
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Agent with empty isolation from worktree allowed",
    {"prompt": "/ralph", "isolation": ""},
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Agent with isolation:none from worktree allowed",
    {"prompt": "/tdd", "isolation": "none"},
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Agent without isolation from main repo allowed",
    {"prompt": "/ralph"},
    0,
    cwd_override=SYNTHETIC_REPO,
)


# --- Edge cases ---
print("\nEdge cases")

run_test(
    "Empty tool_input from worktree allowed",
    {},
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)

run_test(
    "Empty tool_input from main repo allowed",
    {},
    0,
    cwd_override=SYNTHETIC_REPO,
)

run_test(
    "cwd contains worktrees in non-.claude path — allowed",
    {"prompt": "/task #42", "isolation": "worktree"},
    0,
    cwd_override="/some/project/worktrees/branch-1",
)

run_test(
    "cwd is .claude dir without worktrees — allowed",
    {"prompt": "/task #42", "isolation": "worktree"},
    0,
    cwd_override=f"{SYNTHETIC_REPO}/.claude/hooks",
)

run_test(
    "Malformed JSON — gracefully allows (fail-open)",
    {},
    0,
    cwd_override=SYNTHETIC_WORKTREE,
)


# --- Performance ---
print("\nPerformance")


def test_performance() -> None:
    """Hook completes in under 200ms on average."""
    import time

    iterations = 10
    input_json = json.dumps(
        {"tool_input": {"prompt": "/task #42", "isolation": "worktree"}}
    )
    env = os.environ.copy()
    env["AGENT_GUARD_CWD"] = SYNTHETIC_REPO

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
    print(f"  PASS: Performance — under 200ms average")
    passed += 1
except AssertionError as e:
    print(f"  FAIL: Performance — {e}")
    failed += 1


# --- Summary ---
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
