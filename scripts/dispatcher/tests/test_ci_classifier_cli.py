"""Unit tests for ``scripts/dispatcher/ci_classifier_cli.py`` (#4417).

The CLI is the canonical entry point that non-Python callers
(``agent-runner-entrypoint.sh:classify_pr_rollup`` and
``scripts/worker-status.sh``) use to classify a ``gh pr view`` rollup
as ``green`` / ``red`` / ``pending``.  These tests cover the four
canonical fixtures called out in the issue body plus pending and
error edge cases.

Each fixture is also asserted against ``phase_transitions._ci_rollup_state``
directly so any future drift between the Python implementation and
the CLI surface is caught immediately — that drift is exactly the bug
the refactor is preventing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_DISPATCHER_DIR = Path(__file__).resolve().parents[1]
_CLI_PATH = _DISPATCHER_DIR / "ci_classifier_cli.py"

# Import the canonical Python rule for parity assertions. ``conftest``
# already arranges sys.path, but be defensive in case this test runs
# in isolation.
if str(_DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR))

from phase_transitions import _ci_rollup_state  # noqa: E402


def _run_cli(payload: object | str) -> str:
    """Pipe ``payload`` (dict-like or raw string) into the CLI; return stdout."""
    if isinstance(payload, str):
        stdin_text = payload
    else:
        stdin_text = json.dumps(payload)
    result = subprocess.run(
        [sys.executable, str(_CLI_PATH)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    # CLI always exits 0 — the verdict is on stdout.
    assert result.returncode == 0, (
        f"CLI exited non-zero: rc={result.returncode}, "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    return result.stdout.strip()


# --------------------------------------------------------------------------
# Canonical fixtures from the issue body
# --------------------------------------------------------------------------


CANCELLED_PLUS_GREEN = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
        },
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "name": "deploy-vercel",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

FAILURE_ONLY = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "name": "tests",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

FAILURE_PLUS_CANCELLED = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "name": "tests",
        },
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "name": "deploy-vercel",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

CANCELLED_ONLY_MERGEABLE = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "name": "deploy-vercel",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

PENDING_FIXTURE = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "IN_PROGRESS",
            "conclusion": "",
            "name": "tests",
        },
    ],
    "mergeable": "UNKNOWN",
    "mergeStateStatus": "UNKNOWN",
}

DIRTY_FIXTURE = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
        },
    ],
    "mergeable": "CONFLICTING",
    "mergeStateStatus": "DIRTY",
}

UNSTABLE_FIXTURE = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "UNSTABLE",
}

STALE_FIXTURE = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "STALE",
            "name": "supersededjob",
        },
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

VERCEL_STATUSCONTEXT_GREEN = {
    "statusCheckRollup": [
        {
            "__typename": "StatusContext",
            "state": "SUCCESS",
            "context": "Vercel",
        },
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

VERCEL_STATUSCONTEXT_FAILURE = {
    "statusCheckRollup": [
        {
            "__typename": "StatusContext",
            "state": "FAILURE",
            "context": "Vercel",
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "UNSTABLE",
}


_CANONICAL_CASES = [
    pytest.param(CANCELLED_PLUS_GREEN, "green", id="cancelled_plus_green"),
    pytest.param(FAILURE_ONLY, "red", id="failure_only"),
    pytest.param(FAILURE_PLUS_CANCELLED, "red", id="failure_plus_cancelled"),
    pytest.param(CANCELLED_ONLY_MERGEABLE, "green", id="cancelled_only_mergeable"),
    pytest.param(PENDING_FIXTURE, "pending", id="pending_in_progress"),
    pytest.param(DIRTY_FIXTURE, "red", id="dirty_conflicting"),
    pytest.param(UNSTABLE_FIXTURE, "pending", id="unstable_recompute"),
    pytest.param(STALE_FIXTURE, "green", id="stale_treated_as_skip"),
    pytest.param(VERCEL_STATUSCONTEXT_GREEN, "green", id="vercel_statuscontext_green"),
    pytest.param(
        VERCEL_STATUSCONTEXT_FAILURE, "red", id="vercel_statuscontext_failure"
    ),
]


# --------------------------------------------------------------------------
# CLI surface tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload, expected", _CANONICAL_CASES)
def test_cli_classifies_canonical_fixtures(payload: dict, expected: str) -> None:
    """The CLI emits the expected single-token verdict for every canonical fixture."""
    assert _run_cli(payload) == expected


def test_cli_empty_input_emits_error() -> None:
    """Empty stdin → ``error`` (distinct from ``red`` so callers can retry)."""
    assert _run_cli("") == "error"


def test_cli_whitespace_only_input_emits_error() -> None:
    assert _run_cli("   \n  ") == "error"


def test_cli_malformed_json_emits_error() -> None:
    assert _run_cli("not json {{{") == "error"


def test_cli_non_object_payload_emits_error() -> None:
    """A bare list / number / string is not a rollup payload."""
    assert _run_cli("[]") == "error"
    assert _run_cli("42") == "error"
    assert _run_cli('"green"') == "error"


def test_cli_missing_rollup_field_is_pending() -> None:
    """Empty ``{}`` → no rollup → :func:`_ci_rollup_state` returns
    ``pending`` because ``mergeable`` is also missing (rule 4 fails)."""
    assert _run_cli({}) == "pending"


# --------------------------------------------------------------------------
# Parity with the canonical Python implementation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload, expected", _CANONICAL_CASES)
def test_cli_output_matches_python_rule(payload: dict, expected: str) -> None:
    """The CLI's verdict string must equal what
    ``phase_transitions._ci_rollup_state`` returns directly. Drift
    between the two is exactly the bug class the #4417 refactor is
    preventing.
    """
    cli_verdict = _run_cli(payload)
    python_verdict = _ci_rollup_state(payload)
    assert cli_verdict == python_verdict
    assert python_verdict == expected
