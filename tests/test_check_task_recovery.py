#!/usr/bin/env python3
"""Tests for scripts/check-task-recovery.sh

Run from the repo root:
    python3 tests/test_check_task_recovery.py

Each test builds a fake worktree + status file and checks the exit code and
output of the recovery script. See #2545 for motivation.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SCRIPT = os.path.join(REPO_ROOT, "scripts", "check-task-recovery.sh")

passed = 0
failed = 0


def _build_fake_worktree(base: str, agent_id: str) -> tuple[str, str]:
    """Create a fake repo_root + worktree under `base` and return their paths."""
    repo_root = os.path.join(base, "repo")
    worktree = os.path.join(repo_root, ".claude", "worktrees", agent_id)
    status_dir = os.path.join(repo_root, "tmp", "agent-status")
    os.makedirs(worktree, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)
    return worktree, os.path.join(status_dir, f"{agent_id}.txt")


def run_script(worktree: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", SCRIPT, worktree],
        capture_output=True,
        text=True,
        timeout=5,
    )


def run_test(description: str, test_fn: object) -> None:
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


def test_missing_status_file_returns_unknown() -> None:
    """Exit 2 when the status file is absent."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, _ = _build_fake_worktree(tmp, "agent-xxx")
        # Don't create the status file.
        result = run_script(worktree)
        assert result.returncode == 2, (
            f"expected exit 2 (UNKNOWN), got {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "UNKNOWN" in result.stderr


def test_done_phase_returns_done() -> None:
    """Exit 0 when phase is `done`."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-done")
        with open(status_path, "w") as f:
            f.write("issue: #123\nphase: done\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 0, (
            f"expected exit 0 (DONE), got {result.returncode}: "
            f"stdout={result.stdout!r}"
        )
        assert "DONE" in result.stdout


def test_verified_phase_returns_done() -> None:
    """Exit 0 when phase is `verified` (alias for done)."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-verified")
        with open(status_path, "w") as f:
            f.write("issue: #456\nphase: verified\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 0, f"expected exit 0, got {result.returncode}"
        assert "DONE" in result.stdout


def test_blocked_phase_returns_done() -> None:
    """Exit 0 when phase is `blocked` — no further work expected."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-blocked")
        with open(status_path, "w") as f:
            f.write("issue: #789\nphase: blocked\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 0


def test_ralph_worker_phase_returns_resume() -> None:
    """Exit 1 (RESUME) when phase is `ralph-worker (1)` — mid-implementation."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-ralph")
        with open(status_path, "w") as f:
            f.write("issue: #2500\nphase: ralph-worker (1)\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1, (
            f"expected exit 1 (RESUME), got {result.returncode}: "
            f"stdout={result.stdout!r}"
        )
        assert "RESUME" in result.stdout
        assert "A.2b" in result.stdout  # Next step advice


def test_pushing_phase_returns_resume() -> None:
    """Exit 1 when phase is `pushing` — mid-PR creation."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-push")
        with open(status_path, "w") as f:
            f.write("issue: #2502\nphase: pushing\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1
        assert "RESUME" in result.stdout
        assert "A.4" in result.stdout or "A.5" in result.stdout


def test_verifying_phase_returns_resume() -> None:
    """Exit 1 when phase is `verifying` — evidence comment not yet posted."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-verify")
        with open(status_path, "w") as f:
            f.write("issue: #2500\nphase: verifying\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1
        assert "RESUME" in result.stdout
        assert "A.8" in result.stdout


def test_ci_watch_phase_returns_resume() -> None:
    """Exit 1 when phase is `ci-watch (2)` with attempt suffix."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-ci")
        with open(status_path, "w") as f:
            f.write("issue: #111\nphase: ci-watch (2)\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1
        assert "RESUME" in result.stdout


def test_unknown_phase_returns_resume_with_hint() -> None:
    """Exit 1 with a re-read hint when the phase is unrecognized."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-unknown")
        with open(status_path, "w") as f:
            f.write("issue: #999\nphase: bogus-phase\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1
        assert "RESUME" in result.stdout
        assert "SKILL.md" in result.stdout


def test_bad_worktree_path_returns_unknown() -> None:
    """Exit 2 when worktree path doesn't contain .claude/worktrees/."""
    with tempfile.TemporaryDirectory() as tmp:
        result = run_script(tmp)
        assert result.returncode == 2
        assert "UNKNOWN" in result.stderr


def test_empty_phase_field_returns_unknown() -> None:
    """Exit 2 when the phase field is blank."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-empty")
        with open(status_path, "w") as f:
            f.write("issue: #777\nphase:\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 2
        assert "UNKNOWN" in result.stderr


def main() -> int:
    tests = [
        ("missing status file -> UNKNOWN", test_missing_status_file_returns_unknown),
        ("phase=done -> DONE", test_done_phase_returns_done),
        ("phase=verified -> DONE", test_verified_phase_returns_done),
        ("phase=blocked -> DONE", test_blocked_phase_returns_done),
        ("phase=ralph-worker -> RESUME", test_ralph_worker_phase_returns_resume),
        ("phase=pushing -> RESUME", test_pushing_phase_returns_resume),
        ("phase=verifying -> RESUME", test_verifying_phase_returns_resume),
        ("phase=ci-watch (N) -> RESUME", test_ci_watch_phase_returns_resume),
        ("unknown phase -> RESUME with hint", test_unknown_phase_returns_resume_with_hint),
        ("bad worktree path -> UNKNOWN", test_bad_worktree_path_returns_unknown),
        ("empty phase field -> UNKNOWN", test_empty_phase_field_returns_unknown),
    ]
    for desc, fn in tests:
        run_test(desc, fn)

    print()
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
