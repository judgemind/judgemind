"""Tests for scripts/check-issue-verify-test-filename.py.

Covers AC#2 of issue #4549 — the canonical patterns the runner's
``is_helper`` filter classifies as helpers vs runnable tests:

  * ``_*.sh`` test path under ``scripts/tests/`` — fail (the runner
    silently skips it).
  * ``*-test.sh`` antipattern (anywhere) — fail (historical pre-#4545
    shape that originally surfaced via #4540).
  * ``test_<thing>.sh`` — pass (canonical default).
  * ``test__<thing>.sh`` — pass (canonical helper-test variant).

Plus AC#1: a body fixture mirroring #4540's buggy AC produces exit 1
with the offending path named in stderr.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader (the check script has dashes in the filename, so we have
# to load it via importlib rather than ``import``).
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check-issue-verify-test-filename.py"
_spec = importlib.util.spec_from_file_location(
    "check_issue_verify_test_filename", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
check_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mod)


# ---------------------------------------------------------------------------
# extract_verify_lines — pulling Verify: lines out of issue body text.
# ---------------------------------------------------------------------------


class TestExtractVerifyLines:
    def test_single_verify_line(self) -> None:
        body = "Verify: scripts/tests/test_foo.sh exits 0\n"
        lines = check_mod.extract_verify_lines(body)
        assert len(lines) == 1
        assert lines[0][0] == 1
        assert "Verify:" in lines[0][1]

    def test_dash_prefixed_verify(self) -> None:
        body = "- Verify: scripts/tests/test_foo.sh\n"
        lines = check_mod.extract_verify_lines(body)
        assert len(lines) == 1

    def test_indented_verify(self) -> None:
        body = "  Verify: scripts/tests/test_foo.sh\n"
        lines = check_mod.extract_verify_lines(body)
        assert len(lines) == 1

    def test_no_verify_returns_empty(self) -> None:
        body = "## Some heading\n\nRandom text. Nothing to validate.\n"
        lines = check_mod.extract_verify_lines(body)
        assert lines == []

    def test_multiple_verify_lines(self) -> None:
        body = (
            "- [ ] First.\n"
            "  Verify: scripts/tests/test_a.sh\n"
            "- [ ] Second.\n"
            "  Verify: scripts/tests/test_b.sh\n"
        )
        lines = check_mod.extract_verify_lines(body)
        assert len(lines) == 2
        assert lines[0][0] == 2
        assert lines[1][0] == 4

    def test_lowercase_verify_recognised(self) -> None:
        body = "verify: scripts/tests/test_foo.sh\n"
        lines = check_mod.extract_verify_lines(body)
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# find_violations — the filename-validation core.
# ---------------------------------------------------------------------------


class TestFindViolationsClean:
    def test_canonical_test_underscore(self) -> None:
        line = "Verify: scripts/tests/test_helpers.sh exits 0"
        assert check_mod.find_violations(line) == []

    def test_canonical_double_underscore(self) -> None:
        """The double-underscore disambiguator for tests of shared
        helpers passes the runner's ``is_helper`` filter."""
        line = "Verify: scripts/tests/test__test_helpers.sh exits 0"
        assert check_mod.find_violations(line) == []

    def test_pytest_command_passes(self) -> None:
        line = "Verify: pytest -k test_foo"
        assert check_mod.find_violations(line) == []

    def test_grep_command_passes(self) -> None:
        line = "Verify: grep -n 'pattern' file.py"
        assert check_mod.find_violations(line) == []

    def test_curl_command_passes(self) -> None:
        line = "Verify: curl -s https://example.com"
        assert check_mod.find_violations(line) == []

    def test_helper_path_in_grep_arg_still_flags(self) -> None:
        """Defensive: even when ``_helper.sh`` appears as a grep
        argument, the script flags it. False-positive risk is low —
        legitimate test files don't end up under
        ``scripts/tests/_*.sh``."""
        line = "Verify: grep -n 'foo' scripts/tests/_helpers.sh"
        violations = check_mod.find_violations(line)
        assert len(violations) == 1


class TestFindViolationsHelperPrefix:
    def test_canonical_buggy_4540_path(self) -> None:
        """The original buggy filename from #4540 must be flagged with
        a suggested replacement."""
        line = "Verify: scripts/tests/_test-helpers-test.sh exits 0"
        violations = check_mod.find_violations(line)
        # _test-helpers-test.sh matches the helper-prefix regex.
        # The dash-suffix regex would also match `test-helpers-test.sh`
        # but is suppressed because the helper-prefix already covers it.
        assert len(violations) == 1
        bad, suggestion = violations[0]
        assert bad == "scripts/tests/_test-helpers-test.sh"
        assert "test_test_helpers_test.sh" in suggestion
        assert "test__test_helpers_test.sh" in suggestion

    def test_simple_helper_prefix(self) -> None:
        line = "Verify: scripts/tests/_foo.sh exits 0"
        violations = check_mod.find_violations(line)
        assert len(violations) == 1
        assert violations[0][0] == "scripts/tests/_foo.sh"

    def test_helper_prefix_with_dashes(self) -> None:
        line = "Verify: scripts/tests/_my-helper.sh exits 0"
        violations = check_mod.find_violations(line)
        assert len(violations) == 1
        bad, suggestion = violations[0]
        assert bad == "scripts/tests/_my-helper.sh"
        # Dashes converted to underscores in the suggestion.
        assert "test_my_helper.sh" in suggestion

    def test_double_underscore_not_flagged_as_helper_prefix(self) -> None:
        """``test__<thing>.sh`` starts with ``t``, not ``_`` — the
        runner's filter only flags single-underscore-prefixed
        basenames."""
        line = "Verify: scripts/tests/test__helpers.sh exits 0"
        assert check_mod.find_violations(line) == []


class TestFindViolationsDashTestSuffix:
    def test_dash_test_suffix_flagged(self) -> None:
        """``foo-test.sh`` is the historical pre-#4545 antipattern."""
        line = "Verify: scripts/tests/foo-test.sh exits 0"
        violations = check_mod.find_violations(line)
        assert len(violations) == 1
        bad, suggestion = violations[0]
        assert bad == "foo-test.sh"
        assert "test_foo.sh" in suggestion

    def test_dash_test_suffix_outside_scripts_tests(self) -> None:
        """Flag the antipattern even when it appears outside
        ``scripts/tests/`` — the historical AC shape did not always
        anchor on the directory."""
        line = "Verify: ./my-test.sh"
        violations = check_mod.find_violations(line)
        assert len(violations) == 1

    def test_dash_test_suffix_with_underscores(self) -> None:
        line = "Verify: scripts/tests/my_thing-test.sh"
        violations = check_mod.find_violations(line)
        assert len(violations) == 1
        bad, suggestion = violations[0]
        assert bad == "my_thing-test.sh"
        assert "test_my_thing.sh" in suggestion


# ---------------------------------------------------------------------------
# check_body — full pipeline.
# ---------------------------------------------------------------------------


class TestCheckBody:
    def test_clean_body_returns_no_errors(self) -> None:
        body = (
            "## Acceptance criteria\n"
            "- [ ] First.\n"
            "  Verify: scripts/tests/test_foo.sh exits 0\n"
            "- [ ] Second.\n"
            "  Verify: scripts/tests/test__test_helpers.sh exits 0\n"
        )
        assert check_mod.check_body(body) == []

    def test_buggy_4540_ac_flagged(self) -> None:
        """A body containing the original buggy #4540 AC (which named
        ``scripts/tests/_test-helpers-test.sh``) must be flagged with
        the offending path in the diagnostic."""
        body = "Verify: scripts/tests/_test-helpers-test.sh exits 0\n"
        errors = check_mod.check_body(body)
        assert len(errors) == 1
        assert "scripts/tests/_test-helpers-test.sh" in errors[0]
        assert "is_helper" in errors[0]
        # And the suggestion is named.
        assert "test_test_helpers_test.sh" in errors[0]

    def test_multiple_verify_lines_each_validated(self) -> None:
        body = (
            "- [ ] First (clean).\n"
            "  Verify: scripts/tests/test_a.sh\n"
            "- [ ] Second (buggy).\n"
            "  Verify: scripts/tests/_b.sh\n"
            "- [ ] Third (also buggy — dash suffix).\n"
            "  Verify: scripts/tests/c-test.sh\n"
        )
        errors = check_mod.check_body(body)
        assert len(errors) == 2
        # First error names the helper-prefix path.
        assert any("scripts/tests/_b.sh" in e for e in errors)
        # Second error names the dash-suffix path.
        assert any("c-test.sh" in e for e in errors)

    def test_mixed_test_and_non_test_verify_lines(self) -> None:
        """Non-test Verify lines should be silently dropped, leaving
        the test-naming ones to be validated."""
        body = (
            "- [ ] First.\n"
            "  Verify: pytest -k test_foo\n"
            "- [ ] Second (buggy).\n"
            "  Verify: scripts/tests/_foo.sh\n"
            "- [ ] Third.\n"
            "  Verify: curl -s https://dev.api.judgemind.org/graphql\n"
        )
        errors = check_mod.check_body(body)
        assert len(errors) == 1
        assert "scripts/tests/_foo.sh" in errors[0]

    def test_no_verify_lines_returns_empty(self) -> None:
        body = "## Random body\n\nNothing to validate.\n"
        assert check_mod.check_body(body) == []


# ---------------------------------------------------------------------------
# main — CLI integration (only the body-file path; the gh path is
# exercised separately and gated on gh availability).
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_main_clean_body_returns_zero(self, tmp_path: Path) -> None:
        body = "Verify: scripts/tests/test_foo.sh exits 0\n"
        body_path = tmp_path / "body.txt"
        body_path.write_text(body)
        rc = check_mod.main(["--body-file", str(body_path)])
        assert rc == 0

    def test_main_buggy_body_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = "Verify: scripts/tests/_test-helpers-test.sh exits 0\n"
        body_path = tmp_path / "body.txt"
        body_path.write_text(body)
        rc = check_mod.main(["--body-file", str(body_path)])
        captured = capsys.readouterr()
        assert rc == 1
        # Diagnostic must name the offending path (AC#1).
        assert "scripts/tests/_test-helpers-test.sh" in captured.err
        # Per the Fix-block contract (docs/agent/code-standards.md
        # §"Hygiene-check guards: Fix-block contract"), the error
        # path must emit a labelled Fix: block pointing at the
        # canonical remediation.
        assert "Fix:" in captured.err
        assert "test_<thing>.sh" in captured.err
        assert "test__<thing>.sh" in captured.err

    def test_main_dash_suffix_body_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = "Verify: scripts/tests/foo-test.sh exits 0\n"
        body_path = tmp_path / "body.txt"
        body_path.write_text(body)
        rc = check_mod.main(["--body-file", str(body_path)])
        captured = capsys.readouterr()
        assert rc == 1
        assert "foo-test.sh" in captured.err

    def test_main_missing_body_file_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = check_mod.main(["--body-file", str(tmp_path / "nonexistent.txt")])
        captured = capsys.readouterr()
        assert rc == 2
        assert "failed to read body file" in captured.err

    def test_main_requires_input_source(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --issue or --body-file, argparse should error."""
        with pytest.raises(SystemExit) as excinfo:
            check_mod.main([])
        # argparse exits 2 on missing required arg.
        assert excinfo.value.code == 2
