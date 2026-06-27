# venv: none
"""Unit tests for check-dispatcher-breaker-recovery-alert.py (#4593).

The guard enforces that every config-flag circuit breaker in
``scripts/dispatcher/daemon.py`` declares a recovery path AND a Telegram
alert path (the design smell behind #4586 — the diagnoser breaker shipped
a one-way kill-switch flip with no recovery, the same defect #3779 fixed
for the overnight-safety breaker).

Two invariants:

  A — registry completeness: every autonomous breaker-attribution write
      (an ``updated_by = '<tag>'`` SQL literal whose tag is NOT in the
      operator-handler allowlist or a ``*_auto_recover`` / ``*_auto_close``
      recovery tag, plus the ``CAP_FLIPPED_BY_CIRCUIT_BREAKER`` constant
      value) must appear as a registry-entry ``tag``.
  B — recovery + alert methods exist: for each registry entry, daemon.py
      must contain ``def <recovery_method>(`` AND ``def <alert_method>(``.

Scenarios (per the task AC):

  (a) Real-tree pass — regression gate.
  (b) Registered breaker, recovery method ``def`` removed => exit 1.
  (c) Registered breaker, alert method ``def`` removed => exit 1.
  (d) New ``updated_by = 'rogue_breaker'`` trip write, unregistered => exit 1.
  (e) Synthetic compliant daemon => exit 0.
  (f) ``--list`` exits 0 and prints both registry tags.
  (g) Recovery-side tags (``*_auto_recover`` / ``*_auto_close``) are not
      treated as new breaker trip tags.
  plus the ``.sh`` wrapper subprocess end-to-end test on the real tree.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Load the hyphenated script as a module.
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "check-dispatcher-breaker-recovery-alert.py"
)
WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent
    / "check-dispatcher-breaker-recovery-alert.sh"
)
REAL_DAEMON_PATH = Path(__file__).resolve().parent.parent / "dispatcher" / "daemon.py"

spec = importlib.util.spec_from_file_location(
    "check_dispatcher_breaker_recovery_alert", SCRIPT_PATH
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
sys.modules["check_dispatcher_breaker_recovery_alert"] = guard
spec.loader.exec_module(guard)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _compliant_source() -> str:
    """Minimal daemon-ish source with both registered breakers compliant.

    Carries the regex-detectable tokens only: the cap-flipped constant,
    the two trip tags (overnight via the constant + parameterized
    ``updated_by = %s``, diagnoser via a literal ``updated_by = '...'``),
    the operator ``updated_by = 'daemon'`` writes, both recovery-side
    writes (``*_auto_close`` / ``*_auto_recover``), and the four method
    ``def`` lines (two recovery, two alert).
    """

    return (
        'CAP_FLIPPED_BY_CIRCUIT_BREAKER = "circuit_breaker"\n'
        "\n"
        "def _evaluate_circuit_breaker(self):\n"
        "    cur.execute(\n"
        '        "UPDATE dispatcher.config SET value = %s, "\n'
        '        "    updated_by = %s "\n'
        "        (CAP_FLIPPED_BY_CIRCUIT_BREAKER,),\n"
        "    )\n"
        "\n"
        "def _normal_write(self):\n"
        "    cur.execute(\"SET value = '0', updated_by = 'daemon' WHERE ...\")\n"
        "\n"
        "def _check_diagnoser_circuit_breaker(self):\n"
        "    cur.execute(\"SET updated_by = 'diagnoser_circuit_breaker' WHERE ...\")\n"
        "\n"
        "def _check_circuit_breaker_auto_close(self, current_cap):\n"
        "    cur.execute(\"SET updated_by = 'circuit_breaker_auto_close' WHERE ...\")\n"
        "\n"
        "def _send_circuit_breaker_telegram_alert(self):\n"
        "    pass\n"
        "\n"
        "def _check_diagnoser_breaker_auto_recover(self):\n"
        "    cur.execute(\"SET updated_by = 'diagnoser_circuit_breaker_auto_recover' WHERE ...\")\n"
        "\n"
        "def _send_diagnoser_breaker_telegram_alert(self):\n"
        "    pass\n"
    )


def _write(tmp_path: Path, source: str, name: str = "daemon.py") -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


# ---------------------------------------------------------------------------
# Tag extraction — unit level
# ---------------------------------------------------------------------------


class TestTagExtraction:
    def test_constant_value_extracted(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        tags = guard.discover_trip_tags(path)
        assert "circuit_breaker" in tags

    def test_literal_updated_by_extracted(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        tags = guard.discover_trip_tags(path)
        assert "diagnoser_circuit_breaker" in tags

    def test_daemon_tag_not_a_trip_tag(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        tags = guard.discover_trip_tags(path)
        assert "daemon" not in tags

    def test_auto_recover_tag_not_a_trip_tag(self, tmp_path: Path) -> None:
        """AC scenario (g): recovery-side tags are not new breaker trips."""

        path = _write(tmp_path, _compliant_source())
        tags = guard.discover_trip_tags(path)
        assert "diagnoser_circuit_breaker_auto_recover" not in tags

    def test_auto_close_tag_not_a_trip_tag(self, tmp_path: Path) -> None:
        """AC scenario (g): recovery-side tags are not new breaker trips."""

        path = _write(tmp_path, _compliant_source())
        tags = guard.discover_trip_tags(path)
        assert "circuit_breaker_auto_close" not in tags

    def test_method_defined_detected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        assert guard.method_defined(path, "_check_circuit_breaker_auto_close")
        assert guard.method_defined(path, "_send_diagnoser_breaker_telegram_alert")

    def test_method_not_defined(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        assert not guard.method_defined(path, "_nonexistent_method")


# ---------------------------------------------------------------------------
# find_violations — combined
# ---------------------------------------------------------------------------


class TestFindViolations:
    def test_compliant_source_no_violations(self, tmp_path: Path) -> None:
        """AC scenario (e): synthetic compliant daemon => no violations."""

        path = _write(tmp_path, _compliant_source())
        violations = guard.find_violations(path)
        assert violations == []

    def test_recovery_method_removed_flagged(self, tmp_path: Path) -> None:
        """AC scenario (b): registered breaker, recovery def removed => fail."""

        source = _compliant_source().replace(
            "def _check_circuit_breaker_auto_close(self, current_cap):",
            "def _renamed_thing(self, current_cap):",
        )
        path = _write(tmp_path, source)
        violations = guard.find_violations(path)
        assert any("_check_circuit_breaker_auto_close" in v for v in violations)

    def test_alert_method_removed_flagged(self, tmp_path: Path) -> None:
        """AC scenario (c): registered breaker, alert def removed => fail."""

        source = _compliant_source().replace(
            "def _send_diagnoser_breaker_telegram_alert(self):",
            "def _renamed_alert(self):",
        )
        path = _write(tmp_path, source)
        violations = guard.find_violations(path)
        assert any("_send_diagnoser_breaker_telegram_alert" in v for v in violations)

    def test_rogue_trip_tag_flagged(self, tmp_path: Path) -> None:
        """AC scenario (d): new unregistered trip tag => fail."""

        source = _compliant_source() + (
            "\ndef _check_rogue_breaker(self):\n"
            "    cur.execute(\"SET updated_by = 'rogue_breaker' WHERE ...\")\n"
        )
        path = _write(tmp_path, source)
        violations = guard.find_violations(path)
        assert any("rogue_breaker" in v for v in violations)

    def test_rogue_double_quoted_trip_tag_flagged(self, tmp_path: Path) -> None:
        """Tag extraction is quote-tolerant (double quotes too)."""

        source = _compliant_source() + (
            "\ndef _check_rogue2(self):\n"
            "    cur.execute('SET updated_by = \"rogue_two\" WHERE ...')\n"
        )
        path = _write(tmp_path, source)
        violations = guard.find_violations(path)
        assert any("rogue_two" in v for v in violations)


# ---------------------------------------------------------------------------
# CLI behaviour — end-to-end (subprocess)
# ---------------------------------------------------------------------------


class TestCli:
    def _run_cli(
        self, *args: str, daemon_path: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd: list[str] = ["python3", str(SCRIPT_PATH), *args]
        if daemon_path is not None:
            cmd.extend(["--daemon-path", str(daemon_path)])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_cli_real_tree_exit_zero(self) -> None:
        """AC scenario (a): the live daemon.py is compliant — exit 0."""

        result = self._run_cli()
        assert result.returncode == 0, (
            f"stderr={result.stderr!r} stdout={result.stdout!r}"
        )

    def test_cli_compliant_synthetic_exit_zero(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        result = self._run_cli(daemon_path=path)
        assert result.returncode == 0, (
            f"stderr={result.stderr!r} stdout={result.stdout!r}"
        )

    def test_cli_recovery_removed_exit_one_with_fix(self, tmp_path: Path) -> None:
        source = _compliant_source().replace(
            "def _check_circuit_breaker_auto_close(self, current_cap):",
            "def _renamed_thing(self, current_cap):",
        )
        path = _write(tmp_path, source)
        result = self._run_cli(daemon_path=path)
        assert result.returncode == 1
        assert "_check_circuit_breaker_auto_close" in result.stderr
        assert "Fix:" in result.stderr
        assert "#4593" in result.stderr

    def test_cli_rogue_tag_exit_one(self, tmp_path: Path) -> None:
        source = _compliant_source() + (
            "\ndef _check_rogue_breaker(self):\n"
            "    cur.execute(\"SET updated_by = 'rogue_breaker' WHERE ...\")\n"
        )
        path = _write(tmp_path, source)
        result = self._run_cli(daemon_path=path)
        assert result.returncode == 1
        assert "rogue_breaker" in result.stderr
        assert "_BREAKER_REGISTRY" in result.stderr

    def test_cli_list_mode_exit_zero(self) -> None:
        """AC scenario (f): --list exits 0 and prints both registry tags."""

        result = self._run_cli("--list")
        assert result.returncode == 0
        assert "circuit_breaker" in result.stdout
        assert "diagnoser_circuit_breaker" in result.stdout

    def test_cli_missing_daemon_exit_two(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.py"
        result = self._run_cli(daemon_path=missing)
        assert result.returncode == 2
        assert "daemon.py not found" in result.stderr


# ---------------------------------------------------------------------------
# .sh wrapper subprocess end-to-end on the real tree
# ---------------------------------------------------------------------------


class TestWrapper:
    def test_wrapper_real_tree_exit_zero(self) -> None:
        result = subprocess.run(
            [str(WRAPPER_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"stderr={result.stderr!r} stdout={result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# main() in-process — exercises the CLI branches for coverage
# ---------------------------------------------------------------------------


class TestMainInProcess:
    def test_main_default_real_tree_exit_zero(self) -> None:
        """No args => default daemon path => compliant => 0."""

        assert guard.main([]) == 0

    def test_main_list_mode_exit_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = guard.main(["--list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Declared breakers: 2" in out
        assert "circuit_breaker" in out
        assert "diagnoser_circuit_breaker" in out
        assert "Discovered trip tags:" in out

    def test_main_list_mode_synthetic(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _compliant_source())
        rc = guard.main(["--daemon-path", str(path), "--list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "circuit_breaker" in out

    def test_main_compliant_synthetic_exit_zero(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _compliant_source())
        assert guard.main(["--daemon-path", str(path)]) == 0

    def test_main_missing_daemon_exit_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "does-not-exist.py"
        rc = guard.main(["--daemon-path", str(missing)])
        assert rc == 2
        assert "daemon.py not found" in capsys.readouterr().err

    def test_main_violation_exit_one_with_fix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = _compliant_source().replace(
            "def _send_diagnoser_breaker_telegram_alert(self):",
            "def _renamed_alert(self):",
        )
        path = _write(tmp_path, source)
        rc = guard.main(["--daemon-path", str(path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "FAIL:" in err
        assert "_send_diagnoser_breaker_telegram_alert" in err
        assert "Fix:" in err
        assert "#4593" in err

    def test_main_rogue_tag_exit_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = _compliant_source() + (
            "\ndef _check_rogue_breaker(self):\n"
            "    cur.execute(\"SET updated_by = 'rogue_breaker' WHERE ...\")\n"
        )
        path = _write(tmp_path, source)
        rc = guard.main(["--daemon-path", str(path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "rogue_breaker" in err
        assert "_BREAKER_REGISTRY" in err


# ---------------------------------------------------------------------------
# Registry sanity — the declared registry matches today's daemon
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registry_has_two_entries(self) -> None:
        assert len(guard._BREAKER_REGISTRY) == 2

    def test_registry_tags(self) -> None:
        tags = {entry.tag for entry in guard._BREAKER_REGISTRY}
        assert tags == {"circuit_breaker", "diagnoser_circuit_breaker"}

    def test_real_daemon_passes(self) -> None:
        """Regression gate: the live daemon.py has no violations."""

        violations = guard.find_violations(REAL_DAEMON_PATH)
        assert violations == [], (
            "Live scripts/dispatcher/daemon.py has breaker recovery/alert "
            f"violations: {violations}"
        )
