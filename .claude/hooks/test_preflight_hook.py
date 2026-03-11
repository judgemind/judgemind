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

# Resolve the hook relative to this test file's location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(SCRIPT_DIR, "preflight-bash.sh")

passed = 0
failed = 0


def run_test(description: str, command: str, expect_exit: int) -> None:
    """Run the hook with a given command and assert the exit code."""
    global passed, failed
    input_json = json.dumps({"tool_input": {"command": command}})
    result = subprocess.run(
        ["bash", HOOK],
        input=input_json,
        capture_output=True,
        text=True,
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

# --- Summary ---
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
