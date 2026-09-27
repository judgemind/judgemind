"""The suite's per-test timeout fails a stuck test instead of hanging (#4812).

Pre-push runs this suite under xdist. Before #4812 a stuck or very slow test
only had ``faulthandler_timeout``, which prints a stack but lets the test run
on, and the hook buffers each job's output until the job ends, so the dump
was never seen while the push sat there. pytest-timeout fails the test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_HANGING_TEST = """\
import threading


def test_hangs():
    threading.Event().wait(120)


def test_ok():
    assert True
"""


def test_suite_configures_a_per_test_timeout(pytestconfig: pytest.Config) -> None:
    """pyproject sets a positive ``timeout`` for every scraper-framework test."""
    assert float(pytestconfig.getini("timeout")) > 0


def test_hung_test_fails_within_timeout_under_xdist(tmp_path: Path) -> None:
    """A test that blocks past --timeout fails, under the hook's xdist flags."""
    (tmp_path / "test_hang.py").write_text(_HANGING_TEST)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
            "--dist",
            "worksteal",
            "--timeout=2",
            "-q",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=tmp_path,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "1 failed, 1 passed" in out, out
    assert "Timeout" in out, out
