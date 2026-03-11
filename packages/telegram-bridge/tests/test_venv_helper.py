"""Tests for the _venv_helper module (scripts/_venv_helper.py).

Validates that ensure_venv correctly detects missing venvs and empty venvs
(venvs that exist but have no dependencies installed), producing clear error
messages instead of raw ModuleNotFoundError crashes.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts/ is on sys.path so we can import _venv_helper.
REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = str(REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Force-reload _venv_helper to pick up the scripts/ path.
# We must also clear _VENV_HELPER_SKIP temporarily so we can test
# the real logic.
_orig_skip = os.environ.pop("_VENV_HELPER_SKIP", None)
import _venv_helper  # noqa: E402

importlib.reload(_venv_helper)
if _orig_skip is not None:
    os.environ["_VENV_HELPER_SKIP"] = _orig_skip


class TestCheckVenvHasDeps:
    """Tests for _check_venv_has_deps."""

    def test_returns_true_for_venv_with_packages(self, tmp_path: Path) -> None:
        """A venv with non-bootstrap .dist-info dirs should return True."""
        # Create a fake venv structure.
        pkg_dir = tmp_path / "packages" / "fake-pkg"
        venv_dir = pkg_dir / ".venv"
        site_packages = venv_dir / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        # Add bootstrap packages (should be ignored).
        (site_packages / "pip-24.0.dist-info").mkdir()
        (site_packages / "setuptools-70.0.dist-info").mkdir()

        # Add a real package.
        (site_packages / "httpx-0.27.0.dist-info").mkdir()

        with patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path):
            assert _venv_helper._check_venv_has_deps("fake-pkg") is True

    def test_returns_false_for_empty_venv(self, tmp_path: Path) -> None:
        """A venv with only bootstrap packages should return False."""
        pkg_dir = tmp_path / "packages" / "fake-pkg"
        venv_dir = pkg_dir / ".venv"
        site_packages = venv_dir / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        # Only bootstrap packages.
        (site_packages / "pip-24.0.dist-info").mkdir()
        (site_packages / "setuptools-70.0.dist-info").mkdir()

        with patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path):
            assert _venv_helper._check_venv_has_deps("fake-pkg") is False

    def test_returns_false_for_missing_lib_dir(self, tmp_path: Path) -> None:
        """A venv without a lib/ dir should return False."""
        pkg_dir = tmp_path / "packages" / "fake-pkg"
        venv_dir = pkg_dir / ".venv"
        (venv_dir / "bin").mkdir(parents=True)

        with patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path):
            assert _venv_helper._check_venv_has_deps("fake-pkg") is False

    def test_returns_false_for_completely_empty_site_packages(self, tmp_path: Path) -> None:
        """A venv with an empty site-packages should return False."""
        pkg_dir = tmp_path / "packages" / "fake-pkg"
        venv_dir = pkg_dir / ".venv"
        site_packages = venv_dir / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        with patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path):
            assert _venv_helper._check_venv_has_deps("fake-pkg") is False


class TestEnsureVenvWithEmptyVenv:
    """Tests that ensure_venv exits with a clear error for empty venvs."""

    def test_exits_with_clear_error_for_empty_venv(self, tmp_path: Path) -> None:
        """ensure_venv should exit(1) with install instructions when the venv
        exists but has no deps installed."""
        # Create a venv that exists but has no deps.
        pkg_dir = tmp_path / "packages" / "fake-pkg"
        venv_dir = pkg_dir / ".venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        venv_py = venv_bin / "python3"
        venv_py.write_text("#!/usr/bin/env python3\n")
        venv_py.chmod(0o755)

        site_packages = venv_dir / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)
        # Only bootstrap — no real deps.
        (site_packages / "pip-24.0.dist-info").mkdir()

        # Temporarily clear the skip env var.
        orig_skip = os.environ.pop("_VENV_HELPER_SKIP", None)

        try:
            with (
                patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path),
                patch("sys.executable", str(venv_py)),
                patch("os.path.realpath", side_effect=lambda p: str(p)),
                pytest.raises(SystemExit) as exc_info,
            ):
                _venv_helper.ensure_venv("fake-pkg")
            assert exc_info.value.code == 1
        finally:
            if orig_skip is not None:
                os.environ["_VENV_HELPER_SKIP"] = orig_skip

    def test_exits_with_clear_error_for_missing_venv(self, tmp_path: Path) -> None:
        """ensure_venv should exit(1) with create instructions when the venv
        does not exist at all."""
        orig_skip = os.environ.pop("_VENV_HELPER_SKIP", None)

        try:
            with (
                patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path),
                pytest.raises(SystemExit) as exc_info,
            ):
                _venv_helper.ensure_venv("nonexistent-pkg")
            assert exc_info.value.code == 1
        finally:
            if orig_skip is not None:
                os.environ["_VENV_HELPER_SKIP"] = orig_skip

    def test_skip_env_var_bypasses_check(self, tmp_path: Path) -> None:
        """When _VENV_HELPER_SKIP is set, ensure_venv should return immediately
        without checking anything."""
        os.environ["_VENV_HELPER_SKIP"] = "1"
        try:
            # Should not raise even for a nonexistent package.
            _venv_helper.ensure_venv("totally-fake-pkg")
        finally:
            # Restore for other tests.
            os.environ["_VENV_HELPER_SKIP"] = "1"

    def test_empty_venv_error_message_contains_install_instructions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The error message for an empty venv should tell the user how to
        install dependencies."""
        pkg_dir = tmp_path / "packages" / "test-pkg"
        venv_dir = pkg_dir / ".venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        venv_py = venv_bin / "python3"
        venv_py.write_text("#!/usr/bin/env python3\n")
        venv_py.chmod(0o755)

        site_packages = venv_dir / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)
        (site_packages / "pip-24.0.dist-info").mkdir()

        orig_skip = os.environ.pop("_VENV_HELPER_SKIP", None)

        try:
            with (
                patch.object(_venv_helper, "_find_repo_root", return_value=tmp_path),
                patch("sys.executable", str(venv_py)),
                patch("os.path.realpath", side_effect=lambda p: str(p)),
                pytest.raises(SystemExit),
            ):
                _venv_helper.ensure_venv("test-pkg")

            captured = capsys.readouterr()
            assert "pip install" in captured.err
            assert "test-pkg" in captured.err
            assert "no dependencies installed" in captured.err.lower()
        finally:
            if orig_skip is not None:
                os.environ["_VENV_HELPER_SKIP"] = orig_skip
