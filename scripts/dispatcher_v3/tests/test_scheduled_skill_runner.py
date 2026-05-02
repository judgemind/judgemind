"""Unit tests for ``dispatcher_v3.scheduled_skill_runner``.

Pins the contract that SKILL_NAME is validated against RECOGNIZED_SKILLS,
the correct argv is forwarded to ``subprocess.Popen``, and bad/missing env
values cause a non-zero exit with a clear stderr message.

``subprocess.Popen`` is mocked so the suite runs without a real ``claude``
binary.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from dispatcher_v3 import scheduled_skill_runner
from dispatcher_v3.scheduled_skill_runner import RECOGNIZED_SKILLS


# ── importability ─────────────────────────────────────────────────────────


def test_module_importable() -> None:
    """The module is importable; public surface includes main and RECOGNIZED_SKILLS."""
    assert callable(scheduled_skill_runner.main)
    assert isinstance(RECOGNIZED_SKILLS, frozenset)
    assert len(RECOGNIZED_SKILLS) > 0


# ── argv construction ─────────────────────────────────────────────────────


def _fake_popen(returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.wait.return_value = returncode
    return proc


def test_audit_skill_invokes_canonical_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKILL_NAME=audit produces argv ``["claude", "-p", "/audit"]``."""
    monkeypatch.setenv("SKILL_NAME", "audit")

    fake_proc = _fake_popen()
    with patch("dispatcher_v3.scheduled_skill_runner.subprocess") as mock_sub:
        mock_sub.Popen.return_value = fake_proc
        rc = scheduled_skill_runner.main()

    assert rc == 0
    mock_sub.Popen.assert_called_once_with(["claude", "-p", "/audit"])


@pytest.mark.parametrize("skill", sorted(RECOGNIZED_SKILLS))
def test_all_recognized_skills_invoke_canonical_argv(
    skill: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every recognized skill builds ``["claude", "-p", f"/{skill}"]``."""
    monkeypatch.setenv("SKILL_NAME", skill)

    fake_proc = _fake_popen()
    with patch("dispatcher_v3.scheduled_skill_runner.subprocess") as mock_sub:
        mock_sub.Popen.return_value = fake_proc
        rc = scheduled_skill_runner.main()

    assert rc == 0
    mock_sub.Popen.assert_called_once_with(["claude", "-p", f"/{skill}"])


# ── error paths ───────────────────────────────────────────────────────────


def test_unknown_skill_name_exits_nonzero_with_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SKILL_NAME=does-not-exist returns non-zero and writes 'unknown skill' to stderr."""
    monkeypatch.setenv("SKILL_NAME", "does-not-exist")

    with patch("dispatcher_v3.scheduled_skill_runner.subprocess"):
        rc = scheduled_skill_runner.main()

    assert rc != 0
    captured = capsys.readouterr()
    assert "unknown skill" in captured.err


def test_missing_skill_name_exits_nonzero_with_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No SKILL_NAME env var returns non-zero and writes 'SKILL_NAME env var required'."""
    monkeypatch.delenv("SKILL_NAME", raising=False)

    with patch("dispatcher_v3.scheduled_skill_runner.subprocess"):
        rc = scheduled_skill_runner.main()

    assert rc != 0
    captured = capsys.readouterr()
    assert "SKILL_NAME env var required" in captured.err


# ── exit-code propagation ─────────────────────────────────────────────────


def test_propagates_subprocess_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero claude exit code propagates through main()."""
    monkeypatch.setenv("SKILL_NAME", "audit")

    fake_proc = _fake_popen(returncode=42)
    with patch("dispatcher_v3.scheduled_skill_runner.subprocess") as mock_sub:
        mock_sub.Popen.return_value = fake_proc
        rc = scheduled_skill_runner.main()

    assert rc == 42
