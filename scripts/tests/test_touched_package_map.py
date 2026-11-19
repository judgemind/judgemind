"""Tests for scripts/touched_package_map.py, the mapping shared by pre-push and ralph (#5151)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from touched_package_map import (  # noqa: E402
    is_python_test_trigger,
    is_ts_test_trigger,
    select_test_packages,
    split_package_path,
)

SCRIPT = Path(__file__).resolve().parent.parent / "touched_package_map.py"


class TestSplitPackagePath:
    def test_packages_path(self) -> None:
        assert split_package_path("packages/api/src/x.ts") == ("api", "src/x.ts")

    def test_infra_path(self) -> None:
        assert split_package_path("infra/lambda/handler.py") == (
            "infra/lambda",
            "handler.py",
        )

    @pytest.mark.parametrize(
        "path", ["README.md", "packages/README.md", "scripts/foo.py", "docs/a/b.md"]
    )
    def test_outside_a_package(self, path: str) -> None:
        assert split_package_path(path) is None


class TestPythonTriggers:
    @pytest.mark.parametrize(
        "rel",
        [
            "src/pkg/mod.py",
            "tests/test_mod.py",
            "tests/unit/conftest.py",
            "conftest.py",
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "pytest.ini",
            "tox.ini",
            ".coveragerc",
            "requirements.txt",
            "requirements-dev.txt",
        ],
    )
    def test_selects_tests(self, rel: str) -> None:
        assert is_python_test_trigger("nlp-pipeline", rel)

    @pytest.mark.parametrize(
        "rel",
        [
            "README.md",
            "Dockerfile",
            "tools/gen.py",
            "src/pkg/data.json",
            "tests/fixtures/page.html",
            "docs/requirements.txt",
        ],
    )
    def test_does_not_select_tests(self, rel: str) -> None:
        assert not is_python_test_trigger("nlp-pipeline", rel)

    def test_infra_any_py_selects(self) -> None:
        assert is_python_test_trigger("infra/lambda", "handlers/x.py")
        assert is_python_test_trigger("infra/lambda", "pyproject.toml")
        assert not is_python_test_trigger("infra/lambda", "main.tf")


class TestTsTriggers:
    @pytest.mark.parametrize(
        "rel",
        [
            "src/app/page.tsx",
            "src/lib/x.ts",
            "app/page.tsx",
            "tests/unit.ts",
            "__tests__/a.test.js",
            "lib/util.jsx",
            "package.json",
            "package-lock.json",
            "tsconfig.json",
            "tsconfig.build.json",
            "vitest.config.ts",
            "eslint.config.mjs",
            "next.config.js",
        ],
    )
    def test_selects_tests(self, rel: str) -> None:
        assert is_ts_test_trigger("web", rel)

    @pytest.mark.parametrize(
        "rel",
        [
            "README.md",
            "migrations/001_init.sql",
            "src/data-access/schema.sql",
            "public/logo.svg",
            "scripts/run-tests.mjs",
        ],
    )
    def test_does_not_select_tests(self, rel: str) -> None:
        assert not is_ts_test_trigger("web", rel)

    def test_infra_is_never_ts(self) -> None:
        assert not is_ts_test_trigger("infra/lambda", "index.ts")


class TestSelectTestPackages:
    def test_pyproject_only_selects_python_package(self) -> None:
        assert select_test_packages(["packages/nlp-pipeline/pyproject.toml"]) == (
            ["nlp-pipeline"],
            [],
        )

    def test_mixed_sorted_and_deduplicated(self) -> None:
        files = [
            "packages/web/src/a.tsx",
            "packages/scraper-framework/src/b.py",
            "packages/nlp-pipeline/tests/test_c.py",
            "packages/nlp-pipeline/src/d.py",
            "packages/api/package.json",
            "infra/lambda/handler.py",
            "scripts/foo.py",
            "docs/x.md",
        ]
        assert select_test_packages(files) == (
            ["infra/lambda", "nlp-pipeline", "scraper-framework"],
            ["api", "web"],
        )

    def test_empty(self) -> None:
        assert select_test_packages([]) == ([], [])


class TestCli:
    def _run(self, stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_prints_lang_tab_id_lines(self) -> None:
        result = self._run(
            "packages/nlp-pipeline/pyproject.toml\n\npackages/web/src/a.tsx\n"
        )
        assert result.returncode == 0
        assert result.stdout == "python\tnlp-pipeline\nts\tweb\n"

    def test_no_packages_prints_nothing(self) -> None:
        result = self._run("docs/x.md\nscripts/foo.py\n")
        assert result.returncode == 0
        assert result.stdout == ""
