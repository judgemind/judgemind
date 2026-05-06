# venv: none
"""Tests for ``scripts/audits/audit-shipped-zombies.py``.

Mocks ``gh`` and ``scripts/check-shipped-pr.sh`` via env hooks
(``AUDIT_SHIPPED_GH_BIN`` and ``AUDIT_SHIPPED_CHECK_BIN``). The script
under test is a pure-stdlib subprocess wrapper, so tests stay hermetic by
pointing each binary hook at a tiny per-test bash mock.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audits" / "audit-shipped-zombies.py"


def _import_audit_module():
    """Load the audit script as a module under a Python-safe name.

    The on-disk filename uses dashes (``audit-shipped-zombies.py``) so it
    cannot be imported via the normal ``import`` machinery — we use
    ``importlib.util.spec_from_file_location`` to load it as
    ``audit_shipped_zombies`` instead.
    """

    spec = importlib.util.spec_from_file_location("audit_shipped_zombies", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_shipped_zombies"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def audit_module():
    return _import_audit_module()


def _write_executable(path: Path, body: str) -> Path:
    """Write a bash script body and chmod +x it."""

    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_gh_mock(tmp_path: Path, issues: list[dict]) -> Path:
    """Create a minimal ``gh`` mock that returns ``issues`` for ``gh issue list``."""

    issues_json = json.dumps(issues)
    # No textwrap.dedent here: the heredoc body must be at column 0 with no
    # leading whitespace on the JSON line, so the whole script is written at
    # column 0 directly.
    body = (
        "#!/usr/bin/env bash\n"
        "# Mock gh: only handles `gh issue list ... --json number,title ...`\n"
        'case "${1:-}" in\n'
        "    issue)\n"
        '        if [[ "${2:-}" == "list" ]]; then\n'
        "            cat <<'JSON'\n"
        f"{issues_json}\n"
        "JSON\n"
        "            exit 0\n"
        "        fi\n"
        "        ;;\n"
        "esac\n"
        'echo "mock-gh: unexpected args: $@" >&2\n'
        "exit 99\n"
    )
    return _write_executable(tmp_path / "gh", body)


def _make_check_shipped_mock(
    tmp_path: Path,
    *,
    matches: dict[int, dict] | None = None,
) -> Path:
    """Create a mock for ``scripts/check-shipped-pr.sh``.

    For each issue number passed in ``matches``, the mock prints the
    documented ``shipped:`` line + JSON summary and exits 0. Any
    unconfigured issue number falls through to a ``not-shipped:`` line +
    exit 1, matching the wrapper's documented behaviour.

    Body is built without ``textwrap.dedent`` so the JSON heredoc lines
    sit at column 0 (any leading whitespace would be included in the
    here-doc body, breaking JSON).
    """

    matches = matches or {}

    case_lines: list[str] = []
    for issue_num, summary in matches.items():
        summary_json = json.dumps(summary, indent=2, sort_keys=True)
        case_lines.append(f"    {issue_num})")
        case_lines.append(
            "        echo "
            f"'shipped: PR #{summary['shipped_pr']} merged to main with "
            f"{summary.get('overlap_count', 1)} file overlap(s) for "
            f"issue #{issue_num} (exit 0)'"
        )
        case_lines.append("        cat <<'JSON'")
        case_lines.append(summary_json)
        case_lines.append("JSON")
        case_lines.append("        exit 0")
        case_lines.append("        ;;")

    body_lines: list[str] = [
        "#!/usr/bin/env bash",
        "# Mock for scripts/check-shipped-pr.sh — matches by issue number.",
        'issue_arg="${1:-}"',
        'issue_arg="${issue_arg#\\#}"',
        'case "$issue_arg" in',
        *case_lines,
        "    *)",
        '        echo "not-shipped: no candidate file paths in issue '
        '#${issue_arg} body (exit 1)"',
        "        exit 1",
        "        ;;",
        "esac",
    ]
    body = "\n".join(body_lines) + "\n"
    return _write_executable(tmp_path / "check-shipped-pr.sh", body)


# ─── Unit: _parse_shipped_summary ─────────────────────────────────────────


def test_parse_shipped_summary_extracts_json(audit_module):
    stdout = textwrap.dedent(
        """\
        shipped: PR #3229 merged to main with 1 file overlap(s) for issue #2831 (exit 0)
        {
          "issue": 2831,
          "shipped_pr": 3229,
          "overlap_count": 1,
          "overlap_files": ["scripts/foo.sh"],
          "added_files": ["scripts/foo.sh"],
          "candidate_files": ["scripts/foo.sh"]
        }
        """
    )
    parsed = audit_module._parse_shipped_summary(stdout)
    assert parsed is not None
    assert parsed["shipped_pr"] == 3229
    assert parsed["issue"] == 2831
    assert parsed["overlap_files"] == ["scripts/foo.sh"]


def test_parse_shipped_summary_returns_none_on_empty(audit_module):
    assert audit_module._parse_shipped_summary("") is None


def test_parse_shipped_summary_returns_none_when_no_brace(audit_module):
    assert audit_module._parse_shipped_summary("not-shipped: nothing\n") is None


def test_parse_shipped_summary_returns_none_on_invalid_json(audit_module):
    stdout = "shipped: ...\n{not valid json"
    assert audit_module._parse_shipped_summary(stdout) is None


# ─── Unit: render_report ───────────────────────────────────────────────────


def test_render_report_no_matches(audit_module):
    report = audit_module.render_report(
        issues_scanned=10,
        matches=[],
        repo="judgemind/judgemind",
    )
    assert "Issues scanned: **10**" in report
    assert "Zombies found: **0**" in report
    assert "No zombies found." in report
    # No table when there are no matches.
    assert "| Issue | Title |" not in report


def test_render_report_with_matches(audit_module):
    matches = [
        {
            "issue": 2831,
            "issue_title": "dx: add scripts/foo.sh",
            "shipped_pr": 3229,
            "overlap_count": 1,
            "overlap_files": ["scripts/foo.sh"],
            "added_files": ["scripts/foo.sh"],
            "candidate_files": ["scripts/foo.sh"],
        },
        {
            "issue": 9001,
            "issue_title": "dx: add packages/web/bar.ts",
            "shipped_pr": 9050,
            "overlap_count": 1,
            "overlap_files": ["packages/web/bar.ts"],
            "added_files": [],
            "candidate_files": ["packages/web/bar.ts"],
        },
    ]
    report = audit_module.render_report(
        issues_scanned=20,
        matches=matches,
        repo="judgemind/judgemind",
    )
    assert "Issues scanned: **20**" in report
    assert "Zombies found: **2**" in report
    assert "| #2831 | dx: add scripts/foo.sh | #3229 |" in report
    assert "`scripts/foo.sh`" in report
    assert "| #9001 | dx: add packages/web/bar.ts | #9050 |" in report
    assert "/task #N" in report  # next-steps section


# ─── Integration: run_audit with mocks ─────────────────────────────────────


def test_run_audit_no_zombies_in_queue(tmp_path, audit_module):
    issues = [
        {"number": 100, "title": "feat: foo"},
        {"number": 101, "title": "feat: bar"},
    ]
    gh_bin = _make_gh_mock(tmp_path, issues)
    check_bin = _make_check_shipped_mock(tmp_path, matches=None)

    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(gh_bin),
        check_bin=str(check_bin),
    )
    assert exit_code == 0
    assert "Issues scanned: **2**" in report
    assert "Zombies found: **0**" in report
    assert "No zombies found." in report


def test_run_audit_finds_zombie(tmp_path, audit_module):
    issues = [
        {"number": 2831, "title": "dx: add scripts/foo.sh"},
        {"number": 100, "title": "feat: clean"},
    ]
    matches = {
        2831: {
            "issue": 2831,
            "shipped_pr": 3229,
            "overlap_count": 1,
            "overlap_files": ["scripts/foo.sh"],
            "added_files": ["scripts/foo.sh"],
            "candidate_files": ["scripts/foo.sh"],
        }
    }
    gh_bin = _make_gh_mock(tmp_path, issues)
    check_bin = _make_check_shipped_mock(tmp_path, matches=matches)

    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(gh_bin),
        check_bin=str(check_bin),
    )
    assert exit_code == 0
    assert "Issues scanned: **2**" in report
    assert "Zombies found: **1**" in report
    assert "| #2831 | dx: add scripts/foo.sh | #3229 |" in report
    assert "`scripts/foo.sh`" in report


def test_run_audit_empty_queue(tmp_path, audit_module):
    gh_bin = _make_gh_mock(tmp_path, [])
    check_bin = _make_check_shipped_mock(tmp_path, matches=None)
    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(gh_bin),
        check_bin=str(check_bin),
    )
    assert exit_code == 0
    assert "Issues scanned: **0**" in report
    assert "No zombies found." in report


def test_run_audit_preflight_fails_when_gh_missing(tmp_path, audit_module):
    # Point at a non-existent gh binary.
    missing_gh = tmp_path / "definitely-not-installed-gh"
    check_bin = _make_check_shipped_mock(tmp_path, matches=None)
    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(missing_gh),
        check_bin=str(check_bin),
    )
    assert exit_code == 1
    assert "error" in report.lower()


def test_run_audit_preflight_fails_when_check_bin_missing(tmp_path, audit_module):
    gh_bin = _make_gh_mock(tmp_path, [])
    missing_check = tmp_path / "missing-check.sh"
    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(gh_bin),
        check_bin=str(missing_check),
    )
    assert exit_code == 1
    assert "error" in report.lower()


def test_run_audit_preflight_fails_when_check_bin_not_executable(
    tmp_path, audit_module
):
    gh_bin = _make_gh_mock(tmp_path, [])
    not_exec = tmp_path / "not-exec.sh"
    not_exec.write_text("#!/bin/bash\nexit 0\n")
    # Deliberately do not chmod +x.
    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(gh_bin),
        check_bin=str(not_exec),
    )
    assert exit_code == 1
    assert "error" in report.lower()


def test_run_audit_skips_issues_with_non_int_number(tmp_path, audit_module):
    # Defensive: gh is unlikely to ever return non-int numbers, but if a
    # hand-crafted fixture or a future API change does, we should skip them
    # rather than crash.
    issues = [
        {"number": "not-an-int", "title": "garbage"},
        {"number": 200, "title": "real issue"},
    ]
    gh_bin = _make_gh_mock(tmp_path, issues)
    check_bin = _make_check_shipped_mock(tmp_path, matches=None)
    exit_code, report = audit_module.run_audit(
        repo="judgemind/judgemind",
        limit=10,
        gh_bin=str(gh_bin),
        check_bin=str(check_bin),
    )
    assert exit_code == 0
    # Both rows are counted in issues_scanned (mirrors gh's view), but the
    # non-int one is silently skipped during checking.
    assert "Issues scanned: **2**" in report


# ─── End-to-end: run the script as a subprocess ────────────────────────────


def test_main_dry_run_exits_zero_and_prints_report(tmp_path, audit_module):
    """Acceptance criterion #1: ``--dry-run`` exits 0 and prints markdown."""

    issues = [{"number": 2831, "title": "dx: add scripts/foo.sh"}]
    matches = {
        2831: {
            "issue": 2831,
            "shipped_pr": 3229,
            "overlap_count": 1,
            "overlap_files": ["scripts/foo.sh"],
            "added_files": ["scripts/foo.sh"],
            "candidate_files": ["scripts/foo.sh"],
        }
    }
    gh_bin = _make_gh_mock(tmp_path, issues)
    check_bin = _make_check_shipped_mock(tmp_path, matches=matches)

    env = os.environ.copy()
    env["AUDIT_SHIPPED_GH_BIN"] = str(gh_bin)
    env["AUDIT_SHIPPED_CHECK_BIN"] = str(check_bin)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "# Shipped-zombie audit report" in result.stdout
    assert "| #2831 |" in result.stdout
    assert "#3229" in result.stdout


def test_main_with_no_matches_states_no_zombies_found(tmp_path):
    """Acceptance criterion #2 (negative case): explicit count when nothing matches."""

    issues = [{"number": 9001, "title": "feat: nothing shipped"}]
    gh_bin = _make_gh_mock(tmp_path, issues)
    check_bin = _make_check_shipped_mock(tmp_path, matches=None)

    env = os.environ.copy()
    env["AUDIT_SHIPPED_GH_BIN"] = str(gh_bin)
    env["AUDIT_SHIPPED_CHECK_BIN"] = str(check_bin)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "No zombies found." in result.stdout
    assert "Issues scanned: **1**" in result.stdout


# ─── Header / structure invariants ─────────────────────────────────────────


def test_script_carries_permanent_marker():
    """Acceptance criterion #1 (suffix): ``# permanent: true`` header present."""

    text = SCRIPT_PATH.read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:10])
    assert "# permanent: true" in head


def test_script_carries_venv_header():
    """Acceptance criterion #1 (suffix): ``# venv:`` header present."""

    text = SCRIPT_PATH.read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:10])
    # The issue allowed `# venv: scripts` or none; we used `# venv: none`
    # because the script is pure stdlib + gh shell-out.
    assert "# venv:" in head


def test_script_is_executable():
    """Acceptance criterion #1 (suffix): script is executable."""

    mode = SCRIPT_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, f"{SCRIPT_PATH} is not executable"


def test_docstring_lists_manual_review_steps():
    """Acceptance criterion #3: top-of-file docstring lists manual review."""

    text = SCRIPT_PATH.read_text(encoding="utf-8")
    # The docstring should reference: report skim, /task #N, §4a.2 pivot.
    assert "/task #N" in text
    assert "§4a.2" in text
    assert "verify-and-close" in text
