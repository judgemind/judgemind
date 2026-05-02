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
    status_dir = os.path.join(worktree, "tmp")
    os.makedirs(worktree, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)
    return worktree, os.path.join(status_dir, "agent-status.txt")


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
    """Exit 1 (RESUME) pointing to A.2b when phase is `ralph-worker (1)` and ralph-done.txt has SHIP."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-ralph")
        with open(status_path, "w") as f:
            f.write("issue: #2500\nphase: ralph-worker (1)\nupdated: x\nsummary: y\n")
        # Write ralph-done.txt with SHIP so the script routes to A.2b
        ralph_dir = os.path.join(worktree, "tmp", "ralph")
        os.makedirs(ralph_dir, exist_ok=True)
        with open(os.path.join(ralph_dir, "ralph-done.txt"), "w") as f:
            f.write("SHIP\nAll acceptance criteria met.\n")
        result = run_script(worktree)
        assert result.returncode == 1, (
            f"expected exit 1 (RESUME), got {result.returncode}: "
            f"stdout={result.stdout!r}"
        )
        assert "RESUME" in result.stdout
        assert "A.2b" in result.stdout  # Next step advice


def test_ralph_worker_no_done_file_returns_ralph_loop() -> None:
    """Exit 1 (RESUME) pointing to ralph Step 2 when ralph-done.txt is absent."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-ralph-mid")
        with open(status_path, "w") as f:
            f.write("issue: #2500\nphase: ralph-worker (3)\nupdated: x\nsummary: y\n")
        # Do NOT create ralph-done.txt — ralph is mid-loop
        result = run_script(worktree)
        assert result.returncode == 1, (
            f"expected exit 1 (RESUME), got {result.returncode}: "
            f"stdout={result.stdout!r}"
        )
        assert "RESUME" in result.stdout
        # Should point to ralph Step 2, not A.2b
        assert "ralph" in result.stdout.lower()
        assert "A.2b" not in result.stdout


def test_ralph_worker_done_file_returns_a2b() -> None:
    """Exit 1 (RESUME) pointing to A.2b explicitly when ralph-done.txt first line is SHIP."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-ralph-ship")
        with open(status_path, "w") as f:
            f.write("issue: #2501\nphase: ralph-reviewer (2)\nupdated: x\nsummary: y\n")
        ralph_dir = os.path.join(worktree, "tmp", "ralph")
        os.makedirs(ralph_dir, exist_ok=True)
        with open(os.path.join(ralph_dir, "ralph-done.txt"), "w") as f:
            f.write("SHIP\nAll reviewers approved.\n")
        result = run_script(worktree)
        assert result.returncode == 1
        assert "RESUME" in result.stdout
        assert "A.2b" in result.stdout


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


def test_audit_category_phase_returns_resume_with_next_category() -> None:
    """Exit 1 (RESUME) with a meaningful next-step for audit-category-1.5."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-audit")
        with open(status_path, "w") as f:
            f.write("issue: audit\nphase: audit-category-1.5\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1, (
            f"expected exit 1 (RESUME), got {result.returncode}: "
            f"stdout={result.stdout!r}"
        )
        assert "RESUME" in result.stdout
        # Should mention the next category (1.6 Security) or audit SKILL.md
        assert "1.6" in result.stdout or "Security" in result.stdout or "audit" in result.stdout.lower()


def test_spotcheck_step_phase_returns_resume_with_next_step() -> None:
    """Exit 1 (RESUME) with a meaningful next-step for spotcheck-step-2."""
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-spotcheck")
        with open(status_path, "w") as f:
            f.write("issue: spotcheck\nphase: spotcheck-step-2\nupdated: x\nsummary: y\n")
        result = run_script(worktree)
        assert result.returncode == 1, (
            f"expected exit 1 (RESUME), got {result.returncode}: "
            f"stdout={result.stdout!r}"
        )
        assert "RESUME" in result.stdout
        # Should mention the S3 orphan check (2.5) or screenshots/cross-reference
        assert "2.5" in result.stdout or "orphan" in result.stdout.lower() or "spotcheck" in result.stdout.lower()


def test_status_file_inside_worktree_sandbox_compatible() -> None:
    """Status file path returned by the script is fully inside the worktree.

    This verifies the sandbox-compatibility guarantee: the path must begin with
    the worktree root so that worktree-sandboxed agents can write to it without
    triggering a cross-worktree BLOCKED error.
    """
    with tempfile.TemporaryDirectory() as tmp:
        worktree, status_path = _build_fake_worktree(tmp, "agent-sandbox")
        with open(status_path, "w") as f:
            f.write("issue: #9999\nphase: claiming\nupdated: x\nsummary: y\n")
        # The status file itself must be under the worktree
        assert status_path.startswith(worktree), (
            f"STATUS_FILE {status_path!r} is not inside the worktree {worktree!r}"
        )
        # Also confirm the script reads the file successfully (returns RESUME, not UNKNOWN)
        result = run_script(worktree)
        assert result.returncode == 1, (
            f"expected exit 1 (RESUME), got {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "RESUME" in result.stdout


def main() -> int:
    tests = [
        ("missing status file -> UNKNOWN", test_missing_status_file_returns_unknown),
        ("phase=done -> DONE", test_done_phase_returns_done),
        ("phase=verified -> DONE", test_verified_phase_returns_done),
        ("phase=blocked -> DONE", test_blocked_phase_returns_done),
        ("phase=ralph-worker + SHIP done file -> A.2b", test_ralph_worker_phase_returns_resume),
        ("phase=ralph-worker + no done file -> ralph loop", test_ralph_worker_no_done_file_returns_ralph_loop),
        ("phase=ralph-reviewer + SHIP done file -> A.2b", test_ralph_worker_done_file_returns_a2b),
        ("phase=pushing -> RESUME", test_pushing_phase_returns_resume),
        ("phase=verifying -> RESUME", test_verifying_phase_returns_resume),
        ("phase=ci-watch (N) -> RESUME", test_ci_watch_phase_returns_resume),
        ("unknown phase -> RESUME with hint", test_unknown_phase_returns_resume_with_hint),
        ("bad worktree path -> UNKNOWN", test_bad_worktree_path_returns_unknown),
        ("empty phase field -> UNKNOWN", test_empty_phase_field_returns_unknown),
        ("phase=audit-category-1.5 -> RESUME with next category", test_audit_category_phase_returns_resume_with_next_category),
        ("phase=spotcheck-step-2 -> RESUME with next step", test_spotcheck_step_phase_returns_resume_with_next_step),
        ("status file is inside worktree (sandbox compatible)", test_status_file_inside_worktree_sandbox_compatible),
    ]
    for desc, fn in tests:
        run_test(desc, fn)

    print()
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
