"""Regression tests for the git-env-scrub conftest block (#5058).

In #5045 a ``git push`` exported GIT_DIR to the pre-push hook, and tests that
ran ``git init`` / ``git config`` / ``git commit`` in scratch directories
wrote to the shared repository instead (core.bare=true, main moved). The
conftest block in every Python test root unsets git's repository env vars
for the whole session. These tests export GIT_DIR at a sacrificial bare
repository, run a scratch-repo writer under pytest, and check the
sacrificial repository is byte-for-byte unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
WRITER = TESTS_DIR / "_git_env_scrub_scratch_writer.py"
BEGIN = "# --- BEGIN git-env-scrub (#5058) ---"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def _snapshot(git_dir: Path) -> dict[str, str]:
    """Every file under a git dir except objects, keyed by relative path."""
    snap: dict[str, str] = {}
    for path in sorted(git_dir.rglob("*")):
        rel = path.relative_to(git_dir).as_posix()
        if path.is_file() and not rel.startswith(("objects/", "hooks/")):
            snap[rel] = path.read_text(errors="replace")
    snap["<objects>"] = " ".join(
        sorted(p.name for p in (git_dir / "objects").rglob("*") if p.is_file())
    )
    return snap


@pytest.fixture
def canary(tmp_path: Path) -> Path:
    """A sacrificial bare repo with one commit on main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README").write_text("canary\n")
    _git("add", "README", cwd=seed)
    _git("commit", "-q", "-m", "canary", cwd=seed)
    bare = tmp_path / "canary.git"
    _git("clone", "-q", "--bare", str(seed), str(bare))
    return bare


def _run_writer(
    canary: Path, tmp_path: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    """Run the writer in a pytest subprocess with GIT_DIR at the canary."""
    env = dict(os.environ)
    env["GIT_DIR"] = str(canary)
    # The negative control runs without the conftest; keep its global config
    # writes away from the user's ~/.gitconfig too.
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "outer-gitconfig")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *extra,
            str(WRITER),
        ],
        check=False,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_scratch_repo_writes_leave_inherited_git_dir_untouched(
    canary: Path, tmp_path: Path
) -> None:
    before = _snapshot(canary)
    result = _run_writer(canary, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(canary) == before
    assert _git("--git-dir", str(canary), "config", "core.bare") == "true"


def test_without_the_conftest_the_same_run_damages_the_repo(
    canary: Path, tmp_path: Path
) -> None:
    """Negative control: --noconftest drops the scrub, and GIT_DIR wins.

    This is what makes the test above meaningful: the writer really does act
    on an inherited GIT_DIR when nothing scrubs it.
    """
    before = _snapshot(canary)
    _run_writer(canary, tmp_path, "--noconftest")
    after = _snapshot(canary)
    assert after != before
    assert "Scratch Writer" in after["config"]


def test_session_env_is_scrubbed_and_global_config_pinned() -> None:
    names = _git("rev-parse", "--local-env-vars").split()
    assert "GIT_DIR" in names
    leaked = [n for n in names if n in os.environ]
    assert leaked == []
    global_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    assert global_config.is_file()
    assert _git("config", "--global", "user.name") == "Judgemind Test"
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"


def test_every_python_test_root_carries_the_block() -> None:
    roots = [
        "scripts/tests",
        "scripts/dispatcher/tests",
        "scripts/dispatcher_v3/tests",
        "packages/scraper-framework/tests",
        "packages/nlp-pipeline/tests",
        "packages/judgemind-config/tests",
    ]
    missing = [
        r for r in roots if BEGIN not in (REPO_ROOT / r / "conftest.py").read_text()
    ]
    assert missing == []


def _load_conftest() -> ModuleType:
    """A private copy of scripts/tests/conftest.py, to poke its helpers."""
    spec = importlib.util.spec_from_file_location(
        "_git_env_scrub_conftest_copy", TESTS_DIR / "conftest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_var_names_empty_when_git_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest = _load_conftest()

    def _no_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(conftest.subprocess, "run", _no_git)
    assert conftest._git_env_scrub_var_names() == []


def test_var_names_fail_loudly_when_git_does_not_list_git_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest = _load_conftest()
    broken = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(conftest.subprocess, "run", lambda *a, **k: broken)
    with pytest.raises(RuntimeError, match="Fix:"):
        conftest._git_env_scrub_var_names()
