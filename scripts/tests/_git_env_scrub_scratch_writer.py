"""Scratch-repo git writer driven by test_git_env_scrub.py (#5058).

The leading underscore keeps pytest from collecting this file in a normal
``pytest scripts/tests/`` run. test_git_env_scrub.py runs it in a pytest
subprocess with GIT_DIR exported at a sacrificial repository, the way a git
hook would, and checks that the sacrificial repository is untouched.

It does what the real scratch-repo tests do: init, config, commit and
update-ref, all inside ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def test_scratch_repo_git_writes(tmp_path: Path) -> None:
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Scratch Writer")
    _git(repo, "config", "user.email", "scratch@example.com")
    (repo / "file.txt").write_text("hello\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "scratch commit")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/heads/scratch-ref", head)
    _git(repo, "config", "core.bare", "true")

    # Every write landed in the scratch repo itself.
    assert (
        repo / ".git" / "refs" / "heads" / "scratch-ref"
    ).read_text().strip() == head
    assert "Scratch Writer" in (repo / ".git" / "config").read_text()
