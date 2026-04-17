# venv: none
"""Unit tests for check-script-headers.py (#2533)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Load the script as a module (hyphenated filename).
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "check-script-headers.py"
spec = importlib.util.spec_from_file_location("check_script_headers", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
check_script_headers = importlib.util.module_from_spec(spec)
sys.modules["check_script_headers"] = check_script_headers
spec.loader.exec_module(check_script_headers)

matches_one_off_pattern = check_script_headers.matches_one_off_pattern
has_marker = check_script_headers.has_marker
iter_candidate_files = check_script_headers.iter_candidate_files
find_unmarked_scripts = check_script_headers.find_unmarked_scripts
ONE_OFF_NAME_PATTERNS = check_script_headers.ONE_OFF_NAME_PATTERNS


# ---------------------------------------------------------------------------
# matches_one_off_pattern
# ---------------------------------------------------------------------------


class TestMatchesOneOffPattern:
    @pytest.mark.parametrize(
        "filename",
        [
            "backfill_llm_enrichment.py",
            "cleanup_sd_bad_rulings.py",
            "fix_riverside_enrichment.py",
            "dedup_judges.py",
            "merge_honorific_judges.py",
            "migrate_s3_keys.py",
            "remediate_enrichment_fixtures.py",
        ],
    )
    def test_matching_names(self, filename: str) -> None:
        assert matches_one_off_pattern(filename)

    @pytest.mark.parametrize(
        "filename",
        [
            "audit_field_completeness.py",
            "check-test-except-pass.py",
            "rebuild_db.py",
            "reingest_from_s3.py",
            "phase_timer.py",
            "screenshot.py",
        ],
    )
    def test_non_matching_names(self, filename: str) -> None:
        assert not matches_one_off_pattern(filename)

    def test_case_insensitive(self) -> None:
        assert matches_one_off_pattern("Backfill_Something.py")
        assert matches_one_off_pattern("CLEANUP.py")

    def test_patterns_coverage(self) -> None:
        # All documented fragments match a canonical filename.
        assert set(ONE_OFF_NAME_PATTERNS) == {
            "backfill",
            "cleanup",
            "fix",
            "dedup",
            "merge",
            "migrate",
            "remediat",
        }


# ---------------------------------------------------------------------------
# has_marker
# ---------------------------------------------------------------------------


class TestHasMarker:
    def _write(self, tmp_path: Path, name: str, content: str) -> Path:
        target = tmp_path / name
        target.write_text(textwrap.dedent(content).lstrip("\n"))
        return target

    def test_one_off_at_line_3(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            """
            #!/usr/bin/env python3
            \"\"\"Short docstring.\"\"\"
            # one-off: true
            from __future__ import annotations
            """,
        )
        assert has_marker(path)

    def test_permanent_at_line_3(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            """
            #!/usr/bin/env python3
            \"\"\"Short.\"\"\"
            # permanent: true
            """,
        )
        assert has_marker(path)

    def test_marker_after_long_docstring(self, tmp_path: Path) -> None:
        # Reproduces the backfill_llm_enrichment.py pattern (#2533): a
        # long module docstring pushes the marker to line ~32. The
        # default 50-line window must still catch it.
        docstring_lines = "\n".join(f"  line {i}" for i in range(1, 26))
        # Build the file literally rather than via dedent — avoids the
        # common-leading-whitespace trap with mixed indentation levels.
        content = (
            "#!/usr/bin/env python3\n"
            '"""Long docstring follows.\n'
            "\n"
            f"{docstring_lines}\n"
            '"""\n'
            "# venv: scraper-framework\n"
            "# permanent: true\n"
            "from __future__ import annotations\n"
        )
        path = tmp_path / "backfill_foo.py"
        path.write_text(content)
        # Sanity check: marker should land on line ~31.
        lines = content.splitlines()
        assert "# permanent: true" in lines[30], (
            f"expected marker near line 31, got: {lines[28:33]}"
        )
        assert has_marker(path)

    def test_marker_beyond_window_not_found(self, tmp_path: Path) -> None:
        # Force the marker onto line 60 — beyond the default window.
        padding = "\n".join(["# filler"] * 60)
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            f"""
            #!/usr/bin/env python3
            {padding}
            # one-off: true
            """,
        )
        assert not has_marker(path)

    def test_no_marker(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            """
            #!/usr/bin/env python3
            \"\"\"No marker at all.\"\"\"
            from __future__ import annotations
            """,
        )
        assert not has_marker(path)

    def test_marker_with_extra_spaces(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            """
            #   one-off:   true
            """,
        )
        assert has_marker(path)

    def test_marker_inside_docstring_does_not_count(self, tmp_path: Path) -> None:
        # The marker pattern embedded inside a docstring is NOT a
        # top-level comment line (the docstring body line starts with
        # whitespace before any ``#``), so it should not be detected.
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            """
            \"\"\"Docstring mentioning '# one-off: true' as text.\"\"\"
            from __future__ import annotations
            """,
        )
        assert not has_marker(path)

    def test_custom_window(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "backfill_foo.py",
            """
            #!/usr/bin/env python3
            # line 2
            # line 3
            # one-off: true
            """,
        )
        assert has_marker(path, window=4)
        assert not has_marker(path, window=3)


# ---------------------------------------------------------------------------
# iter_candidate_files
# ---------------------------------------------------------------------------


class TestIterCandidateFiles:
    def test_lists_top_level_py_only(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# a\n")
        (tmp_path / "b.sh").write_text("# b\n")
        (tmp_path / "c.py").write_text("# c\n")

        result = iter_candidate_files([tmp_path])
        names = sorted(p.name for p in result)
        assert names == ["a.py", "c.py"]

    def test_skips_excluded_subdirs(self, tmp_path: Path) -> None:
        (tmp_path / "top.py").write_text("# top\n")
        (tmp_path / "archive").mkdir()
        (tmp_path / "archive" / "old.py").write_text("# old\n")
        (tmp_path / "eval").mkdir()
        (tmp_path / "eval" / "runner.py").write_text("# runner\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "t.py").write_text("# t\n")

        result = iter_candidate_files([tmp_path])
        names = sorted(p.name for p in result)
        assert names == ["top.py"]

    def test_skips_unknown_subdirs(self, tmp_path: Path) -> None:
        # Unknown subdirectories are also skipped — callers must pass
        # them explicitly if they want to scan them.
        (tmp_path / "top.py").write_text("# top\n")
        (tmp_path / "unknown").mkdir()
        (tmp_path / "unknown" / "file.py").write_text("# file\n")

        result = iter_candidate_files([tmp_path])
        names = sorted(p.name for p in result)
        assert names == ["top.py"]

    def test_explicit_file_paths_accepted(self, tmp_path: Path) -> None:
        f = tmp_path / "backfill_x.py"
        f.write_text("# one-off: true\n")
        result = iter_candidate_files([f])
        assert result == [f]

    def test_always_exempt_basenames_filtered(self, tmp_path: Path) -> None:
        f = tmp_path / "check-script-headers.py"
        f.write_text("# the check script itself\n")
        # Passed as explicit file: exempt.
        assert iter_candidate_files([f]) == []
        # Found via directory scan: exempt.
        assert iter_candidate_files([tmp_path]) == []

    def test_missing_path_ignored(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert iter_candidate_files([missing]) == []


# ---------------------------------------------------------------------------
# find_unmarked_scripts
# ---------------------------------------------------------------------------


class TestFindUnmarkedScripts:
    def test_unmarked_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "backfill_missing.py").write_text(
            '"""Missing marker."""\nfrom __future__ import annotations\n'
        )
        result = find_unmarked_scripts([tmp_path])
        assert [p.name for p in result] == ["backfill_missing.py"]

    def test_marked_script_is_ok(self, tmp_path: Path) -> None:
        (tmp_path / "backfill_marked.py").write_text('"""Marked."""\n# one-off: true\n')
        assert find_unmarked_scripts([tmp_path]) == []

    def test_non_matching_name_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "utility.py").write_text('"""Not a one-off name."""\n')
        assert find_unmarked_scripts([tmp_path]) == []

    def test_permanent_marker_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "backfill_perma.py").write_text(
            '"""Permanent utility."""\n# permanent: true\n'
        )
        assert find_unmarked_scripts([tmp_path]) == []

    def test_marker_beyond_window_flagged(self, tmp_path: Path) -> None:
        padding = "\n".join(["# filler"] * 60)
        (tmp_path / "backfill_late.py").write_text(
            f"#!/usr/bin/env python3\n{padding}\n# one-off: true\n"
        )
        result = find_unmarked_scripts([tmp_path])
        assert [p.name for p in result] == ["backfill_late.py"]

    def test_scripts_in_archive_subdir_not_scanned(self, tmp_path: Path) -> None:
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "backfill_old.py").write_text('"""No marker but archived."""\n')
        # Default scan excludes archive/, so no findings:
        assert find_unmarked_scripts([tmp_path]) == []


# ---------------------------------------------------------------------------
# End-to-end: real scripts/ directory should pass.
# ---------------------------------------------------------------------------


class TestAgainstRealScriptsDir:
    def test_real_scripts_dir_passes(self) -> None:
        # The repo's own scripts/ must pass this check at all times;
        # otherwise the baseline is broken and a human should add the
        # missing markers.
        scripts_dir = SCRIPT_PATH.parent
        unmarked = find_unmarked_scripts([scripts_dir])
        assert unmarked == [], "Unmarked one-off scripts detected:\n" + "\n".join(
            f"  {p}" for p in unmarked
        )


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_exits_zero_on_clean_dir(self, tmp_path: Path) -> None:
        (tmp_path / "utility.py").write_text('"""Fine."""\n')
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_cli_exits_one_on_violation(self, tmp_path: Path) -> None:
        (tmp_path / "backfill_missing.py").write_text('"""No marker."""\n')
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "backfill_missing.py" in result.stderr

    def test_cli_default_scans_repo_scripts(self) -> None:
        # No args -> scans the scripts/ directory where the check lives.
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        # The real scripts/ directory should pass.
        assert result.returncode == 0, result.stderr
