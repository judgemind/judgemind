#!/usr/bin/env python3
"""Tests for .claude/hooks/check-inbox.sh and check-inbox-reader.py

Run from the repo root:
    python3 .claude/hooks/test_check_inbox_hook.py

Each test creates a temporary directory structure simulating a worktree,
then runs the check-inbox-reader.py script directly (since the shell hook
delegates to it after fast-path checks).
"""

import json
import os
import subprocess
import sys
import tempfile

# Resolve the reader script relative to this test file's location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
READER = os.path.join(SCRIPT_DIR, "check-inbox-reader.py")

passed = 0
failed = 0


def run_reader(inbox_path: str) -> subprocess.CompletedProcess[str]:
    """Run the check-inbox-reader.py script with the given inbox path."""
    return subprocess.run(
        [sys.executable, READER, inbox_path],
        capture_output=True,
        text=True,
        timeout=10,
    )


def run_test(description: str, test_fn: object) -> None:
    """Run a test function and report pass/fail."""
    global passed, failed
    try:
        test_fn()
        print(f"  PASS: {description}")
        passed += 1
    except AssertionError as e:
        print(f"  FAIL: {description}")
        print(f"        {e}")
        failed += 1
    except Exception as e:
        print(f"  FAIL: {description} (exception: {e})")
        failed += 1


def test_no_inbox_file() -> None:
    """Reader exits 0 with no output when inbox doesn't exist."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "nonexistent.json")
        result = run_reader(inbox)
        # The reader will fail to open the file, but should exit cleanly
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


def test_empty_inbox_file() -> None:
    """Reader exits 0 with no output when inbox is empty."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        with open(inbox, "w") as f:
            f.write("")
        result = run_reader(inbox)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


def test_whitespace_only_inbox_truncated() -> None:
    """Reader truncates a whitespace-only inbox to avoid repeated invocations."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        with open(inbox, "w") as f:
            f.write("  \n\t  \n")
        result = run_reader(inbox)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"
        # File should be truncated to avoid repeated Python invocations
        assert os.path.getsize(inbox) == 0, "Expected whitespace-only inbox to be truncated"


def test_empty_json_array() -> None:
    """Reader exits 0 with no output when inbox is an empty JSON array."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        with open(inbox, "w") as f:
            json.dump([], f)
        result = run_reader(inbox)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"
        # File should be truncated
        assert os.path.getsize(inbox) == 0, "Expected inbox to be truncated"


def test_echoes_messages() -> None:
    """Reader prints messages prefixed with [orchestrator]."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        messages = [
            {"timestamp": "2026-03-19T12:00:00", "message": "Rebase onto main"},
            {"timestamp": "2026-03-19T12:01:00", "message": "PR #100 merged"},
        ]
        with open(inbox, "w") as f:
            json.dump(messages, f)

        result = run_reader(inbox)
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {lines}"
        assert lines[0] == "[orchestrator] Rebase onto main"
        assert lines[1] == "[orchestrator] PR #100 merged"


def test_truncates_after_reading() -> None:
    """Reader truncates the inbox file after printing messages."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        messages = [{"timestamp": "2026-01-01T00:00:00", "message": "test"}]
        with open(inbox, "w") as f:
            json.dump(messages, f)

        result = run_reader(inbox)
        assert result.returncode == 0

        # File should be truncated (empty)
        assert os.path.getsize(inbox) == 0, "Expected inbox to be truncated to 0 bytes"


def test_handles_malformed_json() -> None:
    """Reader handles malformed JSON gracefully (no crash, truncates file)."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        with open(inbox, "w") as f:
            f.write("this is not json{{{")

        result = run_reader(inbox)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"
        # File should be truncated
        assert os.path.getsize(inbox) == 0, "Expected inbox to be truncated"


def test_handles_non_dict_entries() -> None:
    """Reader skips entries that are not dicts with a 'message' field."""
    with tempfile.TemporaryDirectory() as tmp:
        inbox = os.path.join(tmp, "inbox.json")
        messages = [
            "just a string",
            {"no_message_field": True},
            {"timestamp": "2026-01-01T00:00:00", "message": "valid message"},
            42,
        ]
        with open(inbox, "w") as f:
            json.dump(messages, f)

        result = run_reader(inbox)
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}: {lines}"
        assert lines[0] == "[orchestrator] valid message"


def test_no_arguments() -> None:
    """Reader exits 0 with no arguments (no inbox path provided)."""
    result = subprocess.run(
        [sys.executable, READER],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
    assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


# --- Run all tests ---
print("check-inbox-reader.py tests")
print("=" * 50)

run_test("No inbox file — no output, exit 0", test_no_inbox_file)
run_test("Empty inbox file — no output, exit 0", test_empty_inbox_file)
run_test("Whitespace-only inbox — truncated, no output", test_whitespace_only_inbox_truncated)
run_test("Empty JSON array — no output, truncates", test_empty_json_array)
run_test("Echoes messages with [orchestrator] prefix", test_echoes_messages)
run_test("Truncates inbox after reading", test_truncates_after_reading)
run_test("Handles malformed JSON gracefully", test_handles_malformed_json)
run_test("Handles non-dict entries gracefully", test_handles_non_dict_entries)
run_test("No arguments — exits cleanly", test_no_arguments)

print(f"\n{'=' * 50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
