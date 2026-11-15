"""Regression tests: a test that reaches the gh shim fails the run (#5075).

The gh-isolate block (#5071) puts a ``gh`` shim first on PATH in every
pytest session. Before #5075 a test that fell through to the shim just got
exit 4 and still passed, so it exercised a "gh failed" branch instead of the
behaviour it claimed to test. These tests run a throwaway pytest session
whose conftest.py is the canonical gh-isolate block from
``scripts/tests/conftest.py`` and assert that a shim hit fails it, naming
the test and the gh command, while a test-provided fake gh and the
``gh_shim_allowed`` opt-out keep a test green.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_CANON = Path(__file__).resolve().parent / "conftest.py"
_BEGIN = "# --- BEGIN gh-isolate (#5071) ---"
_END = "# --- END gh-isolate (#5071) ---"


def _canonical_block() -> str:
    text = _CANON.read_text()
    start = text.index(_BEGIN)
    end = text.index(_END) + len(_END)
    return text[start:end]


def _run_session(tmp_path: Path, test_source: str) -> subprocess.CompletedProcess[str]:
    """Run ``pytest`` on ``test_source`` under the canonical gh-isolate block."""
    (tmp_path / "conftest.py").write_text(
        "from __future__ import annotations\n\nimport os\n\nimport pytest\n\n"
        + _canonical_block()
        + "\n"
    )
    (tmp_path / "test_inner.py").write_text(test_source)
    env = dict(os.environ)
    # Keep the inner session's deliberate hits out of an outer measurement log.
    env.pop("GH_ISOLATE_LOG", None)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--rootdir",
            str(tmp_path),
            "-c",
            os.devnull,
            str(tmp_path),
            "-q",
            "-rA",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_GH_CALL = """
import subprocess


def _gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
"""


def test_shim_hit_fails_the_test_and_names_it(tmp_path: Path) -> None:
    result = _run_session(
        tmp_path,
        _GH_CALL
        + """

def test_calls_real_gh():
    # Swallows the failure, as the daemon's best-effort gh helpers do.
    assert _gh("pr", "view", "9", "--repo", "judgemind/judgemind").returncode == 4


def test_no_gh():
    assert True
""",
    )
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "real gh called from a test (#5075)" in out
    assert "test_inner.py::test_calls_real_gh" in out
    assert "gh pr view 9 --repo judgemind/judgemind" in out
    assert "Fix: stub gh in this test" in out
    # The hit is reported as an error at the test's teardown; the test that
    # made no gh call is unaffected.
    assert "ERROR test_inner.py::test_calls_real_gh" in out, out
    assert "ERROR test_inner.py::test_no_gh" not in out, out
    assert "1 error" in out, out


def test_fake_gh_and_opt_out_stay_green(tmp_path: Path) -> None:
    result = _run_session(
        tmp_path,
        _GH_CALL
        + """
import os


def test_own_fake_gh_wins(tmp_path, monkeypatch):
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\\necho fake\\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert _gh("issue", "view", "1").stdout.strip() == "fake"


def test_shim_on_purpose(gh_shim_allowed):
    assert _gh("auth", "status").returncode == 4


def test_after_opt_out_is_clean():
    assert True
""",
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "3 passed" in out, out
    assert "real gh called from a test (#5075)" not in out


def test_hit_outside_any_test_fails_the_session(tmp_path: Path) -> None:
    result = _run_session(
        tmp_path,
        _GH_CALL
        + """
import pytest


@pytest.fixture(scope="session")
def calls_gh_at_teardown():
    yield
    _gh("issue", "list", "--label", "agent/ready")


def test_uses_session_fixture(calls_gh_at_teardown):
    assert True
""",
    )
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "real gh called from a test (#5075)" in out
    assert "outside any test" in out
    assert "gh issue list --label agent/ready" in out
