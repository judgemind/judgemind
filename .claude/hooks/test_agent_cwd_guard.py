#!/usr/bin/env python3
"""Tests for .claude/hooks/agent-cwd-guard.sh

Run from the repo root:
    python3 .claude/hooks/test_agent_cwd_guard.py

Each test runs the hook script from a specific working directory and checks
whether it prints a warning or stays silent.
Exit 0 = allowed (always — the hook never blocks).
"""

import os
import subprocess
import sys
import tempfile

# Resolve paths relative to this test file's location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(SCRIPT_DIR, "agent-cwd-guard.sh")

passed = 0
failed = 0


def _realpath(path: str) -> str:
    """Resolve symlinks (e.g. /var -> /private/var on macOS)."""
    return os.path.realpath(path)


def run_hook(cwd: str) -> subprocess.CompletedProcess[str]:
    """Run the agent-cwd-guard.sh hook from the given working directory."""
    return subprocess.run(
        ["bash", HOOK],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
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


# --- Tests ---


def test_no_drift_arbitrary_dir() -> None:
    """No warning when cwd is an arbitrary directory (not a worktree)."""
    with tempfile.TemporaryDirectory() as tmp:
        result = run_hook(tmp)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


def test_no_drift_home_dir() -> None:
    """No warning when cwd is the user's home directory."""
    result = run_hook(os.path.expanduser("~"))
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
    assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


def test_drift_detected_agent_worktree() -> None:
    """Warning printed when cwd is inside .claude/worktrees/agent-xxx/."""
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _realpath(tmp)
        fake_worktree = os.path.join(tmp, ".claude", "worktrees", "agent-abc123")
        os.makedirs(fake_worktree)
        result = run_hook(fake_worktree)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert "[cwd-guard] WARNING:" in result.stdout, (
            f"Expected warning in stdout, got: {result.stdout!r}"
        )
        assert f"Run: cd {real_tmp}" in result.stdout, (
            f"Expected repo root in warning, got: {result.stdout!r}"
        )


def test_drift_detected_worker_worktree() -> None:
    """Warning printed when cwd is inside .claude/worktrees/worker-N/."""
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _realpath(tmp)
        fake_worktree = os.path.join(tmp, ".claude", "worktrees", "worker-3")
        os.makedirs(fake_worktree)
        result = run_hook(fake_worktree)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert "[cwd-guard] WARNING:" in result.stdout, (
            f"Expected warning in stdout, got: {result.stdout!r}"
        )
        assert f"Run: cd {real_tmp}" in result.stdout, (
            f"Expected repo root in warning, got: {result.stdout!r}"
        )


def test_drift_detected_subdirectory() -> None:
    """Warning printed when cwd is a subdirectory within a worktree."""
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _realpath(tmp)
        subdir = os.path.join(
            tmp, ".claude", "worktrees", "agent-xyz789", "packages", "api"
        )
        os.makedirs(subdir)
        result = run_hook(subdir)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert "[cwd-guard] WARNING:" in result.stdout, (
            f"Expected warning in stdout, got: {result.stdout!r}"
        )
        assert f"Run: cd {real_tmp}" in result.stdout, (
            f"Expected repo root in warning, got: {result.stdout!r}"
        )


def test_always_exits_zero() -> None:
    """Hook always exits 0, even when drift is detected."""
    with tempfile.TemporaryDirectory() as tmp:
        fake_worktree = os.path.join(tmp, ".claude", "worktrees", "agent-test")
        os.makedirs(fake_worktree)
        result = run_hook(fake_worktree)
        assert result.returncode == 0, (
            f"Hook must always exit 0, got {result.returncode}"
        )


def test_no_drift_claude_dir_without_worktrees() -> None:
    """No warning when cwd is inside .claude/ but not in worktrees/."""
    with tempfile.TemporaryDirectory() as tmp:
        claude_dir = os.path.join(tmp, ".claude", "hooks")
        os.makedirs(claude_dir)
        result = run_hook(claude_dir)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


def test_warning_format() -> None:
    """Warning message has the expected format."""
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = _realpath(tmp)
        fake_worktree = os.path.join(tmp, ".claude", "worktrees", "agent-format")
        os.makedirs(fake_worktree)
        result = run_hook(fake_worktree)
        expected_prefix = "[cwd-guard] WARNING: cwd drifted to "
        assert result.stdout.startswith(expected_prefix), (
            f"Expected warning to start with {expected_prefix!r}, "
            f"got: {result.stdout!r}"
        )
        assert result.stdout.strip().endswith(f"Run: cd {real_tmp}"), (
            f"Expected warning to end with 'Run: cd {real_tmp}', "
            f"got: {result.stdout!r}"
        )


def test_no_false_positive_worktrees_in_name() -> None:
    """No warning when 'worktrees' appears in dir name but not as .claude/worktrees/."""
    with tempfile.TemporaryDirectory() as tmp:
        # A directory named "worktrees" not under .claude/
        worktrees_dir = os.path.join(tmp, "my-project", "worktrees", "branch-1")
        os.makedirs(worktrees_dir)
        result = run_hook(worktrees_dir)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
        assert result.stdout == "", f"Expected no output, got: {result.stdout!r}"


def test_performance() -> None:
    """Hook completes in under 100ms."""
    import time

    iterations = 10
    with tempfile.TemporaryDirectory() as tmp:
        fake_worktree = os.path.join(tmp, ".claude", "worktrees", "agent-perf")
        os.makedirs(fake_worktree)
        start = time.monotonic()
        for _ in range(iterations):
            run_hook(fake_worktree)
        elapsed = time.monotonic() - start
        avg_ms = (elapsed / iterations) * 1000
        # Allow some headroom — the acceptance criterion is <100ms
        assert avg_ms < 100, (
            f"Average execution time {avg_ms:.1f}ms exceeds 100ms limit"
        )


# --- Run all tests ---
print("agent-cwd-guard.sh tests")
print("=" * 50)

run_test("No drift — arbitrary directory", test_no_drift_arbitrary_dir)
run_test("No drift — home directory", test_no_drift_home_dir)
run_test("Drift detected — agent worktree", test_drift_detected_agent_worktree)
run_test("Drift detected — worker worktree", test_drift_detected_worker_worktree)
run_test("Drift detected — subdirectory in worktree", test_drift_detected_subdirectory)
run_test("Always exits zero", test_always_exits_zero)
run_test("No drift — .claude/ without worktrees/", test_no_drift_claude_dir_without_worktrees)
run_test("Warning format is correct", test_warning_format)
run_test("No false positive — worktrees in other path", test_no_false_positive_worktrees_in_name)
run_test("Performance — under 100ms", test_performance)

print(f"\n{'=' * 50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
