# venv: none
"""Unit tests for check-ci-guards-skip-list-coverage.py (#4379).

Three synthetic-fixture scenarios per the AC:
  1. Passing — every required-arg guard is in SKIP_LIST or carries marker.
  2. Failing — a required-arg guard is missing from SKIP_LIST AND has no
     marker; the check must exit 1 and name the guard in the Fix block.
  3. Marker-rescued — a required-arg guard is missing from SKIP_LIST but
     carries the ``# ci-guards: skip`` marker; the check must exit 0.

Plus end-to-end tests against the real ``scripts/`` tree:
  * ``test_real_scripts_dir_passes`` — every actual argument-required guard
    is covered. This is the regression gate for the #4372-class bug — if
    a future PR ships a new required-arg guard without updating SKIP_LIST,
    this test catches it.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Load the script as a module (hyphenated filename — can't ``import`` directly).
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "check-ci-guards-skip-list-coverage.py"
)
spec = importlib.util.spec_from_file_location(
    "check_ci_guards_skip_list_coverage", SCRIPT_PATH
)
assert spec is not None and spec.loader is not None
check_ci_guards = importlib.util.module_from_spec(spec)
sys.modules["check_ci_guards_skip_list_coverage"] = check_ci_guards
spec.loader.exec_module(check_ci_guards)


# ---------------------------------------------------------------------------
# Test fixtures: build a synthetic scripts/ tree with three guards
# ---------------------------------------------------------------------------


def _write_umbrella(tmp_path: Path, skip_list: list[str]) -> Path:
    """Write a minimal scripts/run-ci-guards.sh whose SKIP_LIST mirrors the input.

    The umbrella's actual logic doesn't run here — only the SKIP_LIST=( ... )
    block is parsed by the meta-check. We emit just enough scaffolding for
    the parser to find the array, plus the entries themselves.
    """

    entries = "\n".join(f'    "{name}"' for name in skip_list)
    body = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        # synthetic test umbrella
        # permanent: true
        SKIP_LIST=(
        {entries}
        )
        """
    )
    path = tmp_path / "run-ci-guards.sh"
    path.write_text(body)
    return path


def _write_required_py_guard(tmp_path: Path, name: str, marker: bool = False) -> Path:
    """Create a synthetic check-*.py guard with argparse required=True."""

    marker_line = "# ci-guards: skip\n" if marker else ""
    body = (
        "#!/usr/bin/env python3\n"
        "# venv: none\n"
        "# permanent: true\n"
        f"{marker_line}"
        '"""Synthetic guard."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--issue", required=True)\n'
        "args = parser.parse_args()\n"
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_required_sh_guard(tmp_path: Path, name: str, marker: bool = False) -> Path:
    """Create a synthetic check-*.sh guard with ``${1:?...}``."""

    marker_line = "# ci-guards: skip\n" if marker else ""
    body = (
        "#!/usr/bin/env bash\n"
        "# permanent: true\n"
        f"{marker_line}"
        'ISSUE="${1:?Usage: synthetic-guard <issue>}"\n'
        'echo "$ISSUE"\n'
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_optional_py_guard(tmp_path: Path, name: str) -> Path:
    """Create a synthetic check-*.py guard with no required args."""

    body = (
        "#!/usr/bin/env python3\n"
        "# venv: none\n"
        "# permanent: true\n"
        '"""Synthetic guard with optional args."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--maybe", default=None)\n'
        "args = parser.parse_args()\n"
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_optional_sh_guard(tmp_path: Path, name: str) -> Path:
    """Create a synthetic check-*.sh guard with defaulted positional."""

    body = '#!/usr/bin/env bash\n# permanent: true\narg="${1:-default}"\necho "$arg"\n'
    path = tmp_path / name
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# parse_skip_list
# ---------------------------------------------------------------------------


class TestParseSkipList:
    def test_parses_entries(self, tmp_path: Path) -> None:
        umbrella = _write_umbrella(
            tmp_path,
            ["check-foo.sh", "check-bar.py", "check-baz.sh"],
        )
        result = check_ci_guards.parse_skip_list(umbrella)
        assert result == {"check-foo.sh", "check-bar.py", "check-baz.sh"}

    def test_empty_list(self, tmp_path: Path) -> None:
        umbrella = _write_umbrella(tmp_path, [])
        result = check_ci_guards.parse_skip_list(umbrella)
        assert result == set()

    def test_missing_block_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "no-skip-list.sh"
        path.write_text("#!/usr/bin/env bash\necho hi\n")
        with pytest.raises(ValueError, match="Could not locate SKIP_LIST"):
            check_ci_guards.parse_skip_list(path)


# ---------------------------------------------------------------------------
# Argument-required detection
# ---------------------------------------------------------------------------


class TestRequiredArgDetection:
    def test_python_required_true_flagged(self, tmp_path: Path) -> None:
        path = _write_required_py_guard(tmp_path, "check-thing.py")
        assert check_ci_guards.is_argument_required(path)

    def test_python_optional_only_not_flagged(self, tmp_path: Path) -> None:
        path = _write_optional_py_guard(tmp_path, "check-thing.py")
        assert not check_ci_guards.is_argument_required(path)

    def test_python_mutex_required_group_flagged(self, tmp_path: Path) -> None:
        # Mirrors check-issue-verify-sql.py:608 — the canonical pattern.
        body = (
            "#!/usr/bin/env python3\n"
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "src = parser.add_mutually_exclusive_group(required=True)\n"
            'src.add_argument("--issue", type=int)\n'
            'src.add_argument("--body-file")\n'
        )
        path = tmp_path / "check-mutex.py"
        path.write_text(body)
        assert check_ci_guards.is_argument_required(path)

    def test_shell_strict_positional_flagged(self, tmp_path: Path) -> None:
        path = _write_required_sh_guard(tmp_path, "check-thing.sh")
        assert check_ci_guards.is_argument_required(path)

    def test_shell_strict_positional_no_message_flagged(self, tmp_path: Path) -> None:
        # ``${1:?}`` (empty error message) is also a strict-required
        # positional and must be detected.
        body = '#!/usr/bin/env bash\n# permanent: true\nARG="${1:?}"\necho "$ARG"\n'
        path = tmp_path / "check-bare.sh"
        path.write_text(body)
        assert check_ci_guards.is_argument_required(path)

    def test_shell_defaulted_positional_not_flagged(self, tmp_path: Path) -> None:
        path = _write_optional_sh_guard(tmp_path, "check-thing.sh")
        assert not check_ci_guards.is_argument_required(path)

    def test_unknown_extension_not_flagged(self, tmp_path: Path) -> None:
        # The dispatcher only looks at .py / .sh — a hypothetical .rb
        # would be skipped. (No-op in practice; defensive coverage.)
        path = tmp_path / "check-thing.rb"
        path.write_text("# pretend Ruby script\n")
        assert not check_ci_guards.is_argument_required(path)


# ---------------------------------------------------------------------------
# has_opt_out_marker
# ---------------------------------------------------------------------------


class TestOptOutMarker:
    def test_marker_present_top(self, tmp_path: Path) -> None:
        path = tmp_path / "check-marked.sh"
        path.write_text("#!/usr/bin/env bash\n# ci-guards: skip\necho hi\n")
        assert check_ci_guards.has_opt_out_marker(path)

    def test_marker_present_with_extra_spaces(self, tmp_path: Path) -> None:
        path = tmp_path / "check-marked.sh"
        path.write_text("#!/usr/bin/env bash\n#   ci-guards:   skip   \necho hi\n")
        assert check_ci_guards.has_opt_out_marker(path)

    def test_marker_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "check-bare.sh"
        path.write_text("#!/usr/bin/env bash\necho hi\n")
        assert not check_ci_guards.has_opt_out_marker(path)

    def test_marker_beyond_window_not_found(self, tmp_path: Path) -> None:
        # Push the marker past line 20 — must NOT be detected.
        body = (
            "#!/usr/bin/env bash\n"
            + ("# filler\n" * 25)
            + "# ci-guards: skip\n"
            + "echo hi\n"
        )
        path = tmp_path / "check-far.sh"
        path.write_text(body)
        assert not check_ci_guards.has_opt_out_marker(path)


# ---------------------------------------------------------------------------
# Three-fixture AC scenarios (synthetic scripts/ tree)
# ---------------------------------------------------------------------------


class TestThreeFixtureScenarios:
    """Per AC #2: seed three synthetic guards (passing, missing, marker-rescued)
    and assert the correct verdict for each."""

    def _build_tree(self, tmp_path: Path) -> tuple[Path, Path]:
        """Return (scripts_dir, umbrella_path)."""

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        # Passing: required-arg guard IS in SKIP_LIST.
        _write_required_sh_guard(scripts_dir, "check-passing.sh")
        # Failing: required-arg guard NOT in SKIP_LIST and NO marker.
        _write_required_py_guard(scripts_dir, "check-missing.py", marker=False)
        # Marker-rescued: required-arg guard NOT in SKIP_LIST but HAS marker.
        _write_required_sh_guard(scripts_dir, "check-marker.sh", marker=True)
        umbrella = _write_umbrella(scripts_dir, ["check-passing.sh"])
        return scripts_dir, umbrella

    def test_full_tree_reports_only_missing(self, tmp_path: Path) -> None:
        scripts_dir, umbrella = self._build_tree(tmp_path)
        skip_list = check_ci_guards.parse_skip_list(umbrella)
        violations = check_ci_guards.find_violations(scripts_dir, skip_list)
        names = sorted(p.name for p in violations)
        assert names == ["check-missing.py"]

    def test_passing_alone_no_violations(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_required_sh_guard(scripts_dir, "check-passing.sh")
        umbrella = _write_umbrella(scripts_dir, ["check-passing.sh"])
        skip_list = check_ci_guards.parse_skip_list(umbrella)
        assert check_ci_guards.find_violations(scripts_dir, skip_list) == []

    def test_marker_alone_no_violations(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_required_sh_guard(scripts_dir, "check-marker.sh", marker=True)
        umbrella = _write_umbrella(scripts_dir, [])
        skip_list = check_ci_guards.parse_skip_list(umbrella)
        assert check_ci_guards.find_violations(scripts_dir, skip_list) == []

    def test_missing_alone_one_violation(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_required_py_guard(scripts_dir, "check-missing.py", marker=False)
        umbrella = _write_umbrella(scripts_dir, [])
        skip_list = check_ci_guards.parse_skip_list(umbrella)
        violations = check_ci_guards.find_violations(scripts_dir, skip_list)
        assert [p.name for p in violations] == ["check-missing.py"]


# ---------------------------------------------------------------------------
# discover_check_scripts: exclusion + filtering
# ---------------------------------------------------------------------------


class TestDiscoverCheckScripts:
    def test_self_excluded(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_required_py_guard(
            scripts_dir, "check-ci-guards-skip-list-coverage.py", marker=False
        )
        _write_required_py_guard(scripts_dir, "check-other.py", marker=False)
        names = sorted(
            p.name for p in check_ci_guards.discover_check_scripts(scripts_dir)
        )
        assert names == ["check-other.py"]

    def test_non_check_files_skipped(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "tool.py").write_text("# not a check\n")
        (scripts_dir / "README.md").write_text("# docs\n")
        _write_required_py_guard(scripts_dir, "check-real.py")
        names = sorted(
            p.name for p in check_ci_guards.discover_check_scripts(scripts_dir)
        )
        assert names == ["check-real.py"]

    def test_subdir_files_skipped(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        sub = scripts_dir / "tests"
        sub.mkdir()
        (sub / "check-fake.py").write_text("# in subdir\n")
        _write_required_py_guard(scripts_dir, "check-real.py")
        names = sorted(
            p.name for p in check_ci_guards.discover_check_scripts(scripts_dir)
        )
        assert names == ["check-real.py"]


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


class TestCli:
    def _build_failing_tree(self, tmp_path: Path) -> Path:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_required_py_guard(scripts_dir, "check-missing.py", marker=False)
        _write_umbrella(scripts_dir, [])
        return scripts_dir

    def test_cli_exits_zero_on_clean_tree(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_optional_py_guard(scripts_dir, "check-fine.py")
        _write_umbrella(scripts_dir, [])
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--scripts-dir",
                str(scripts_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_cli_exits_one_on_violation(self, tmp_path: Path) -> None:
        scripts_dir = self._build_failing_tree(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--scripts-dir",
                str(scripts_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "check-missing.py" in result.stderr

    def test_cli_emits_fix_block_on_violation(self, tmp_path: Path) -> None:
        # AC: emit a copy-pasteable Fix: block per the §Hygiene-check
        # guards Fix-block contract.
        scripts_dir = self._build_failing_tree(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--scripts-dir",
                str(scripts_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        # Fix block markers — both options must be present.
        assert re.search(r"^Fix:", result.stderr, flags=re.MULTILINE), (
            f"Fix: line missing from stderr:\n{result.stderr}"
        )
        assert "Option A" in result.stderr
        assert "Option B" in result.stderr
        # The literal SKIP_LIST entry the operator would paste must
        # name the violating guard.
        assert '"check-missing.py"' in result.stderr
        # The marker hint must include the literal opt-out comment.
        assert "# ci-guards: skip" in result.stderr

    def test_cli_exit_two_on_missing_umbrella(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        # No umbrella file — exit 2.
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--scripts-dir",
                str(scripts_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "umbrella not found" in result.stderr.lower()

    def test_cli_exit_two_on_missing_scripts_dir(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--scripts-dir",
                str(tmp_path / "nope"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2

    def test_cli_default_scans_repo(self) -> None:
        # No --scripts-dir → scans the real scripts/ directory the script
        # lives in. The real repo state must be clean (every required-arg
        # guard either in SKIP_LIST or marked).
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            "Real scripts/ has a required-arg guard missing from "
            "run-ci-guards.sh's SKIP_LIST.\n" + result.stderr
        )


# ---------------------------------------------------------------------------
# End-to-end: the real scripts/ directory must pass.
# ---------------------------------------------------------------------------


class TestRealScriptsDir:
    """The repo's actual scripts/ tree is the regression gate.

    Per AC #5: this test would have caught #4372 before merge — when
    ``check-issue-verify-sql.py`` shipped without a SKIP_LIST entry, this
    test (and the CI guard) would have failed.
    """

    def test_real_scripts_dir_passes(self) -> None:
        scripts_dir = SCRIPT_PATH.parent
        umbrella = scripts_dir / "run-ci-guards.sh"
        skip_list = check_ci_guards.parse_skip_list(umbrella)
        violations = check_ci_guards.find_violations(scripts_dir, skip_list)
        assert violations == [], (
            "Required-arg guards missing from run-ci-guards.sh SKIP_LIST:\n"
            + "\n".join(f"  {p.name}" for p in violations)
        )

    def test_real_scripts_dir_skip_list_parses(self) -> None:
        # Sanity: the umbrella's SKIP_LIST is parseable. If the umbrella
        # gets refactored out from under us, this fails loudly instead of
        # silently dropping every guard from the check.
        umbrella = SCRIPT_PATH.parent / "run-ci-guards.sh"
        skip_list = check_ci_guards.parse_skip_list(umbrella)
        # The list is non-empty (sanity — there are documented entries).
        assert len(skip_list) > 0
        # check-issue-verify-sql.py must be in the list (regression gate
        # for #4372 — if a future refactor accidentally drops it, this
        # test catches that).
        assert "check-issue-verify-sql.py" in skip_list


# ---------------------------------------------------------------------------
# Regression simulation: AC #5 — without the #4372 fix, the check fails.
# ---------------------------------------------------------------------------


class TestPre4372Regression:
    """AC #5: revert the #4372 SKIP_LIST entry locally and confirm exit 1.

    We don't actually mutate the umbrella in the worktree — instead, we
    pass a synthetic umbrella whose SKIP_LIST omits ``check-issue-verify-sql.py``
    while pointing the check at the real scripts/ dir. This simulates the
    pre-#4372 state exactly.
    """

    def test_pre_4372_state_fails(self, tmp_path: Path) -> None:
        scripts_dir = SCRIPT_PATH.parent
        real_umbrella = scripts_dir / "run-ci-guards.sh"
        real_skip = check_ci_guards.parse_skip_list(real_umbrella)
        # Drop the #4372 entry — this is the pre-#4372 SKIP_LIST.
        broken_skip = real_skip - {"check-issue-verify-sql.py"}
        # Build a synthetic umbrella with the broken list so we don't
        # touch the working-tree umbrella.
        broken_umbrella = _write_umbrella(tmp_path, sorted(broken_skip))
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--scripts-dir",
                str(scripts_dir),
                "--umbrella",
                str(broken_umbrella),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, (
            "Expected meta-check to fail when check-issue-verify-sql.py is "
            "missing from SKIP_LIST.\nstdout: "
            + result.stdout
            + "\nstderr: "
            + result.stderr
        )
        assert "check-issue-verify-sql.py" in result.stderr

    def test_post_4372_state_passes(self) -> None:
        # The current real umbrella with the #4372 fix in place — the
        # check must exit 0. (Same as test_real_scripts_dir_passes but
        # framed as "the fix is in place.")
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
