"""Regression tests: the dispatcher suite cannot reach real GitHub (#5071).

In #5071 a ``_housekeeping_tick()`` test ran the real
``scripts/sweep-completed-parents.sh`` with the developer's ``gh`` login; it
closed an open issue and posted comments. The conftest gh-isolate block now
empties ``GH_CONFIG_DIR``, removes the GitHub token variables and puts a
``gh`` shim first on PATH. These tests run under that environment, so they
pass only while the guard is in place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above

_TOKEN_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


def _run_gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=30, check=False
    )


def test_gh_auth_status_fails_under_suite_env() -> None:
    """``gh auth status`` must fail even when gh is logged in on this machine."""
    result = _run_gh("auth", "status")
    assert result.returncode != 0
    assert "real gh called from a test (#5071)" in result.stderr


def test_gh_issue_close_with_fake_token_hits_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token in the env does not help: ``gh issue close`` hits the shim.

    The resolved ``gh`` is the shim script, not a gh binary, so no network
    call can happen; the shim logs the call and exits 4.
    """
    for name in _TOKEN_VARS:
        monkeypatch.setenv(name, "ghp_fake_token_5071")
    gh_path = shutil.which("gh")
    assert gh_path is not None
    assert "gh shim from the gh-isolate test fixture" in Path(gh_path).read_text()
    log = Path(os.environ["GH_ISOLATE_LOG"])
    before = log.read_text().splitlines() if log.exists() else []

    result = _run_gh(
        "issue", "close", "4673", "--repo", "judgemind/judgemind", "--comment", "x"
    )

    assert result.returncode == 4
    assert "FAIL: real gh called from a test (#5071): gh issue close 4673" in (
        result.stderr
    )
    after = log.read_text().splitlines()
    new = [line for line in after if line not in before]
    assert any(
        "test_gh_issue_close_with_fake_token_hits_shim" in line
        and "gh issue close 4673" in line
        for line in new
    ), new


def test_session_env_has_no_github_login() -> None:
    """Tokens are gone and GH_CONFIG_DIR holds no stored login."""
    for name in _TOKEN_VARS:
        assert name not in os.environ, name
    config_dir = Path(os.environ["GH_CONFIG_DIR"])
    assert config_dir.is_dir()
    assert not (config_dir / "hosts.yml").exists()


def test_test_provided_gh_stub_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that installs its own fake gh still gets it, not the shim."""
    fake = tmp_path / "gh"
    fake.write_text('#!/bin/sh\necho "fake gh: $*"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    result = _run_gh("issue", "view", "1")
    assert result.returncode == 0
    assert result.stdout.strip() == "fake gh: issue view 1"


@pytest.mark.parametrize(
    "healer",
    ["_housekeeping_sweep_completed_parents", "_housekeeping_close_orphan_prs"],
)
def test_gh_healers_are_stubbed_for_tick_tests(healer: str) -> None:
    """The conftest replaces the gh-calling housekeeping healers (#5071).

    ``_housekeeping_tick()`` tests that do not stub them must never spawn
    sweep-completed-parents.sh or ``gh pr close``.
    """
    d = object.__new__(daemon.DispatcherDaemon)
    result = getattr(d, healer)()
    assert isinstance(result, dict)
    assert getattr(daemon.DispatcherDaemon, healer).__module__ != daemon.__name__


@pytest.mark.real_gh_healers
def test_real_gh_healers_marker_keeps_real_methods() -> None:
    """``@pytest.mark.real_gh_healers`` opts a test out of the stubs."""
    for healer in (
        "_housekeeping_sweep_completed_parents",
        "_housekeeping_close_orphan_prs",
    ):
        method = getattr(daemon.DispatcherDaemon, healer)
        assert method.__module__ == daemon.__name__, healer
