"""Tests for scripts/check-no-unbounded-timeouts.py.

Covers one positive (clean) and one negative (offending) fixture per
rule, plus the allowlist behaviour. Tracking: issue #3356.
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
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check-no-unbounded-timeouts.py"
_spec = importlib.util.spec_from_file_location(
    "check_no_unbounded_timeouts", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
check_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mod)


# ---------------------------------------------------------------------------
# Fixture helper: write a Python source string to a temp file, then run
# scan_file on it and return the violation list.
# ---------------------------------------------------------------------------


def _scan(tmp_path: Path, source: str, name: str = "fixture.py") -> list[str]:
    path = tmp_path / name
    path.write_text(source)
    return check_mod.scan_file(path)


# ---------------------------------------------------------------------------
# Rule 1: boto3.client(...)
# ---------------------------------------------------------------------------


class TestBoto3Client:
    def test_inline_config_literal_passes(self, tmp_path: Path) -> None:
        source = (
            "import boto3\n"
            "import botocore.config\n"
            "boto3.client(\n"
            "    'ecs',\n"
            "    config=botocore.config.Config(\n"
            "        connect_timeout=5, read_timeout=10,\n"
            "        retries={'max_attempts': 2},\n"
            "    ),\n"
            ")\n"
        )
        assert _scan(tmp_path, source) == []

    def test_named_config_local_variable_passes(self, tmp_path: Path) -> None:
        """Daemon's preferred shape: build Config(...) into a local then
        pass the local. The check resolves the Name back to its
        assignment in the same function."""
        source = (
            "import boto3\n"
            "import botocore.config\n"
            "def make_client():\n"
            "    cfg = botocore.config.Config(connect_timeout=5, read_timeout=10)\n"
            "    return boto3.client('ecs', config=cfg)\n"
        )
        assert _scan(tmp_path, source) == []

    def test_no_config_kwarg_fails(self, tmp_path: Path) -> None:
        source = "import boto3\nboto3.client('ecs', region_name='us-west-2')\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "boto3.client" in violations[0]

    def test_config_missing_read_timeout_fails(self, tmp_path: Path) -> None:
        source = (
            "import boto3\n"
            "import botocore.config\n"
            "boto3.client('ecs', config=botocore.config.Config(connect_timeout=5))\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "read_timeout" in violations[0]

    def test_config_missing_connect_timeout_fails(self, tmp_path: Path) -> None:
        source = (
            "import boto3\n"
            "import botocore.config\n"
            "boto3.client('ecs', config=botocore.config.Config(read_timeout=10))\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "connect_timeout" in violations[0]


# ---------------------------------------------------------------------------
# Rule 2: psycopg.connect(...)
# ---------------------------------------------------------------------------


class TestPsycopgConnect:
    def test_options_with_statement_timeout_passes(self, tmp_path: Path) -> None:
        source = (
            "import psycopg\n"
            "psycopg.connect(\n"
            "    'postgres://x',\n"
            "    connect_timeout=10,\n"
            "    options='-c statement_timeout=30000',\n"
            ")\n"
        )
        assert _scan(tmp_path, source) == []

    def test_named_options_constant_passes(self, tmp_path: Path) -> None:
        """A Name reference for ``options=`` is trusted: the daemon uses
        ``options=PSYCOPG_STATEMENT_TIMEOUT_OPTIONS``."""
        source = (
            "import psycopg\n"
            "PSYCOPG_STATEMENT_TIMEOUT_OPTIONS = '-c statement_timeout=30000'\n"
            "psycopg.connect(\n"
            "    'postgres://x',\n"
            "    connect_timeout=10,\n"
            "    options=PSYCOPG_STATEMENT_TIMEOUT_OPTIONS,\n"
            ")\n"
        )
        assert _scan(tmp_path, source) == []

    def test_missing_options_fails(self, tmp_path: Path) -> None:
        source = "import psycopg\npsycopg.connect('postgres://x', connect_timeout=10)\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "statement_timeout" in violations[0]
        assert "psycopg.connect" in violations[0]

    def test_options_without_statement_timeout_fails(self, tmp_path: Path) -> None:
        source = (
            "import psycopg\n"
            "psycopg.connect('postgres://x', connect_timeout=10, options='-c x=y')\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "statement_timeout" in violations[0]

    def test_missing_connect_timeout_fails(self, tmp_path: Path) -> None:
        source = (
            "import psycopg\n"
            "psycopg.connect(\n"
            "    'postgres://x',\n"
            "    options='-c statement_timeout=30000',\n"
            ")\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "connect_timeout" in violations[0]


# ---------------------------------------------------------------------------
# Rule 3: subprocess.run / check_output / check_call
# ---------------------------------------------------------------------------


class TestSubprocessSimple:
    @pytest.mark.parametrize("call", ["run", "check_output", "check_call"])
    def test_with_timeout_passes(self, tmp_path: Path, call: str) -> None:
        source = f"import subprocess\nsubprocess.{call}(['ls'], timeout=30)\n"
        assert _scan(tmp_path, source) == []

    @pytest.mark.parametrize("call", ["run", "check_output", "check_call"])
    def test_without_timeout_fails(self, tmp_path: Path, call: str) -> None:
        source = f"import subprocess\nsubprocess.{call}(['ls'])\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert f"subprocess.{call}" in violations[0]

    def test_kwargs_splat_passes(self, tmp_path: Path) -> None:
        source = (
            "import subprocess\n"
            "def run_helper(cmd, **kw):\n"
            "    return subprocess.run(cmd, **kw)\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# Rule 4: subprocess.Popen with paired wait(timeout=) + TimeoutExpired -> kill()
# ---------------------------------------------------------------------------


class TestSubprocessPopen:
    def test_popen_with_paired_wait_and_kill_passes(self, tmp_path: Path) -> None:
        """The two daemon Popen sites both follow this canonical shape."""
        source = (
            "import subprocess\n"
            "def spawn():\n"
            "    proc = subprocess.Popen(['x'], stdout=subprocess.PIPE)\n"
            "    try:\n"
            "        proc.wait(timeout=60)\n"
            "    except subprocess.TimeoutExpired:\n"
            "        proc.kill()\n"
            "        proc.wait(timeout=10)\n"
        )
        assert _scan(tmp_path, source) == []

    def test_popen_without_pairing_fails(self, tmp_path: Path) -> None:
        source = (
            "import subprocess\n"
            "def spawn():\n"
            "    proc = subprocess.Popen(['x'])\n"
            "    proc.wait()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "subprocess.Popen" in violations[0]

    def test_popen_with_wait_but_no_kill_fails(self, tmp_path: Path) -> None:
        """Catching TimeoutExpired without ``proc.kill()`` would leak the
        zombie child — must fail."""
        source = (
            "import subprocess\n"
            "def spawn():\n"
            "    proc = subprocess.Popen(['x'])\n"
            "    try:\n"
            "        proc.wait(timeout=10)\n"
            "    except subprocess.TimeoutExpired:\n"
            "        pass\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_popen_with_explicit_timeout_passes(self, tmp_path: Path) -> None:
        """Some Python versions surface ``timeout=`` directly on Popen
        for read-side bounding; allow that shape too."""
        source = "import subprocess\nsubprocess.Popen(['x'], timeout=30)\n"
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# Rule 5: requests.<method>(...)
# ---------------------------------------------------------------------------


class TestRequests:
    @pytest.mark.parametrize(
        "method", ["get", "post", "put", "delete", "patch", "head", "request"]
    )
    def test_with_timeout_passes(self, tmp_path: Path, method: str) -> None:
        source = (
            f"import requests\nrequests.{method}('https://example.com', timeout=10)\n"
        )
        assert _scan(tmp_path, source) == []

    @pytest.mark.parametrize(
        "method", ["get", "post", "put", "delete", "patch", "head", "request"]
    )
    def test_without_timeout_fails(self, tmp_path: Path, method: str) -> None:
        source = f"import requests\nrequests.{method}('https://example.com')\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert f"requests.{method}" in violations[0]


# ---------------------------------------------------------------------------
# Rule 6: urllib.request.urlopen
# ---------------------------------------------------------------------------


class TestUrlopen:
    def test_dotted_with_timeout_passes(self, tmp_path: Path) -> None:
        source = (
            "import urllib.request\n"
            "urllib.request.urlopen('https://example.com', timeout=30)\n"
        )
        assert _scan(tmp_path, source) == []

    def test_dotted_without_timeout_fails(self, tmp_path: Path) -> None:
        source = (
            "import urllib.request\nurllib.request.urlopen('https://example.com')\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "urlopen" in violations[0]

    def test_from_import_with_timeout_passes(self, tmp_path: Path) -> None:
        source = (
            "from urllib.request import urlopen\n"
            "urlopen('https://example.com', timeout=30)\n"
        )
        assert _scan(tmp_path, source) == []

    def test_from_import_without_timeout_fails(self, tmp_path: Path) -> None:
        source = "from urllib.request import urlopen\nurlopen('https://example.com')\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Rule 7: socket.create_connection
# ---------------------------------------------------------------------------


class TestSocketCreateConnection:
    def test_with_timeout_passes(self, tmp_path: Path) -> None:
        source = "import socket\nsocket.create_connection(('host', 443), timeout=10)\n"
        assert _scan(tmp_path, source) == []

    def test_without_timeout_fails(self, tmp_path: Path) -> None:
        source = "import socket\nsocket.create_connection(('host', 443))\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "socket.create_connection" in violations[0]


# ---------------------------------------------------------------------------
# Rule 8: http.client.HTTP{S,}Connection
# ---------------------------------------------------------------------------


class TestHttpClientConnection:
    @pytest.mark.parametrize("cls", ["HTTPConnection", "HTTPSConnection"])
    def test_dotted_with_timeout_passes(self, tmp_path: Path, cls: str) -> None:
        source = f"import http.client\nhttp.client.{cls}('host', timeout=10)\n"
        assert _scan(tmp_path, source) == []

    @pytest.mark.parametrize("cls", ["HTTPConnection", "HTTPSConnection"])
    def test_dotted_without_timeout_fails(self, tmp_path: Path, cls: str) -> None:
        source = f"import http.client\nhttp.client.{cls}('host')\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert cls in violations[0]

    def test_from_import_without_timeout_fails(self, tmp_path: Path) -> None:
        source = "from http.client import HTTPConnection\nHTTPConnection('host')\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Allowlist behaviour
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_allowlist_with_reason_exempts(self, tmp_path: Path) -> None:
        source = (
            "import requests\n"
            "requests.get('https://example.com')  # noqa: timeout-required: test fixture\n"
        )
        assert _scan(tmp_path, source) == []

    def test_bare_noqa_does_not_exempt(self, tmp_path: Path) -> None:
        """A bare ``# noqa`` (no rule name, no reason) must NOT exempt."""
        source = "import requests\nrequests.get('https://example.com')  # noqa\n"
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_noqa_rule_only_does_not_exempt(self, tmp_path: Path) -> None:
        """``# noqa: timeout-required`` without a colon-reason must NOT
        exempt — the rule requires a non-empty justification."""
        source = (
            "import requests\n"
            "requests.get('https://example.com')  # noqa: timeout-required\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_noqa_empty_reason_does_not_exempt(self, tmp_path: Path) -> None:
        """``# noqa: timeout-required:`` (colon present, reason empty)
        must NOT exempt."""
        source = (
            "import requests\n"
            "requests.get('https://example.com')  # noqa: timeout-required: \n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_allowlist_inside_multiline_call_exempts(self, tmp_path: Path) -> None:
        """A noqa marker on any line covered by the call's span exempts."""
        source = (
            "import requests\n"
            "requests.get(\n"
            "    'https://example.com',  # noqa: timeout-required: legacy\n"
            ")\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# End-to-end: the real scripts/dispatcher tree must scan clean.
# ---------------------------------------------------------------------------


class TestRealDispatcherTree:
    def test_dispatcher_tree_scans_clean(self) -> None:
        """The post-fix dispatcher must scan clean — this is the primary
        regression gate."""
        rc = check_mod.main([])
        assert rc == 0, "scripts/dispatcher/ has unbounded IO call(s)"


# ---------------------------------------------------------------------------
# tests/ directory exclusion
# ---------------------------------------------------------------------------


class TestPathExclusion:
    def test_tests_directory_skipped(self, tmp_path: Path) -> None:
        """A file under a ``tests/`` segment must be skipped even if it
        contains an unbounded call."""
        bad_file = tmp_path / "tests" / "fixture.py"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text("import requests\nrequests.get('https://example.com')\n")
        rc = check_mod.main([str(tmp_path)])
        assert rc == 0

    def test_non_tests_path_scanned(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "fixture.py"
        bad_file.write_text("import requests\nrequests.get('https://example.com')\n")
        rc = check_mod.main([str(tmp_path)])
        assert rc == 1
