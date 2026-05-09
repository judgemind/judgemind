# venv: none
"""Unit tests for check-no-check-prefix-on-ecs-oneshots.py (#4563).

Five synthetic-fixture scenarios per the AC:

  (a) ``check-foo-data.py`` with ``# venv: scraper-framework`` +
      ``# permanent: true`` + ``required=True`` argparse — fails.
  (b) ``check-foo.sh`` with ``# venv: scraper-framework`` +
      ``# permanent: true`` + ``${1:?}`` — fails.
  (c) Legitimate CI guard like ``check-no-unbounded-timeouts.py`` (no
      ``# venv: <pkg>`` non-none, no ``# permanent: true`` /
      stdlib-only, no ``required=True``) — passes.
  (d) The three grandfathered allowlist entries — pass.
  (e) A non-``check-`` prefixed ECS oneshot like ``cc-dual-run-diff.py``
      — passes (out of glob scope).

Plus end-to-end tests against the real ``scripts/`` tree:

  * ``test_real_scripts_dir_passes`` — the post-merge tree is clean.
    This is the regression gate — if a future PR ships a new
    ECS-oneshot data-check named ``check-*``, this test catches it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Load the script as a module (hyphenated filename — can't ``import`` directly).
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "check-no-check-prefix-on-ecs-oneshots.py"
)
spec = importlib.util.spec_from_file_location(
    "check_no_check_prefix_on_ecs_oneshots", SCRIPT_PATH
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
sys.modules["check_no_check_prefix_on_ecs_oneshots"] = guard
spec.loader.exec_module(guard)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _write_check_foo_data_py(tmp_path: Path, name: str = "check-foo-data.py") -> Path:
    """Synthetic ECS-oneshot Python script — three signals present."""

    body = (
        "#!/usr/bin/env python3\n"
        "# venv: scraper-framework\n"
        "# permanent: true\n"
        '"""Synthetic ECS-oneshot data-check script."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--date", required=True)\n'
        "args = parser.parse_args()\n"
        'print(f"date={args.date}")\n'
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_check_foo_sh(tmp_path: Path, name: str = "check-foo.sh") -> Path:
    """Synthetic ECS-oneshot shell script — three signals present."""

    body = (
        "#!/usr/bin/env bash\n"
        "# venv: scraper-framework\n"
        "# permanent: true\n"
        'DATE="${1:?Usage: check-foo.sh <YYYY-MM-DD>}"\n'
        'echo "date=$DATE"\n'
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_legitimate_ci_guard(
    tmp_path: Path, name: str = "check-no-unbounded-timeouts.py"
) -> Path:
    """Synthetic legitimate CI guard — stdlib-only, no required arg."""

    body = (
        "#!/usr/bin/env python3\n"
        "# venv: none\n"
        "# permanent: true\n"
        '"""Synthetic legitimate CI guard."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--scripts-dir", default=None)\n'
        "args = parser.parse_args()\n"
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_grandfathered_streak(
    tmp_path: Path, name: str = "check-scraper-zero-record-streak.py"
) -> Path:
    """Synthetic version of one of the three grandfathered allowlist entries.

    Real ``check-scraper-zero-record-streak.py`` carries ``# venv:
    scraper-framework`` + ``# permanent: true`` but does NOT have
    ``required=True`` today (its threshold / scraper / lookback args
    are all defaulted). That means under the strict rule it would not
    fire even without the allowlist. We synthesize the worst-case
    shape (all three signals present) to confirm the allowlist
    correctly exempts the grandfathered names independent of whether
    they would fire today — defensive behaviour against a future
    refactor adding ``required=True`` to one of the three.
    """

    body = (
        "#!/usr/bin/env python3\n"
        "# venv: scraper-framework\n"
        "# permanent: true\n"
        '"""Synthetic grandfathered guard."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--scraper", required=True)\n'
        "args = parser.parse_args()\n"
    )
    path = tmp_path / name
    path.write_text(body)
    return path


def _write_non_check_prefixed_oneshot(
    tmp_path: Path, name: str = "cc-dual-run-diff.py"
) -> Path:
    """Synthetic ECS oneshot WITHOUT the check- prefix.

    All three ECS-oneshot signals are present, but the basename does
    not match the ``check-*.{sh,py}`` glob — so this guard's scanner
    skips it entirely (out of scope).
    """

    body = (
        "#!/usr/bin/env python3\n"
        "# venv: scraper-framework\n"
        "# permanent: true\n"
        '"""Synthetic non-check-prefixed ECS oneshot."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--date", required=True)\n'
        "args = parser.parse_args()\n"
    )
    path = tmp_path / name
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Header / signal detection — unit-level
# ---------------------------------------------------------------------------


class TestHeaderDetection:
    def test_packaged_venv_header_detected(self, tmp_path: Path) -> None:
        path = _write_check_foo_data_py(tmp_path)
        assert guard.has_packaged_venv_header(path)

    def test_venv_none_not_packaged(self, tmp_path: Path) -> None:
        path = _write_legitimate_ci_guard(tmp_path)
        assert not guard.has_packaged_venv_header(path)

    def test_no_venv_header_not_packaged(self, tmp_path: Path) -> None:
        body = '#!/usr/bin/env python3\n"""No venv header at all."""\n'
        path = tmp_path / "check-no-header.py"
        path.write_text(body)
        assert not guard.has_packaged_venv_header(path)

    def test_permanent_header_detected(self, tmp_path: Path) -> None:
        path = _write_check_foo_data_py(tmp_path)
        assert guard.has_permanent_header(path)

    def test_one_off_header_not_permanent(self, tmp_path: Path) -> None:
        body = (
            "#!/usr/bin/env python3\n# venv: none\n# one-off: true\n"
            '"""One-off, not permanent."""\n'
        )
        path = tmp_path / "check-oneoff.py"
        path.write_text(body)
        assert not guard.has_permanent_header(path)

    def test_python_required_arg_detected(self, tmp_path: Path) -> None:
        path = _write_check_foo_data_py(tmp_path)
        assert guard.has_python_required_arg(path)

    def test_python_optional_only_not_flagged(self, tmp_path: Path) -> None:
        path = _write_legitimate_ci_guard(tmp_path)
        assert not guard.has_python_required_arg(path)

    def test_shell_strict_positional_detected(self, tmp_path: Path) -> None:
        path = _write_check_foo_sh(tmp_path)
        assert guard.has_shell_required_positional(path)

    def test_shell_defaulted_positional_not_flagged(self, tmp_path: Path) -> None:
        body = '#!/usr/bin/env bash\n# permanent: true\nARG="${1:-default}"\n'
        path = tmp_path / "check-defaulted.sh"
        path.write_text(body)
        assert not guard.has_shell_required_positional(path)


# ---------------------------------------------------------------------------
# is_ecs_oneshot — combined-signal classification
# ---------------------------------------------------------------------------


class TestIsEcsOneshot:
    def test_python_full_signals_is_ecs_oneshot(self, tmp_path: Path) -> None:
        path = _write_check_foo_data_py(tmp_path)
        assert guard.is_ecs_oneshot(path)

    def test_shell_full_signals_is_ecs_oneshot(self, tmp_path: Path) -> None:
        path = _write_check_foo_sh(tmp_path)
        assert guard.is_ecs_oneshot(path)

    def test_legitimate_ci_guard_is_not(self, tmp_path: Path) -> None:
        path = _write_legitimate_ci_guard(tmp_path)
        assert not guard.is_ecs_oneshot(path)

    def test_non_check_prefixed_oneshot_still_classified(self, tmp_path: Path) -> None:
        # ``is_ecs_oneshot`` itself doesn't filter on basename — the
        # discovery glob does. So this synthetic non-check-prefixed
        # oneshot still passes the classifier; the higher-level
        # ``find_violations`` is what filters on glob membership.
        path = _write_non_check_prefixed_oneshot(tmp_path)
        assert guard.is_ecs_oneshot(path)

    def test_packaged_venv_no_required_arg_not_oneshot(self, tmp_path: Path) -> None:
        # Edge case: a script with ``# venv: scraper-framework`` +
        # ``# permanent: true`` but NO required args is unusual but
        # legitimate (e.g. a scheduled probe with all defaulted
        # options). We do NOT flag it — the third signal is the
        # unambiguous distinguisher.
        body = (
            "#!/usr/bin/env python3\n# venv: scraper-framework\n"
            "# permanent: true\nimport argparse\n"
            "parser = argparse.ArgumentParser()\n"
            'parser.add_argument("--threshold", type=int, default=3)\n'
            "args = parser.parse_args()\n"
        )
        path = tmp_path / "check-no-required.py"
        path.write_text(body)
        assert not guard.is_ecs_oneshot(path)

    def test_required_arg_no_packaged_venv_not_oneshot(self, tmp_path: Path) -> None:
        # Edge case: a script with ``required=True`` and ``# permanent:
        # true`` but ``# venv: none`` (stdlib-only). Many legitimate
        # CI guards take ``--issue N`` or similar — they live in
        # ``run-ci-guards.sh``'s SKIP_LIST per #4379, not in this
        # guard's namespace. We do NOT flag them.
        body = (
            "#!/usr/bin/env python3\n# venv: none\n# permanent: true\n"
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            'parser.add_argument("--issue", required=True)\n'
            "args = parser.parse_args()\n"
        )
        path = tmp_path / "check-issue-thing.py"
        path.write_text(body)
        assert not guard.is_ecs_oneshot(path)


# ---------------------------------------------------------------------------
# discover_check_scripts + find_violations — directory-level
# ---------------------------------------------------------------------------


class TestFindViolations:
    def test_synthetic_python_oneshot_flagged(self, tmp_path: Path) -> None:
        """AC scenario (a): synthetic ``check-foo-data.py`` — fails."""

        _write_check_foo_data_py(tmp_path)
        violations = guard.find_violations(tmp_path)
        assert len(violations) == 1
        assert violations[0].name == "check-foo-data.py"

    def test_synthetic_shell_oneshot_flagged(self, tmp_path: Path) -> None:
        """AC scenario (b): synthetic ``check-foo.sh`` — fails."""

        _write_check_foo_sh(tmp_path)
        violations = guard.find_violations(tmp_path)
        assert len(violations) == 1
        assert violations[0].name == "check-foo.sh"

    def test_legitimate_ci_guard_passes(self, tmp_path: Path) -> None:
        """AC scenario (c): legitimate CI guard — passes."""

        _write_legitimate_ci_guard(tmp_path)
        violations = guard.find_violations(tmp_path)
        assert violations == []

    def test_grandfathered_allowlist_entries_pass(self, tmp_path: Path) -> None:
        """AC scenario (d): the three grandfathered allowlist entries — pass.

        Each carries the full three-signal ECS-oneshot shape (synthesized
        worst-case so the allowlist's defensive behaviour is exercised
        even though today's real grandfathered scripts don't have
        ``required=True``), but the allowlist exempts them.
        """

        for name in (
            "check-scraper-zero-record-streak.py",
            "check-scraper-zero-record-runner.py",
            "check-short-unsubstantive-rulings.py",
        ):
            _write_grandfathered_streak(tmp_path, name)

        violations = guard.find_violations(tmp_path)
        assert violations == []

    def test_non_check_prefixed_oneshot_skipped(self, tmp_path: Path) -> None:
        """AC scenario (e): non-``check-`` prefixed ECS oneshot — passes (out of glob scope)."""

        _write_non_check_prefixed_oneshot(tmp_path)
        violations = guard.find_violations(tmp_path)
        assert violations == []

    def test_mixed_tree(self, tmp_path: Path) -> None:
        """One offending Python guard + a legitimate CI guard + a grandfathered + a non-prefixed.

        Only the offender is flagged.
        """

        _write_check_foo_data_py(tmp_path)
        _write_legitimate_ci_guard(tmp_path)
        _write_grandfathered_streak(tmp_path)
        _write_non_check_prefixed_oneshot(tmp_path)

        violations = guard.find_violations(tmp_path)
        assert len(violations) == 1
        assert violations[0].name == "check-foo-data.py"

    def test_self_basename_excluded(self, tmp_path: Path) -> None:
        """The guard's own filenames are never in the discovered set."""

        # Drop a file named like the guard itself but with three
        # signals present. The discovery glob must skip it.
        _write_check_foo_data_py(
            tmp_path, name="check-no-check-prefix-on-ecs-oneshots.py"
        )
        violations = guard.find_violations(tmp_path)
        assert violations == []


# ---------------------------------------------------------------------------
# CLI behaviour — end-to-end
# ---------------------------------------------------------------------------


class TestCli:
    def _run_cli(
        self, *args: str, scripts_dir: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd: list[str] = ["python3", str(SCRIPT_PATH), *args]
        if scripts_dir is not None:
            cmd.extend(["--scripts-dir", str(scripts_dir)])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_cli_clean_tree_exit_zero(self, tmp_path: Path) -> None:
        _write_legitimate_ci_guard(tmp_path)
        result = self._run_cli(scripts_dir=tmp_path)
        assert result.returncode == 0, (
            f"stderr={result.stderr!r} stdout={result.stdout!r}"
        )

    def test_cli_violation_exit_one_with_fix_block(self, tmp_path: Path) -> None:
        _write_check_foo_data_py(tmp_path)
        result = self._run_cli(scripts_dir=tmp_path)
        assert result.returncode == 1
        # Violation list and Fix-block contract both surface in stderr.
        assert "check-foo-data.py" in result.stderr
        assert "Fix:" in result.stderr
        # Canonical-rename suggestion uses underscored Python form.
        assert "git mv scripts/check-foo-data.py scripts/foo_data.py" in result.stderr
        # Cite the upstream rationale (#4558).
        assert "#4558" in result.stderr

    def test_cli_shell_violation_renames_with_hyphens(self, tmp_path: Path) -> None:
        # Shell scripts in this repo use kebab-case — the rename
        # suggestion should keep hyphens (only Python gets underscored).
        _write_check_foo_sh(tmp_path)
        result = self._run_cli(scripts_dir=tmp_path)
        assert result.returncode == 1
        assert "git mv scripts/check-foo.sh scripts/foo.sh" in result.stderr

    def test_cli_list_mode_exit_zero(self, tmp_path: Path) -> None:
        _write_check_foo_data_py(tmp_path)
        _write_legitimate_ci_guard(tmp_path)
        result = self._run_cli("--list", scripts_dir=tmp_path)
        assert result.returncode == 0
        assert "Discovered:" in result.stdout
        assert "check-foo-data.py" in result.stdout
        assert "check-no-unbounded-timeouts.py" in result.stdout

    def test_cli_list_mode_grandfathered_tagged(self, tmp_path: Path) -> None:
        _write_grandfathered_streak(tmp_path)
        result = self._run_cli("--list", scripts_dir=tmp_path)
        assert result.returncode == 0
        assert "(grandfathered)" in result.stdout

    def test_cli_missing_scripts_dir_exit_two(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        result = self._run_cli(scripts_dir=missing)
        assert result.returncode == 2
        assert "scripts/ dir not found" in result.stderr


# ---------------------------------------------------------------------------
# End-to-end: real scripts/ tree must pass
# ---------------------------------------------------------------------------


class TestRealScriptsDir:
    """Regression gate against the real ``scripts/`` tree.

    If a future PR ships a new ECS-oneshot data-check script in the
    ``scripts/check-*.{sh,py}`` namespace, this test fails — closing
    the loop the #4558 docs section opened and the in-PR audit cycle
    relies on.
    """

    def test_real_scripts_dir_passes(self) -> None:
        scripts_dir = Path(__file__).resolve().parent.parent
        violations = guard.find_violations(scripts_dir)
        assert violations == [], (
            "Live ``scripts/`` tree contains check-*.{sh,py} guards "
            f"that match all three ECS-oneshot signals: "
            f"{[v.name for v in violations]}. "
            "Either rename them per #4558's naming rule, or add to "
            "_GRANDFATHERED_ALLOWLIST with a justification."
        )

    def test_grandfathered_allowlist_present_in_tree(self) -> None:
        """Every name in ``_GRANDFATHERED_ALLOWLIST`` must exist in the live tree.

        If a grandfathered script is renamed (the canonical fix per
        #4558), the allowlist must be updated in the same PR. This
        test catches the case where the allowlist drifts ahead of the
        tree — without it, the allowlist could harbour stale entries
        forever.
        """

        scripts_dir = Path(__file__).resolve().parent.parent
        for name in guard._GRANDFATHERED_ALLOWLIST:
            assert (scripts_dir / name).exists(), (
                f"Grandfathered allowlist entry {name!r} no longer "
                "exists in scripts/. Update _GRANDFATHERED_ALLOWLIST "
                "in scripts/check-no-check-prefix-on-ecs-oneshots.py."
            )


# ---------------------------------------------------------------------------
# Suggested-rename helper
# ---------------------------------------------------------------------------


class TestSuggestedRename:
    @pytest.mark.parametrize(
        "input_name,expected",
        [
            ("check-foo-data.py", "foo_data.py"),
            ("check-foo.sh", "foo.sh"),
            ("check-scraper-zero-record-streak.py", "scraper_zero_record_streak.py"),
            ("check-scraper-zero-record-runner.py", "scraper_zero_record_runner.py"),
            ("check-short-unsubstantive-rulings.py", "short_unsubstantive_rulings.py"),
            # No ``check-`` prefix — only the underscoring rule applies.
            ("foo-bar.py", "foo_bar.py"),
            ("foo-bar.sh", "foo-bar.sh"),
        ],
    )
    def test_rename_shape(self, input_name: str, expected: str) -> None:
        assert guard._suggested_rename(input_name) == expected
