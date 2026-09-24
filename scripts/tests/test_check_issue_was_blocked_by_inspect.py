# venv: none
"""Tests for ``scripts/_check_issue_was_blocked_by_inspect.py`` (issue #4610).

The inspector reads an issue body on stdin and a JSON map of
``{cited_number: {state, stateReason}}`` from ``$SIBLING_STATES_JSON``. It
detects the durable ``Was-blocked-by:`` provenance marker left by
``scripts/_unblock_dependents.py`` / ``unblock-issues.yml`` and decides
whether every cited former blocker is now closed-as-completed.

Branches covered:
  - no marker → ``clear:no-marker``
  - all cited former blockers closed-completed → ``was-blocked-by:<nums>``
  - one cited former blocker still open → ``clear:not-all-closed-completed``
  - one cited former blocker closed-as-not_planned → ``clear:not-all-closed-completed``
  - a cited former blocker missing from the state map → ``clear:not-all-closed-completed``
  - malformed ``$SIBLING_STATES_JSON`` → exit 1
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_issue_was_blocked_by_inspect.py"

# Mirror the test_unblock_dependents.py import style: make scripts/ importable.
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _import_helper_module():
    """Load the inspector as ``check_issue_was_blocked_by_inspect``."""
    spec = importlib.util.spec_from_file_location(
        "check_issue_was_blocked_by_inspect", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_issue_was_blocked_by_inspect"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def helper():
    return _import_helper_module()


def _run(helper, body: str, states_json: str, monkeypatch, capsys) -> tuple[int, str]:
    """Drive ``main()`` with the given body (stdin) and SIBLING_STATES_JSON."""
    monkeypatch.setenv("SIBLING_STATES_JSON", states_json)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    rc = helper.main()
    out = capsys.readouterr().out.strip()
    return rc, out


MARKER = "Was-blocked-by: #100, #101 (all closed-completed 2026-05-08; auto-unblocked)"


def test_no_marker(helper, monkeypatch, capsys):
    body = "## Problem\n\nA normal issue. Mentions #100 but no marker.\n"
    rc, out = _run(
        helper,
        body,
        '{"100": {"state": "closed", "stateReason": "COMPLETED"}}',
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert out == "clear:no-marker"


def test_all_closed_completed_pivot(helper, monkeypatch, capsys):
    states = (
        '{"100": {"state": "closed", "stateReason": "COMPLETED"}, '
        '"101": {"state": "closed", "stateReason": "completed"}}'
    )
    rc, out = _run(helper, f"Body\n\n{MARKER}\n", states, monkeypatch, capsys)
    assert rc == 0
    assert out == "was-blocked-by:100,101"


def test_one_still_open_clears(helper, monkeypatch, capsys):
    states = (
        '{"100": {"state": "closed", "stateReason": "COMPLETED"}, '
        '"101": {"state": "open", "stateReason": null}}'
    )
    rc, out = _run(helper, f"{MARKER}\n", states, monkeypatch, capsys)
    assert rc == 0
    assert out == "clear:not-all-closed-completed"


def test_not_planned_clears(helper, monkeypatch, capsys):
    states = (
        '{"100": {"state": "closed", "stateReason": "COMPLETED"}, '
        '"101": {"state": "closed", "stateReason": "NOT_PLANNED"}}'
    )
    rc, out = _run(helper, f"{MARKER}\n", states, monkeypatch, capsys)
    assert rc == 0
    assert out == "clear:not-all-closed-completed"


def test_missing_blocker_clears(helper, monkeypatch, capsys):
    # #101 is absent from the state map (unresolved reference).
    states = '{"100": {"state": "closed", "stateReason": "COMPLETED"}}'
    rc, out = _run(helper, f"{MARKER}\n", states, monkeypatch, capsys)
    assert rc == 0
    assert out == "clear:not-all-closed-completed"


def test_malformed_json_returns_1(helper, monkeypatch, capsys):
    rc, out = _run(helper, f"{MARKER}\n", "{not valid json", monkeypatch, capsys)
    assert rc == 1
    assert out == ""


def test_non_dict_json_returns_1(helper, monkeypatch, capsys):
    rc, out = _run(helper, f"{MARKER}\n", "[1, 2, 3]", monkeypatch, capsys)
    assert rc == 1
    assert out == ""


def test_single_blocker_marker(helper, monkeypatch, capsys):
    body = "Was-blocked-by: #4282 (all closed-completed 2026-05-08; auto-unblocked)\n"
    states = '{"4282": {"state": "closed", "stateReason": "COMPLETED"}}'
    rc, out = _run(helper, body, states, monkeypatch, capsys)
    assert rc == 0
    assert out == "was-blocked-by:4282"


def test_marker_with_leading_whitespace(helper, monkeypatch, capsys):
    body = "  Was-blocked-by: #100 (all closed-completed 2026-05-08; auto-unblocked)\n"
    states = '{"100": {"state": "closed", "stateReason": "COMPLETED"}}'
    rc, out = _run(helper, body, states, monkeypatch, capsys)
    assert rc == 0
    assert out == "was-blocked-by:100"
