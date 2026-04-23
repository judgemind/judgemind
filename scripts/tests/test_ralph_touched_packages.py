"""Tests for ralph_touched_packages module."""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralph_touched_packages import (
    derive_touched_packages,
    derive_touched_ts_packages,
    git_diff_names,
)


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
        """Files not under packages/<name>/src/ or tests/ are excluded."""
        files = [
            "scripts/some_script.py",
            "infra/lambda-handler/handler.py",
            "packages/scraper-framework/pyproject.toml",
        ]
        assert derive_touched_packages(files) == []

    def test_package_config_file_excluded(self) -> None:
        """Config files at the package root level are excluded."""
        files = [
            "packages/scraper-framework/pyproject.toml",
            "packages/scraper-framework/setup.cfg",
        ]
        assert derive_touched_packages(files) == []

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

    def test_ts_in_tests_subdir_not_detected(self) -> None:
        """TypeScript files under packages/<pkg>/tests/ are not detected (only src/ and app/)."""
        files = ["packages/web/tests/unit.ts"]
        assert derive_touched_ts_packages(files) == []

    def test_mixed_py_and_ts_returns_ts_only(self) -> None:
        """With mixed files, only TypeScript packages are returned."""
        files = [
            "packages/scraper-framework/src/framework/base.py",
            "packages/web/src/app/page.tsx",
        ]
        result = derive_touched_ts_packages(files)
        assert result == ["web"]


class TestGitDiffNames:
    """Tests for git_diff_names function."""

    def test_returns_list_for_nonexistent_path(self, tmp_path: Path) -> None:
        """Returns empty list for a non-git directory."""
        result = git_diff_names(tmp_path / "nonexistent")
        assert result == []

    def test_returns_list_type(self, tmp_path: Path) -> None:
        """Always returns a list (not None or other types)."""
        result = git_diff_names(tmp_path)
        assert isinstance(result, list)

    def test_filters_empty_lines(self, tmp_path: Path) -> None:
        """Result never contains empty strings."""
        result = git_diff_names(tmp_path)
        assert all(line.strip() for line in result)
