"""Tests for the _venv_helper module."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add the scripts directory to the path so we can import the helper module.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _venv_helper import _find_repo_root, _venv_python, ensure_venv


class TestFindRepoRoot:
    """Tests for _find_repo_root()."""

    def test_returns_parent_of_scripts_dir(self) -> None:
        root = _find_repo_root()
        # scripts/ should be a child of the repo root
        assert (root / "scripts").is_dir()

    def test_returns_path_object(self) -> None:
        root = _find_repo_root()
        assert isinstance(root, Path)


class TestVenvPython:
    """Tests for _venv_python()."""

    def test_returns_correct_path_for_scraper_framework(self) -> None:
        result = _venv_python("scraper-framework")
        root = _find_repo_root()
        expected = root / "packages" / "scraper-framework" / ".venv" / "bin" / "python3"
        assert result == expected

    def test_returns_correct_path_for_telegram_bridge(self) -> None:
        result = _venv_python("telegram-bridge")
        root = _find_repo_root()
        expected = root / "packages" / "telegram-bridge" / ".venv" / "bin" / "python3"
        assert result == expected


class TestEnsureVenv:
    """Tests for ensure_venv()."""

    def test_skips_when_env_var_set(self) -> None:
        """ensure_venv should be a no-op when _VENV_HELPER_SKIP is set."""
        with patch.dict(os.environ, {"_VENV_HELPER_SKIP": "1"}):
            # Should return without error even for a nonexistent package
            ensure_venv("nonexistent-package")

    def test_returns_when_already_in_correct_venv(self, tmp_path: Path) -> None:
        """ensure_venv should return if sys.prefix matches the venv directory."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)
            # Create a fake venv structure
            fake_venv = tmp_path / "packages" / "test-pkg" / ".venv" / "bin"
            fake_venv.mkdir(parents=True)
            fake_python = fake_venv / "python3"
            fake_python.symlink_to(sys.executable)

            venv_dir = str(tmp_path / "packages" / "test-pkg" / ".venv")

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                # Simulate being inside the venv: sys.prefix points to the
                # venv root, and differs from sys.base_prefix.
                with patch("sys.prefix", venv_dir):
                    with patch("sys.base_prefix", "/usr/lib/python3"):
                        with patch("_venv_helper._check_venv_has_deps", return_value=True):
                            # Should return without re-exec
                            ensure_venv("test-pkg")

    def test_not_fooled_by_symlinked_venv_python(self, tmp_path: Path) -> None:
        """System python must not be mistaken for venv python even when
        the venv bin/python3 is a symlink to the same binary (the bug
        that realpath-based detection had)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)
            os.environ.pop("_VENV_HELPER_USE_EXECV", None)

            # Create a fake venv whose python3 symlinks to sys.executable
            fake_venv = tmp_path / "packages" / "test-pkg" / ".venv" / "bin"
            fake_venv.mkdir(parents=True)
            fake_python = fake_venv / "python3"
            fake_python.symlink_to(sys.executable)

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with patch("_venv_helper._check_venv_has_deps", return_value=True):
                    # sys.prefix == sys.base_prefix means we are NOT in a venv,
                    # so ensure_venv must re-exec rather than return.
                    mock_result = type("Result", (), {"returncode": 0})()
                    with patch("subprocess.run", return_value=mock_result) as mock_run:
                        with pytest.raises(SystemExit) as exc_info:
                            ensure_venv("test-pkg")
                        assert exc_info.value.code == 0
                        mock_run.assert_called_once()

    def test_exits_with_error_when_venv_missing(self, tmp_path: Path) -> None:
        """ensure_venv should sys.exit(1) with helpful message when no venv."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with pytest.raises(SystemExit) as exc_info:
                    ensure_venv("nonexistent-package")
                assert exc_info.value.code == 1

    def test_error_message_includes_setup_instructions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Error message should tell the user how to create the venv."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with pytest.raises(SystemExit):
                    ensure_venv("scraper-framework")

            captured = capsys.readouterr()
            assert "scraper-framework" in captured.err
            assert "python3.12 -m venv" in captured.err
            assert "pip install" in captured.err

    def test_uses_subprocess_run_by_default(self, tmp_path: Path) -> None:
        """ensure_venv should use subprocess.run by default (not os.execv)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)
            os.environ.pop("_VENV_HELPER_USE_EXECV", None)

            # Create a fake venv Python
            fake_venv = tmp_path / "packages" / "test-pkg" / ".venv" / "bin"
            fake_venv.mkdir(parents=True)
            fake_python = fake_venv / "python3"
            fake_python.write_text("#!/bin/sh\n")

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with patch("_venv_helper._check_venv_has_deps", return_value=True):
                    mock_result = type("Result", (), {"returncode": 0})()
                    with patch("subprocess.run", return_value=mock_result) as mock_run:
                        with pytest.raises(SystemExit) as exc_info:
                            ensure_venv("test-pkg")
                        assert exc_info.value.code == 0
                        mock_run.assert_called_once()
                        call_args = mock_run.call_args[0][0]
                        assert call_args[0] == str(fake_python)

    def test_propagates_nonzero_exit_code(self, tmp_path: Path) -> None:
        """ensure_venv should propagate non-zero exit codes from the subprocess."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)
            os.environ.pop("_VENV_HELPER_USE_EXECV", None)

            fake_venv = tmp_path / "packages" / "test-pkg" / ".venv" / "bin"
            fake_venv.mkdir(parents=True)
            fake_python = fake_venv / "python3"
            fake_python.write_text("#!/bin/sh\n")

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with patch("_venv_helper._check_venv_has_deps", return_value=True):
                    mock_result = type("Result", (), {"returncode": 42})()
                    with patch("subprocess.run", return_value=mock_result):
                        with pytest.raises(SystemExit) as exc_info:
                            ensure_venv("test-pkg")
                        assert exc_info.value.code == 42

    def test_uses_execv_when_env_var_set(self, tmp_path: Path) -> None:
        """ensure_venv should use os.execv when _VENV_HELPER_USE_EXECV is set."""
        with patch.dict(os.environ, {"_VENV_HELPER_USE_EXECV": "1"}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)

            fake_venv = tmp_path / "packages" / "test-pkg" / ".venv" / "bin"
            fake_venv.mkdir(parents=True)
            fake_python = fake_venv / "python3"
            fake_python.write_text("#!/bin/sh\n")

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with patch("_venv_helper._check_venv_has_deps", return_value=True):
                    with patch("os.execv") as mock_execv:
                        ensure_venv("test-pkg")
                        mock_execv.assert_called_once()
                        call_args = mock_execv.call_args[0]
                        assert call_args[0] == str(fake_python)

    def test_subprocess_receives_sys_argv(self, tmp_path: Path) -> None:
        """ensure_venv should pass sys.argv to the subprocess."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_VENV_HELPER_SKIP", None)
            os.environ.pop("_VENV_HELPER_USE_EXECV", None)

            fake_venv = tmp_path / "packages" / "test-pkg" / ".venv" / "bin"
            fake_venv.mkdir(parents=True)
            fake_python = fake_venv / "python3"
            fake_python.write_text("#!/bin/sh\n")

            test_argv = ["/some/script.py", "--flag", "value"]

            with patch("_venv_helper._REPO_ROOT", tmp_path):
                with patch("_venv_helper._check_venv_has_deps", return_value=True):
                    with patch("sys.argv", test_argv):
                        mock_result = type("Result", (), {"returncode": 0})()
                        with patch(
                            "subprocess.run", return_value=mock_result
                        ) as mock_run:
                            with pytest.raises(SystemExit):
                                ensure_venv("test-pkg")
                            expected_cmd = [
                                str(fake_python),
                                "/some/script.py",
                                "--flag",
                                "value",
                            ]
                            mock_run.assert_called_once_with(expected_cmd)
