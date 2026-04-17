# venv: none
"""Unit tests for check-ci-job-skipped.py (#2527).

Covers:
  - Glob-to-regex conversion for dorny/paths-filter syntax.
  - ci.yml parsing (filters block + job bodies + if-gate extraction).
  - Diff parsing (which ci.yml lines changed; which files changed).
  - End-to-end find_skipped_modified_jobs() with realistic fixtures.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

# Load the script as a module (it has a hyphen in its name)
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "check-ci-job-skipped.py"
spec = importlib.util.spec_from_file_location("check_ci_job_skipped", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
check_ci_job_skipped = importlib.util.module_from_spec(spec)
sys.modules["check_ci_job_skipped"] = check_ci_job_skipped
spec.loader.exec_module(check_ci_job_skipped)

glob_to_regex = check_ci_job_skipped.glob_to_regex
path_matches_any = check_ci_job_skipped.path_matches_any
parse_ci_yaml = check_ci_job_skipped.parse_ci_yaml
parse_unified_diff = check_ci_job_skipped.parse_unified_diff
find_skipped_modified_jobs = check_ci_job_skipped.find_skipped_modified_jobs


# ---------------------------------------------------------------------------
# glob_to_regex
# ---------------------------------------------------------------------------


class TestGlobToRegex:
    def test_recursive_doublestar(self) -> None:
        pat = glob_to_regex("packages/scraper-framework/**")
        assert pat.match("packages/scraper-framework/src/foo.py")
        assert pat.match("packages/scraper-framework/tests/x/y/z.py")
        assert pat.match("packages/scraper-framework/")  # bare prefix with slash
        assert not pat.match("packages/nlp-pipeline/src/foo.py")
        assert not pat.match("scripts/foo.py")

    def test_single_star_in_dir(self) -> None:
        pat = glob_to_regex("scripts/*.py")
        assert pat.match("scripts/foo.py")
        assert pat.match("scripts/check_something.py")
        assert not pat.match("scripts/sub/foo.py")
        assert not pat.match("scripts/foo.sh")

    def test_exact_path(self) -> None:
        pat = glob_to_regex(".github/workflows/ci.yml")
        assert pat.match(".github/workflows/ci.yml")
        assert not pat.match(".github/workflows/deploy.yml")
        assert not pat.match("other/.github/workflows/ci.yml")

    def test_quoted_glob_is_stripped(self) -> None:
        pat = glob_to_regex("'packages/foo/**'")
        assert pat.match("packages/foo/bar.py")

    def test_leading_double_star_slash(self) -> None:
        pat = glob_to_regex("**/ci.yml")
        assert pat.match("ci.yml")
        assert pat.match(".github/workflows/ci.yml")

    def test_path_matches_any_accepts_first(self) -> None:
        globs = [
            glob_to_regex("packages/scraper-framework/**"),
            glob_to_regex(".github/workflows/ci.yml"),
        ]
        assert path_matches_any(".github/workflows/ci.yml", globs)
        assert path_matches_any("packages/scraper-framework/src/a.py", globs)
        assert not path_matches_any("docs/other.md", globs)


# ---------------------------------------------------------------------------
# ci.yml parsing fixtures
# ---------------------------------------------------------------------------


MINIMAL_CI_YML = textwrap.dedent(
    """\
    name: CI

    on:
      push:

    jobs:
      detect-changes:
        runs-on: ubuntu-latest
        outputs:
          scripts: ${{ steps.changes.outputs.scripts }}
          hooks: ${{ steps.changes.outputs.hooks }}
          scraper: ${{ steps.changes.outputs.scraper }}
        steps:
          - uses: actions/checkout@v6
          - uses: dorny/paths-filter@v4
            id: changes
            with:
              filters: |
                scripts:
                  - 'scripts/**'
                  - '.github/workflows/ci.yml'
                hooks:
                  - '.claude/hooks/**'
                  - '.github/workflows/ci.yml'
                scraper:
                  - 'packages/scraper-framework/**'

      scripts-tests:
        needs: detect-changes
        if: needs.detect-changes.outputs.scripts == 'true'
        runs-on: ubuntu-latest
        steps:
          - name: Test
            run: echo hi

      preflight-hook-test:
        needs: detect-changes
        if: needs.detect-changes.outputs.hooks == 'true'
        runs-on: ubuntu-latest
        steps:
          - name: Test
            run: echo hi

      scraper-tests:
        needs: detect-changes
        if: needs.detect-changes.outputs.scraper == 'true'
        runs-on: ubuntu-latest
        steps:
          - run: echo hi

      always-runs:
        runs-on: ubuntu-latest
        steps:
          - run: echo hi

      multi-filter:
        needs: detect-changes
        if: needs.detect-changes.outputs.scripts == 'true' || needs.detect-changes.outputs.scraper == 'true'
        runs-on: ubuntu-latest
        steps:
          - run: echo hi
    """
)


@pytest.fixture
def minimal_ci_path(tmp_path: Path) -> Path:
    p = tmp_path / "ci.yml"
    p.write_text(MINIMAL_CI_YML)
    return p


class TestParseCIYaml:
    def test_filters_extracted(self, minimal_ci_path: Path) -> None:
        ci = parse_ci_yaml(minimal_ci_path)
        assert ci.filters["scripts"] == ["scripts/**", ".github/workflows/ci.yml"]
        assert ci.filters["hooks"] == [".claude/hooks/**", ".github/workflows/ci.yml"]
        assert ci.filters["scraper"] == ["packages/scraper-framework/**"]

    def test_jobs_found(self, minimal_ci_path: Path) -> None:
        ci = parse_ci_yaml(minimal_ci_path)
        names = [j.name for j in ci.jobs]
        assert names == [
            "detect-changes",
            "scripts-tests",
            "preflight-hook-test",
            "scraper-tests",
            "always-runs",
            "multi-filter",
        ]

    def test_if_gate_filter(self, minimal_ci_path: Path) -> None:
        ci = parse_ci_yaml(minimal_ci_path)
        by_name = {j.name: j for j in ci.jobs}
        assert by_name["scripts-tests"].if_gate_filter == "scripts"
        assert by_name["preflight-hook-test"].if_gate_filter == "hooks"
        assert by_name["scraper-tests"].if_gate_filter == "scraper"
        # always-runs has no `if:`
        assert by_name["always-runs"].if_gate_filter is None
        # multi-filter references both scripts and scraper
        assert by_name["multi-filter"].if_gate_filter == "scripts|scraper"

    def test_line_ranges_are_sane(self, minimal_ci_path: Path) -> None:
        ci = parse_ci_yaml(minimal_ci_path)
        by_name = {j.name: j for j in ci.jobs}
        # Job bodies are non-empty
        for name, job in by_name.items():
            assert job.start_line < job.end_line, f"{name}: {job}"

        # scripts-tests must START strictly after detect-changes ends
        assert by_name["scripts-tests"].start_line > by_name["detect-changes"].start_line

    def test_parses_real_ci_yml(self) -> None:
        """Parse the real repo ci.yml and ensure filters + jobs come out sane."""
        repo_ci = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
        if not repo_ci.is_file():
            pytest.skip("real ci.yml not present")
        ci = parse_ci_yaml(repo_ci)
        # Sanity: expected filters present
        assert "scripts" in ci.filters
        assert "hooks" in ci.filters
        assert "scraper" in ci.filters
        # Both scripts and hooks filters include ci.yml itself post-#2505 / #2410
        assert ".github/workflows/ci.yml" in ci.filters["scripts"]
        assert ".github/workflows/ci.yml" in ci.filters["hooks"]
        # Expected gated jobs present
        names = {j.name for j in ci.jobs}
        assert "scripts-tests" in names
        assert "preflight-hook-test" in names
        # scripts-tests is gated on 'scripts'
        by_name = {j.name: j for j in ci.jobs}
        assert by_name["scripts-tests"].if_gate_filter == "scripts"


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


class TestParseUnifiedDiff:
    def test_basic_changed_files(self) -> None:
        diff = textwrap.dedent(
            """\
            diff --git a/scripts/foo.sh b/scripts/foo.sh
            index abc..def 100755
            --- a/scripts/foo.sh
            +++ b/scripts/foo.sh
            @@ -1,3 +1,3 @@
             hello
            -old
            +new
             world
            """
        )
        r = parse_unified_diff(diff, ".github/workflows/ci.yml")
        assert r.changed_files == ["scripts/foo.sh"]
        assert r.ci_yml_changed_lines == set()

    def test_ci_yml_changed_lines(self) -> None:
        diff = textwrap.dedent(
            """\
            diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
            index abc..def 100644
            --- a/.github/workflows/ci.yml
            +++ b/.github/workflows/ci.yml
            @@ -10,3 +10,4 @@
             context-a
             context-b
            -deleted-line
            +replacement-line
            +brand-new-line
            """
        )
        r = parse_unified_diff(diff, ".github/workflows/ci.yml")
        assert r.changed_files == [".github/workflows/ci.yml"]
        # + lines are at new-side lines 12 and 13 (after 2 context lines starting at 10)
        assert 12 in r.ci_yml_changed_lines
        assert 13 in r.ci_yml_changed_lines

    def test_pure_deletion_is_detected(self) -> None:
        """Deletion-only hunks flag exactly `new_start` — a synthetic marker
        so body-level pure deletes are detected without claiming unchanged
        context lines are "modified"."""
        diff = textwrap.dedent(
            """\
            diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
            --- a/.github/workflows/ci.yml
            +++ b/.github/workflows/ci.yml
            @@ -20,4 +20,2 @@
             ctx-a
            -gone-1
            -gone-2
             ctx-b
            """
        )
        r = parse_unified_diff(diff, ".github/workflows/ci.yml")
        # new_start (line 20) is flagged as a synthetic marker. Context
        # lines (21) are NOT flagged — they're unchanged on the new side.
        assert 20 in r.ci_yml_changed_lines
        assert 21 not in r.ci_yml_changed_lines

    def test_context_lines_never_marked_as_changed(self) -> None:
        """A pure-addition hunk with context should only flag `+` lines."""
        diff = textwrap.dedent(
            """\
            diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
            --- a/.github/workflows/ci.yml
            +++ b/.github/workflows/ci.yml
            @@ -100,3 +100,5 @@
             ctx-a
             ctx-b
            +new-1
            +new-2
             ctx-c
            """
        )
        r = parse_unified_diff(diff, ".github/workflows/ci.yml")
        # Only the two `+` lines (new-side 102 and 103) are marked.
        # Context lines 100, 101, 104 are NOT marked.
        assert r.ci_yml_changed_lines == {102, 103}

    def test_non_ci_yml_changes_dont_record_ci_lines(self) -> None:
        diff = textwrap.dedent(
            """\
            diff --git a/docs/foo.md b/docs/foo.md
            --- a/docs/foo.md
            +++ b/docs/foo.md
            @@ -1,1 +1,1 @@
            -old
            +new
            """
        )
        r = parse_unified_diff(diff, ".github/workflows/ci.yml")
        assert r.changed_files == ["docs/foo.md"]
        assert r.ci_yml_changed_lines == set()


# ---------------------------------------------------------------------------
# find_skipped_modified_jobs (end-to-end)
# ---------------------------------------------------------------------------


class TestFindSkippedModifiedJobs:
    def _get_job_range(self, ci, name: str) -> tuple[int, int]:
        job = next(j for j in ci.jobs if j.name == name)
        return (job.start_line, job.end_line)

    def test_repro_2505_first_commit(self, minimal_ci_path: Path) -> None:
        """Reproduce #2505: modifying scripts-tests body without touching any
        scripts/**-matching file AND without ci.yml being in the filter."""
        ci = parse_ci_yaml(minimal_ci_path)
        # Remove ci.yml from 'scripts' filter to simulate pre-fix state.
        ci.filters["scripts"] = ["scripts/**"]

        start, end = self._get_job_range(ci, "scripts-tests")
        # Simulate a change at some line inside scripts-tests body
        ci_lines = {start + 3}
        # Changed files: only ci.yml itself
        changed_files = [".github/workflows/ci.yml"]

        problems = find_skipped_modified_jobs(ci, changed_files, ci_lines)
        assert len(problems) == 1
        assert problems[0].job_name == "scripts-tests"
        assert problems[0].filter_name == "scripts"

    def test_2505_fix_passes(self, minimal_ci_path: Path) -> None:
        """With ci.yml in the 'scripts' filter (current state), changing
        scripts-tests + ci.yml should NOT flag."""
        ci = parse_ci_yaml(minimal_ci_path)
        # ci.yml IS in filter (as configured in the fixture).
        assert ".github/workflows/ci.yml" in ci.filters["scripts"]

        start, _end = self._get_job_range(ci, "scripts-tests")
        ci_lines = {start + 3}
        changed_files = [".github/workflows/ci.yml"]

        problems = find_skipped_modified_jobs(ci, changed_files, ci_lines)
        assert problems == []

    def test_unchanged_ci_yml_no_problems(self, minimal_ci_path: Path) -> None:
        ci = parse_ci_yaml(minimal_ci_path)
        changed_files = ["scripts/new-tool.sh"]
        problems = find_skipped_modified_jobs(ci, changed_files, set())
        assert problems == []

    def test_always_runs_job_not_flagged(self, minimal_ci_path: Path) -> None:
        """A modified job with no `if:` gate is never a problem."""
        ci = parse_ci_yaml(minimal_ci_path)
        start, _end = self._get_job_range(ci, "always-runs")
        problems = find_skipped_modified_jobs(
            ci, [".github/workflows/ci.yml"], {start + 1}
        )
        assert problems == []

    def test_multi_filter_gate_any_match_is_ok(self, minimal_ci_path: Path) -> None:
        """Job gated on A || B passes if either filter matches."""
        ci = parse_ci_yaml(minimal_ci_path)
        start, _end = self._get_job_range(ci, "multi-filter")
        # ci.yml changed. 'scripts' filter includes ci.yml so the job WILL run.
        problems = find_skipped_modified_jobs(
            ci, [".github/workflows/ci.yml"], {start + 1}
        )
        assert problems == []

    def test_multi_filter_gate_no_match_flags(self, minimal_ci_path: Path) -> None:
        """Job gated on A || B fails if neither filter matches."""
        ci = parse_ci_yaml(minimal_ci_path)
        # Remove ci.yml from the 'scripts' filter so 'multi-filter' (scripts ||
        # scraper) can't run when only ci.yml changes.
        ci.filters["scripts"] = ["scripts/**"]
        start, _end = self._get_job_range(ci, "multi-filter")
        problems = find_skipped_modified_jobs(
            ci, [".github/workflows/ci.yml"], {start + 1}
        )
        assert len(problems) == 1
        assert problems[0].job_name == "multi-filter"
        assert problems[0].filter_name == "scripts|scraper"

    def test_multiple_problem_jobs(self, minimal_ci_path: Path) -> None:
        ci = parse_ci_yaml(minimal_ci_path)
        # Pre-fix state: ci.yml not in either filter
        ci.filters["scripts"] = ["scripts/**"]
        ci.filters["hooks"] = [".claude/hooks/**"]

        s_start, _ = self._get_job_range(ci, "scripts-tests")
        h_start, _ = self._get_job_range(ci, "preflight-hook-test")
        ci_lines = {s_start + 1, h_start + 1}
        problems = find_skipped_modified_jobs(
            ci, [".github/workflows/ci.yml"], ci_lines
        )
        problem_names = sorted(p.job_name for p in problems)
        assert problem_names == ["preflight-hook-test", "scripts-tests"]

    def test_changed_matching_file_prevents_flag(self, minimal_ci_path: Path) -> None:
        """Even without ci.yml in filter, a changed file matching the filter
        means the job will actually run."""
        ci = parse_ci_yaml(minimal_ci_path)
        ci.filters["scripts"] = ["scripts/**"]  # no ci.yml

        start, _end = self._get_job_range(ci, "scripts-tests")
        # Change scripts-tests body AND change a file matching scripts/**
        ci_lines = {start + 2}
        changed_files = [".github/workflows/ci.yml", "scripts/new-tool.sh"]
        problems = find_skipped_modified_jobs(ci, changed_files, ci_lines)
        assert problems == []


# ---------------------------------------------------------------------------
# Issue #2527 acceptance-criteria simulation
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria:
    """These tests encode the issue's acceptance criteria verification."""

    def test_toy_pr_that_would_skip_scripts_tests_fails(
        self, tmp_path: Path
    ) -> None:
        """AC1: a PR that modifies scripts-tests without touching scripts/**
        AND without .github/workflows/ci.yml in the filter is flagged."""
        ci_path = tmp_path / "ci.yml"
        # Construct a minimal ci.yml where 'scripts' filter does NOT include ci.yml
        ci_path.write_text(
            textwrap.dedent(
                """\
                name: CI
                on:
                  push:
                jobs:
                  detect-changes:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: dorny/paths-filter@v4
                        id: changes
                        with:
                          filters: |
                            scripts:
                              - 'scripts/**'
                  scripts-tests:
                    needs: detect-changes
                    if: needs.detect-changes.outputs.scripts == 'true'
                    runs-on: ubuntu-latest
                    steps:
                      - name: Auto-discover tests
                        run: echo new-implementation
                """
            )
        )
        ci = parse_ci_yaml(ci_path)
        start, _end = self._get_job_range(ci, "scripts-tests")
        problems = find_skipped_modified_jobs(
            ci, [".github/workflows/ci.yml"], {start + 1}
        )
        assert len(problems) == 1
        assert problems[0].job_name == "scripts-tests"

    def test_2505_post_fix_passes(self, minimal_ci_path: Path) -> None:
        """AC2: after the filter includes ci.yml, the same diff passes."""
        ci = parse_ci_yaml(minimal_ci_path)
        # ci.yml IS in 'scripts' filter per fixture (mirrors post-fix state).
        start, _end = self._get_job_range(ci, "scripts-tests")
        problems = find_skipped_modified_jobs(
            ci, [".github/workflows/ci.yml"], {start + 1}
        )
        assert problems == []

    def _get_job_range(self, ci, name: str) -> tuple[int, int]:
        job = next(j for j in ci.jobs if j.name == name)
        return (job.start_line, job.end_line)
