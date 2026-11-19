"""Tests for ralph_touched_packages module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralph_touched_packages import (
    EXIT_GIT_ERROR,
    EXIT_NO_PACKAGES,
    EXIT_UNMAPPED,
    GitError,
    derive_touched_packages,
    derive_touched_ts_packages,
    git_diff_names,
    main,
    run,
    unmapped_code_files,
)

SCRIPT = Path(__file__).resolve().parent.parent / "ralph_touched_packages.py"


class TestDeriveTouchedPackages:
    """Tests for derive_touched_packages (Python packages)."""

    def test_empty_diff_returns_empty(self) -> None:
        """Empty changed-files list returns no packages."""
        assert derive_touched_packages([]) == []

    def test_single_package_src_change(self) -> None:
        """A .py file under packages/<pkg>/src/ is detected."""
        files = ["packages/scraper-framework/src/framework/base.py"]
        assert derive_touched_packages(files) == ["scraper-framework"]

    def test_single_package_tests_change(self) -> None:
        """A .py file under packages/<pkg>/tests/ is detected."""
        files = ["packages/nlp-pipeline/tests/test_enrichment.py"]
        assert derive_touched_packages(files) == ["nlp-pipeline"]

    def test_two_packages_returns_sorted(self) -> None:
        """Changes to two packages return sorted list of both."""
        files = [
            "packages/scraper-framework/src/framework/scraper.py",
            "packages/nlp-pipeline/tests/test_enrichment.py",
        ]
        result = derive_touched_packages(files)
        assert result == ["nlp-pipeline", "scraper-framework"]

    def test_docs_only_change_returns_empty(self) -> None:
        """Markdown files are not Python code changes."""
        files = ["docs/specs/architecture-spec-v1.md", "README.md"]
        assert derive_touched_packages(files) == []

    def test_non_package_paths_excluded(self) -> None:
        """Files outside a package's test paths are excluded.

        infra/<dir> packages are the hook's concern; ralph runs only
        packages/<name>/.venv suites.
        """
        files = [
            "scripts/some_script.py",
            "infra/lambda-handler/handler.py",
            "packages/scraper-framework/README.md",
            "packages/scraper-framework/tests/fixtures/page.html",
        ]
        assert derive_touched_packages(files) == []

    @pytest.mark.parametrize(
        "name",
        [
            "pyproject.toml",
            "setup.cfg",
            "requirements.txt",
            "requirements-dev.txt",
            "pytest.ini",
        ],
    )
    def test_package_test_config_selects_package(self, name: str) -> None:
        """A dependency or test-config change selects the package (#5151)."""
        files = [f"packages/nlp-pipeline/{name}"]
        assert derive_touched_packages(files) == ["nlp-pipeline"]

    def test_duplicate_files_in_same_package_deduplicated(self) -> None:
        """Multiple changed files in the same package count as one entry."""
        files = [
            "packages/scraper-framework/src/framework/base.py",
            "packages/scraper-framework/src/framework/scraper.py",
            "packages/scraper-framework/tests/test_base.py",
        ]
        result = derive_touched_packages(files)
        assert result == ["scraper-framework"]

    def test_ts_files_not_in_python_list(self) -> None:
        """TypeScript files are not returned by the Python helper."""
        files = [
            "packages/web/src/app/page.tsx",
            "packages/web/src/components/Header.ts",
        ]
        assert derive_touched_packages(files) == []

    def test_mixed_python_and_non_python(self) -> None:
        """Only Python packages are returned even when other file types are present."""
        files = [
            "packages/scraper-framework/src/framework/base.py",
            "docs/specs/architecture-spec-v1.md",
            "scripts/some_script.py",
            "packages/web/src/app/page.tsx",
        ]
        result = derive_touched_packages(files)
        assert result == ["scraper-framework"]


class TestDeriveTouchedTsPackages:
    """Tests for derive_touched_ts_packages (TypeScript packages)."""

    def test_empty_diff_returns_empty(self) -> None:
        """Empty changed-files list returns no packages."""
        assert derive_touched_ts_packages([]) == []

    def test_ts_file_under_src_detected(self) -> None:
        """A .ts file under packages/<pkg>/src/ is detected."""
        files = ["packages/web/src/components/Header.ts"]
        assert derive_touched_ts_packages(files) == ["web"]

    def test_tsx_file_under_src_detected(self) -> None:
        """A .tsx file under packages/<pkg>/src/ is detected."""
        files = ["packages/web/src/app/page.tsx"]
        assert derive_touched_ts_packages(files) == ["web"]

    def test_ts_file_under_app_detected(self) -> None:
        """A .ts file under packages/<pkg>/app/ is detected."""
        files = ["packages/web/app/layout.tsx"]
        assert derive_touched_ts_packages(files) == ["web"]

    def test_py_files_not_in_ts_list(self) -> None:
        """Python files are not returned by the TypeScript helper."""
        files = [
            "packages/scraper-framework/src/framework/base.py",
            "packages/nlp-pipeline/tests/test_enrichment.py",
        ]
        assert derive_touched_ts_packages(files) == []

    def test_two_ts_packages_sorted(self) -> None:
        """Changes to two TypeScript packages return sorted list."""
        files = [
            "packages/web/src/app/page.tsx",
            "packages/api-client/src/index.ts",
        ]
        result = derive_touched_ts_packages(files)
        assert result == ["api-client", "web"]

    def test_docs_only_excluded(self) -> None:
        """Markdown docs are excluded from TypeScript packages."""
        files = ["docs/web-patterns.md", "packages/web/README.md"]
        assert derive_touched_ts_packages(files) == []

    def test_ts_in_tests_subdir_detected(self) -> None:
        """TypeScript files under packages/<pkg>/tests/ count (#5112).

        packages/api and packages/web keep their vitest suites under tests/,
        so a test-only change there must still select the package.
        """
        files = ["packages/web/tests/unit.ts"]
        assert derive_touched_ts_packages(files) == ["web"]

    def test_ts_package_root_config_detected(self) -> None:
        """A package-root config such as vitest.config.ts selects the package."""
        assert derive_touched_ts_packages(["packages/api/vitest.config.ts"]) == ["api"]

    def test_mixed_py_and_ts_returns_ts_only(self) -> None:
        """With mixed files, only TypeScript packages are returned."""
        files = [
            "packages/scraper-framework/src/framework/base.py",
            "packages/web/src/app/page.tsx",
        ]
        result = derive_touched_ts_packages(files)
        assert result == ["web"]


class TestPythonConftest:
    """A package-root conftest.py shapes the package's test run."""

    def test_root_conftest_detected(self) -> None:
        files = ["packages/scraper-framework/conftest.py"]
        assert derive_touched_packages(files) == ["scraper-framework"]


class TestUnmappedCodeFiles:
    """Code under packages/ that no mapper covers is reported, not dropped."""

    def test_unmapped_python_path_reported(self) -> None:
        files = ["packages/scraper-framework/tools/gen.py", "docs/x.md"]
        assert unmapped_code_files(files) == ["packages/scraper-framework/tools/gen.py"]

    def test_mapped_and_non_code_paths_not_reported(self) -> None:
        files = [
            "packages/web/src/app/page.tsx",
            "packages/nlp-pipeline/src/x.py",
            "packages/nlp-pipeline/pyproject.toml",
            "scripts/foo.py",
        ]
        assert unmapped_code_files(files) == []


# --- temp-repo integration tests --------------------------------------------
# Every git command runs inside tmp_path. The autouse _git_env_scrub fixture in
# scripts/tests/conftest.py strips GIT_DIR & co, so nothing here can reach the
# shared repo (#5058).


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def _write(repo: Path, rel: str, text: str = "x = 1\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A temp repo whose origin/main is its first commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "README.md", "hello\n")
    _write(root, "packages/scraper-framework/src/framework/base.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "checkout", "-q", "-b", "work")
    return root


def _cli(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra, str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )


class TestChangeSet:
    """git_diff_names covers committed and uncommitted work (#5112)."""

    def test_unstaged_change_returns_package(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/src/framework/base.py", "x = 2\n")
        result = _cli(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["scraper-framework"]

    def test_staged_change_returns_package(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/src/framework/base.py", "x = 2\n")
        _git(repo, "add", "-A")
        result = _cli(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["scraper-framework"]

    def test_committed_only_change_returns_package(self, repo: Path) -> None:
        _write(repo, "packages/nlp-pipeline/tests/test_x.py")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "work")
        assert _git(repo, "status", "--porcelain") == ""
        result = _cli(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["nlp-pipeline"]

    def test_untracked_new_file_returns_package(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/tests/test_new.py")
        result = _cli(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["scraper-framework"]

    def test_untracked_path_is_repo_relative_from_subdir(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/tests/test_new.py")
        names = git_diff_names(repo / "packages")
        assert "packages/scraper-framework/tests/test_new.py" in names

    def test_union_is_deduplicated_and_sorted(self, repo: Path) -> None:
        _write(repo, "packages/nlp-pipeline/src/a.py")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "c1")
        _write(repo, "packages/nlp-pipeline/src/a.py", "x = 3\n")
        _git(repo, "add", "-A")
        _write(repo, "packages/scraper-framework/src/framework/base.py", "x = 2\n")
        _write(repo, "packages/web/src/page.tsx", "export {}\n")
        names = git_diff_names(repo)
        assert names == sorted(set(names))
        assert names.count("packages/nlp-pipeline/src/a.py") == 1
        result = _cli(repo)
        assert result.stdout.splitlines() == ["nlp-pipeline", "scraper-framework"]
        ts = _cli(repo, "--ts")
        assert ts.returncode == 0, ts.stderr
        assert ts.stdout.splitlines() == ["web"]

    def test_gitignored_untracked_file_excluded(self, repo: Path) -> None:
        _write(repo, ".gitignore", "*.pyc\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "ignore")
        _write(repo, "packages/scraper-framework/src/framework/base.cpython.pyc")
        assert all(not n.endswith(".pyc") for n in git_diff_names(repo))


class TestNoPackagesOutcomes:
    """An empty package list is always explained, never silent (#5112)."""

    def test_genuinely_empty_change_set(self, repo: Path) -> None:
        result = _cli(repo)
        assert result.returncode == EXIT_NO_PACKAGES
        assert result.stdout == ""
        assert "no-packages: change set is empty" in result.stderr

    def test_changes_outside_any_package(self, repo: Path) -> None:
        _write(repo, "docs/guide.md", "doc\n")
        _write(repo, "scripts/tool.py")
        _write(repo, "packages/scraper-framework/README.md", "doc\n")
        result = _cli(repo)
        assert result.returncode == EXIT_NO_PACKAGES
        assert result.stdout == ""
        assert "no-packages: 3 changed file(s), none under a package" in result.stderr
        assert "docs/guide.md" in result.stderr

    def test_pyproject_only_change_selects_package(self, repo: Path) -> None:
        """A pyproject-only change is not a no-packages result (#5151)."""
        _write(repo, "packages/scraper-framework/pyproject.toml", "[x]\n")
        result = _cli(repo)
        assert result.returncode == 0
        assert result.stdout == "scraper-framework\n"

    def test_python_mode_with_ts_only_change_points_at_ts(self, repo: Path) -> None:
        _write(repo, "packages/web/src/page.tsx", "export {}\n")
        result = _cli(repo)
        assert result.returncode == EXIT_NO_PACKAGES
        assert "TypeScript package(s) web (run --ts)" in result.stderr

    def test_ts_mode_with_python_only_change_points_at_python(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/src/framework/base.py", "x = 2\n")
        result = _cli(repo, "--ts")
        assert result.returncode == EXIT_NO_PACKAGES
        assert "Python package(s) scraper-framework (run without --ts)" in result.stderr

    def test_long_file_list_is_truncated_in_reason(self, repo: Path) -> None:
        for i in range(7):
            _write(repo, f"docs/d{i}.md", "doc\n")
        result = _cli(repo)
        assert result.returncode == EXIT_NO_PACKAGES
        assert "7 changed file(s)" in result.stderr
        assert " ..." in result.stderr


class TestLoudFailures:
    """Dirty trees that can't be mapped, and git errors, exit non-zero."""

    def test_dirty_tree_with_unmapped_path_fails_loudly(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/tools/gen.py")
        result = _cli(repo)
        assert result.returncode == EXIT_UNMAPPED
        assert result.stdout == ""
        assert "packages/scraper-framework/tools/gen.py" in result.stderr
        assert "Fix:" in result.stderr

    def test_unmapped_path_fails_even_alongside_mapped_ones(self, repo: Path) -> None:
        _write(repo, "packages/scraper-framework/src/framework/base.py", "x = 2\n")
        _write(repo, "packages/scraper-framework/tools/gen.py")
        assert _cli(repo).returncode == EXIT_UNMAPPED

    def test_not_a_git_repo_fails_with_fix_block(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        result = _cli(plain)
        assert result.returncode == EXIT_GIT_ERROR
        assert "Fix:" in result.stderr

    def test_missing_origin_main_fails(self, repo: Path) -> None:
        _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
        result = _cli(repo)
        assert result.returncode == EXIT_GIT_ERROR
        assert "origin/main" in result.stderr

    def test_git_diff_names_raises_instead_of_returning_empty(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(GitError):
            git_diff_names(tmp_path / "nonexistent")

    def test_git_binary_missing_raises(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(repo / "no-bin-here"))
        with pytest.raises(GitError):
            git_diff_names(repo)


class TestRunInProcess:
    """run() / main() in-process, so coverage sees the CLI paths."""

    def test_run_prints_packages(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(repo, "packages/scraper-framework/src/framework/base.py", "x = 2\n")
        assert run(repo) == 0
        assert capsys.readouterr().out.splitlines() == ["scraper-framework"]

    def test_main_exits_with_run_code(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            sys, "argv", ["ralph_touched_packages.py", "--ts", str(repo)]
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == EXIT_NO_PACKAGES

    def test_main_defaults_to_cwd(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(repo, "packages/nlp-pipeline/src/a.py")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(sys, "argv", ["ralph_touched_packages.py"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
