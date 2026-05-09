# venv: none
"""Unit tests for check_test_script_imports_mapped.py (#4452).

Covers:
  - Glob-to-regex conversion (mirror of helper)
  - Workflow YAML filter-block parsing
  - AST-based detection of scripts/*.py imports in test files
  - Filter-name resolution per test file (ingestion vs framework vs courts)
  - End-to-end check() with synthesized repo fixtures
  - Production-tree pass: the real worktree must be green after Layer 1
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Load the script as a module (the file is plain importable as
# check_test_script_imports_mapped, but use spec_from_file_location for
# parity with sibling tests and to avoid PYTHONPATH gymnastics).
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "check_test_script_imports_mapped.py"
)
spec = importlib.util.spec_from_file_location(
    "check_test_script_imports_mapped", SCRIPT_PATH
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["check_test_script_imports_mapped"] = mod
spec.loader.exec_module(mod)

glob_to_regex = mod.glob_to_regex
path_matches_any = mod.path_matches_any
PathsFilter = mod.PathsFilter
parse_filters_from_workflow = mod.parse_filters_from_workflow
list_top_level_scripts = mod.list_top_level_scripts
find_script_imports_in_file = mod.find_script_imports_in_file
required_filters_for_test = mod.required_filters_for_test
check = mod.check
INGESTION_TESTS = mod.INGESTION_TESTS


# ---------------------------------------------------------------------------
# Glob-to-regex
# ---------------------------------------------------------------------------


class TestGlobToRegex:
    def test_doublestar_matches_subtree(self) -> None:
        pat = glob_to_regex("packages/scraper-framework/**")
        assert pat.match("packages/scraper-framework/src/x.py")
        assert pat.match("packages/scraper-framework/tests/a/b/c.py")
        assert not pat.match("packages/nlp-pipeline/x.py")

    def test_exact_script_path(self) -> None:
        pat = glob_to_regex("scripts/rebuild_db.py")
        assert pat.match("scripts/rebuild_db.py")
        assert not pat.match("scripts/rebuild_db.sh")
        assert not pat.match("scripts/sub/rebuild_db.py")

    def test_quoted_glob_is_stripped(self) -> None:
        pat = glob_to_regex("'scripts/foo.py'")
        assert pat.match("scripts/foo.py")

    def test_path_matches_any(self) -> None:
        globs = [
            glob_to_regex("scripts/rebuild_db.py"),
            glob_to_regex("scripts/reingest_from_s3.py"),
        ]
        assert path_matches_any("scripts/rebuild_db.py", globs)
        assert path_matches_any("scripts/reingest_from_s3.py", globs)
        assert not path_matches_any("scripts/other.py", globs)


# ---------------------------------------------------------------------------
# parse_filters_from_workflow
# ---------------------------------------------------------------------------


class TestParseFilters:
    def test_simple_filters_block(self, tmp_path: Path) -> None:
        wf = tmp_path / "ci.yml"
        wf.write_text(
            textwrap.dedent(
                """\
                jobs:
                  detect-changes:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: dorny/paths-filter@v4
                        id: changes
                        with:
                          filters: |
                            scraper:
                              - 'packages/scraper-framework/**'
                              - 'scripts/reingest_from_s3.py'
                            scraper-framework:
                              - 'packages/scraper-framework/src/**'
                              - 'scripts/rebuild_db.py'
                """
            )
        )
        filters = parse_filters_from_workflow(wf)
        assert "scraper" in filters
        assert "scripts/reingest_from_s3.py" in filters["scraper"].positives
        assert "packages/scraper-framework/**" in filters["scraper"].positives
        assert "scripts/rebuild_db.py" in filters["scraper-framework"].positives

    def test_negative_entry(self, tmp_path: Path) -> None:
        wf = tmp_path / "ci.yml"
        wf.write_text(
            textwrap.dedent(
                """\
                jobs:
                  detect-changes:
                    steps:
                      - uses: dorny/paths-filter@v4
                        with:
                          filters: |
                            foo:
                              - 'packages/x/**'
                              - '!packages/x/excluded/**'
                """
            )
        )
        filters = parse_filters_from_workflow(wf)
        assert filters["foo"].positives == ["packages/x/**"]
        assert filters["foo"].negatives == ["packages/x/excluded/**"]
        # Coverage check confirms negative excludes
        assert filters["foo"].matches("packages/x/foo.py")
        assert not filters["foo"].matches("packages/x/excluded/foo.py")
        assert not filters["foo"].matches("other/foo.py")

    def test_no_filters_raises(self, tmp_path: Path) -> None:
        wf = tmp_path / "ci.yml"
        wf.write_text(
            textwrap.dedent(
                """\
                jobs:
                  no-filters:
                    runs-on: ubuntu-latest
                """
            )
        )
        with pytest.raises(ValueError):
            parse_filters_from_workflow(wf)


# ---------------------------------------------------------------------------
# list_top_level_scripts
# ---------------------------------------------------------------------------


class TestListTopLevelScripts:
    def test_scans_top_level_only(self, tmp_path: Path) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "foo.py").write_text("# script")
        (scripts / "bar-baz.py").write_text("# hyphen-named script")
        (scripts / "shell-script.sh").write_text("#!/bin/sh")  # ignored
        archive = scripts / "archive"
        archive.mkdir()
        (archive / "old.py").write_text("# archived")  # ignored

        result = list_top_level_scripts(scripts)
        # foo.py: stem == filename-without-suffix
        assert result["foo"] == "scripts/foo.py"
        # bar-baz.py: stem is "bar-baz"
        assert result["bar-baz"] == "scripts/bar-baz.py"
        # archived not included
        assert "old" not in result
        # non-py not included
        assert "shell-script" not in result


# ---------------------------------------------------------------------------
# find_script_imports_in_file
# ---------------------------------------------------------------------------


class TestImportDetection:
    def test_bare_import(self, tmp_path: Path) -> None:
        src = tmp_path / "test_a.py"
        src.write_text("import rebuild_db\nx = rebuild_db.func()\n")
        scripts = {"rebuild_db": "scripts/rebuild_db.py"}
        result = find_script_imports_in_file(src, "tests/test_a.py", scripts)
        assert len(result) == 1
        assert result[0].module_name == "rebuild_db"
        assert result[0].script_path == "scripts/rebuild_db.py"

    def test_from_import(self, tmp_path: Path) -> None:
        src = tmp_path / "test_b.py"
        src.write_text("from rebuild_db import foo\n")
        scripts = {"rebuild_db": "scripts/rebuild_db.py"}
        result = find_script_imports_in_file(src, "tests/test_b.py", scripts)
        assert len(result) == 1

    def test_importlib_import_module(self, tmp_path: Path) -> None:
        src = tmp_path / "test_c.py"
        src.write_text(
            'import importlib\nreingest = importlib.import_module("reingest_from_s3")\n'
        )
        scripts = {"reingest_from_s3": "scripts/reingest_from_s3.py"}
        result = find_script_imports_in_file(src, "tests/test_c.py", scripts)
        assert len(result) == 1
        assert result[0].module_name == "reingest_from_s3"
        assert result[0].script_path == "scripts/reingest_from_s3.py"

    def test_importlib_import_module_hyphen_name(self, tmp_path: Path) -> None:
        src = tmp_path / "test_d.py"
        src.write_text(
            "import importlib\n"
            'check = importlib.import_module("check-scraper-registry")\n'
        )
        scripts = {"check-scraper-registry": "scripts/check-scraper-registry.py"}
        result = find_script_imports_in_file(src, "tests/test_d.py", scripts)
        assert len(result) == 1
        assert result[0].module_name == "check-scraper-registry"

    def test_scripts_dot_prefix(self, tmp_path: Path) -> None:
        src = tmp_path / "test_e.py"
        src.write_text(
            "from scripts.rebuild_db import x\nimport scripts.reingest_from_s3 as r\n"
        )
        scripts = {
            "rebuild_db": "scripts/rebuild_db.py",
            "reingest_from_s3": "scripts/reingest_from_s3.py",
        }
        result = find_script_imports_in_file(src, "tests/test_e.py", scripts)
        names = {r.module_name for r in result}
        assert names == {"rebuild_db", "reingest_from_s3"}

    def test_unrelated_imports_ignored(self, tmp_path: Path) -> None:
        src = tmp_path / "test_f.py"
        src.write_text("import os\nfrom datetime import date\nimport pytest\n")
        scripts = {"rebuild_db": "scripts/rebuild_db.py"}
        result = find_script_imports_in_file(src, "tests/test_f.py", scripts)
        assert result == []

    def test_archived_script_ignored(self, tmp_path: Path) -> None:
        """If a test imports a script that doesn't exist (archived), the guard
        ignores it — that's a separate hygiene problem."""
        src = tmp_path / "test_g.py"
        src.write_text("import backfill_archived_thing\n")
        scripts = {"rebuild_db": "scripts/rebuild_db.py"}
        result = find_script_imports_in_file(src, "tests/test_g.py", scripts)
        assert result == []

    def test_syntax_error_silent(self, tmp_path: Path) -> None:
        src = tmp_path / "test_h.py"
        src.write_text("class S(:\n    pass\n")  # syntax error
        scripts = {"rebuild_db": "scripts/rebuild_db.py"}
        result = find_script_imports_in_file(src, "tests/test_h.py", scripts)
        assert result == []

    def test_dedup_per_file(self, tmp_path: Path) -> None:
        """Multiple imports of the same module emit one TestImport per file."""
        src = tmp_path / "test_i.py"
        src.write_text(
            "import rebuild_db\n"
            "from rebuild_db import x\n"
            "import importlib\n"
            'rebuild_db_again = importlib.import_module("rebuild_db")\n'
        )
        scripts = {"rebuild_db": "scripts/rebuild_db.py"}
        result = find_script_imports_in_file(src, "tests/test_i.py", scripts)
        assert len(result) == 1
        assert result[0].module_name == "rebuild_db"


# ---------------------------------------------------------------------------
# required_filters_for_test
# ---------------------------------------------------------------------------


class TestFilterMapping:
    def test_ingestion_tests_route_to_scraper_filter(self) -> None:
        for path in INGESTION_TESTS:
            assert required_filters_for_test(path) == ["scraper"]

    def test_courts_tests_route_to_scraper_courts_filter(self) -> None:
        assert required_filters_for_test("tests/courts/test_x.py") == ["scraper-courts"]
        assert required_filters_for_test("tests/courts/ca/test_y.py") == [
            "scraper-courts"
        ]

    def test_default_routes_to_scraper_framework_filter(self) -> None:
        assert required_filters_for_test("tests/test_rebuild_db.py") == [
            "scraper-framework"
        ]
        assert required_filters_for_test("tests/ingestion/test_doc_timing.py") == [
            "scraper-framework"
        ]


# ---------------------------------------------------------------------------
# End-to-end check()
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build a minimal fake repo and return (repo_root, scripts_dir, tests_dir, workflow_path)."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    pkg_tests = repo / "packages" / "scraper-framework" / "tests"
    workflow = repo / ".github" / "workflows" / "ci.yml"

    scripts.mkdir(parents=True)
    pkg_tests.mkdir(parents=True)
    workflow.parent.mkdir(parents=True)
    return repo, scripts, pkg_tests, workflow


def _write_workflow(
    workflow: Path,
    scraper_paths: list[str],
    framework_paths: list[str],
    courts_paths: list[str] | None = None,
) -> None:
    """Write a minimal workflow with `scraper:` and `scraper-framework:` filters.

    Indentation matches the production ci.yml layout (8 spaces for filter
    name, 14 for entries) so the parser exercises the same shape as in CI.
    """
    if courts_paths is None:
        courts_paths = ["packages/scraper-framework/src/courts/**"]

    def _block(name: str, paths: list[str]) -> str:
        header = " " * 12 + f"{name}:"
        entries = "\n".join(" " * 14 + f"- '{p}'" for p in paths)
        return header + "\n" + entries

    text = (
        "jobs:\n"
        "  detect-changes:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: dorny/paths-filter@v4\n"
        "        with:\n"
        "          filters: |\n"
        + _block("scraper", scraper_paths)
        + "\n"
        + _block("scraper-framework", framework_paths)
        + "\n"
        + _block("scraper-courts", courts_paths)
        + "\n"
    )
    workflow.write_text(text)


class TestEndToEnd:
    def test_clean_repo_no_violations(self, tmp_path: Path) -> None:
        repo, scripts, tests, wf = _fake_repo(tmp_path)
        (scripts / "rebuild_db.py").write_text("# script\n")
        (tests / "test_rebuild_db.py").write_text(
            'import importlib\nrebuild_db = importlib.import_module("rebuild_db")\n'
        )
        _write_workflow(
            wf,
            scraper_paths=["packages/scraper-framework/**"],
            framework_paths=["packages/scraper-framework/**", "scripts/rebuild_db.py"],
        )
        violations = check(tests, wf, scripts, repo)
        assert violations == []

    def test_missing_in_framework_filter(self, tmp_path: Path) -> None:
        repo, scripts, tests, wf = _fake_repo(tmp_path)
        (scripts / "rebuild_db.py").write_text("# script\n")
        (tests / "test_rebuild_db.py").write_text(
            'import importlib\nrebuild_db = importlib.import_module("rebuild_db")\n'
        )
        _write_workflow(
            wf,
            scraper_paths=["packages/scraper-framework/**"],
            framework_paths=["packages/scraper-framework/**"],  # no rebuild_db
        )
        violations = check(tests, wf, scripts, repo)
        assert len(violations) == 1
        assert violations[0].script_path == "scripts/rebuild_db.py"
        assert violations[0].missing_filter == "scraper-framework"

    def test_missing_in_scraper_filter_for_ingestion_test(self, tmp_path: Path) -> None:
        repo, scripts, tests, wf = _fake_repo(tmp_path)
        (scripts / "reingest_from_s3.py").write_text("# script\n")
        (tests / "test_reingest_from_s3.py").write_text(
            'import importlib\nr = importlib.import_module("reingest_from_s3")\n'
        )
        _write_workflow(
            wf,
            scraper_paths=["packages/scraper-framework/**"],  # missing reingest
            framework_paths=[
                "packages/scraper-framework/**",
                "scripts/reingest_from_s3.py",
            ],
        )
        violations = check(tests, wf, scripts, repo)
        # ingestion tests gate on `scraper`, so this fails the scraper filter
        assert len(violations) == 1
        assert violations[0].missing_filter == "scraper"

    def test_archived_script_ignored(self, tmp_path: Path) -> None:
        """A test that imports a non-existent (archived) script does not
        produce a violation — the script isn't in scripts/*.py so the guard
        cannot map it to a filter."""
        repo, scripts, tests, wf = _fake_repo(tmp_path)
        (tests / "test_x.py").write_text("import backfill_archived_thing\n")
        _write_workflow(wf, scraper_paths=["x/**"], framework_paths=["x/**"])
        violations = check(tests, wf, scripts, repo)
        assert violations == []

    def test_courts_test_routes_to_scraper_courts(self, tmp_path: Path) -> None:
        repo, scripts, tests, wf = _fake_repo(tmp_path)
        (scripts / "rebuild_db.py").write_text("# script\n")
        courts_dir = tests / "courts"
        courts_dir.mkdir()
        (courts_dir / "test_court_x.py").write_text(
            'import importlib\nrebuild_db = importlib.import_module("rebuild_db")\n'
        )
        _write_workflow(
            wf,
            scraper_paths=["x/**"],
            framework_paths=["x/**", "scripts/rebuild_db.py"],
        )
        violations = check(tests, wf, scripts, repo)
        # courts test routes to `scraper-courts`, which doesn't include rebuild_db
        assert len(violations) == 1
        assert violations[0].missing_filter == "scraper-courts"

    def test_glob_filter_match(self, tmp_path: Path) -> None:
        """A wildcard `scripts/*.py` filter entry should cover any script."""
        repo, scripts, tests, wf = _fake_repo(tmp_path)
        (scripts / "rebuild_db.py").write_text("# script\n")
        (tests / "test_rebuild_db.py").write_text(
            'import importlib\nrebuild_db = importlib.import_module("rebuild_db")\n'
        )
        _write_workflow(
            wf,
            scraper_paths=["packages/scraper-framework/**"],
            framework_paths=[
                "packages/scraper-framework/**",
                "scripts/*.py",  # broad wildcard
            ],
        )
        violations = check(tests, wf, scripts, repo)
        assert violations == []


# ---------------------------------------------------------------------------
# Production tree (defense-in-depth)
# ---------------------------------------------------------------------------


def test_production_tree_passes() -> None:
    """The real worktree must be green — i.e. ci.yml's filters cover every
    test-imported script today.  After Layer 1 lands, this test ensures the
    invariant doesn't drift."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    rc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "check_test_script_imports_mapped.py"),
        ],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, (
        f"Production tree failed the check (expected pass after Layer 1).\n"
        f"stdout:\n{rc.stdout}\nstderr:\n{rc.stderr}"
    )
