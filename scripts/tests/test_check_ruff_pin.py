"""Tests for scripts/check-ruff-pin.py (#5153).

Each test builds a scratch tree (not a git checkout, so the guard walks
it) with a requirements-ruff.txt pin and the files under test, then runs
the guard on it with --root.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check-ruff-pin.py"
REPO_ROOT = SCRIPT.parent.parent

_spec = importlib.util.spec_from_file_location("check_ruff_pin", SCRIPT)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PIN = "1.2.3"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _write(tmp_path, "requirements-ruff.txt", f"# the pin\nruff=={PIN}\n")
    return tmp_path


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_clean_tree_passes(tree: Path) -> None:
    _write(
        tree,
        "packages/a/pyproject.toml",
        f'[project]\nname = "a"\n[project.optional-dependencies]\ndev = ["pytest", "ruff=={PIN}"]\n',
    )
    _write(
        tree,
        ".github/workflows/ci.yml",
        "      - run: pip install -r requirements-ruff.txt\n",
    )
    _write(tree, "scripts/install.sh", f'"$PIP" install --quiet pytest ruff=={PIN}\n')
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert f"ruff=={PIN}" in result.stdout


def test_real_repo_passes() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_unpinned_pip_install_in_workflow_fails(tree: Path) -> None:
    _write(
        tree,
        ".github/workflows/ci.yml",
        "      - name: x\n        run: pip install ruff\n",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert ".github/workflows/ci.yml:2: run: pip install ruff" in result.stderr
    assert "Fix:" in result.stderr
    assert "pip install -r requirements-ruff.txt" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        'echo "  Install with: pip install ruff" >&2',
        '"$PIP" install --quiet pytest ruff boto3',
        "python -m pip install 'ruff>=0.3'",
        "uv pip install ruff==9.9.9",
        "RUN pip3 install --no-cache-dir ruff",
    ],
)
def test_command_lines_naming_ruff_fail(tree: Path, line: str) -> None:
    rel = "Dockerfile" if line.startswith("RUN") else "scripts/x.sh"
    _write(tree, rel, line + "\n")
    result = _run(tree)
    assert result.returncode == 1, result.stdout
    assert f"{rel}:1:" in result.stderr


def test_pip_install_of_other_packages_passes(tree: Path) -> None:
    _write(
        tree, ".githooks/pre-push", "pip install ruffle pytest\nruff format --check .\n"
    )
    assert _run(tree).returncode == 0


def test_loose_pyproject_spec_fails(tree: Path) -> None:
    _write(
        tree,
        "packages/a/pyproject.toml",
        '[project]\nname = "a"\n[project.optional-dependencies]\ndev = [\n    "ruff>=0.3",\n]\n',
    )
    result = _run(tree)
    assert result.returncode == 1
    assert 'packages/a/pyproject.toml:5: "ruff>=0.3",' in result.stderr
    assert f'"ruff=={PIN}"' in result.stderr


def test_pyproject_dependency_groups_and_runtime_deps_checked(tree: Path) -> None:
    _write(
        tree,
        "packages/a/pyproject.toml",
        '[project]\nname = "a"\ndependencies = ["Ruff"]\n[dependency-groups]\nlint = ["ruff==0.0.1"]\n',
    )
    result = _run(tree)
    assert result.returncode == 1
    assert result.stderr.count("packages/a/pyproject.toml") == 2


def test_requirements_file_checked_even_under_tests(tree: Path) -> None:
    _write(tree, "scripts/tests/requirements-ci.txt", "pytest\nruff  # linter\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "scripts/tests/requirements-ci.txt:2:" in result.stderr


def test_requirements_include_of_pin_file_passes(tree: Path) -> None:
    _write(
        tree,
        "scripts/tests/requirements-ci.txt",
        f"pytest\n-r ../../requirements-ruff.txt\nruff=={PIN}\n",
    )
    assert _run(tree).returncode == 0


def test_test_dirs_and_archive_are_not_scanned(tree: Path) -> None:
    _write(tree, "scripts/tests/test_x.sh", "pip install ruff\n")
    _write(tree, "scripts/archive/old.sh", "pip install ruff\n")
    _write(
        tree,
        "packages/a/tests/fixture/pyproject.toml",
        '[project]\ndependencies = ["ruff"]\n',
    )
    assert _run(tree).returncode == 0


def test_missing_pin_file_exits_2(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "requirements-ruff.txt not found" in result.stderr
    assert "Fix:" in result.stderr


@pytest.mark.parametrize(
    "content", ["ruff>=1.0\n", "ruff==1.2.3\nruff==1.2.4\n", "# only a comment\n"]
)
def test_malformed_pin_file_exits_2(tmp_path: Path, content: str) -> None:
    _write(tmp_path, "requirements-ruff.txt", content)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "exactly one" in result.stderr


def test_in_process_main_and_helpers(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert guard.read_pin(tree) == PIN
    assert guard.classify("scripts/tests/requirements-ci.txt") == "requirements"
    assert guard.classify("scripts/tests/test_x.sh") is None
    assert guard.classify(".githooks/pre-push") == "commands"
    assert guard.classify(".claude/hooks/preflight-bash.sh") == "commands"
    assert guard.classify("docs/x.md") is None
    assert guard.classify("requirements-ruff.txt") is None
    assert guard.scan_pyproject("p", "not = [valid", PIN) == []
    _write(tree, "scripts/x.sh", "pip install ruff\n")
    assert guard.main(["--root", str(tree)]) == 1
    assert "scripts/x.sh:1:" in capsys.readouterr().err
