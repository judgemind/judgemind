"""Cross-site consistency tests for the CI-rollup classifier (#4417).

The classifier rule lives in
:func:`scripts.dispatcher.phase_transitions._ci_rollup_state` and is
surfaced to non-Python callers via
``scripts/dispatcher/ci_classifier_cli.py``.  Three callers consume
the rule:

* The daemon (``DispatcherDaemon._extract_failing_jobs``) — for
  building the fix-CI input bundle.
* The Fargate agent-runner Python helper (``_extract_failing_jobs``
  inside ``agent-runner-entrypoint.sh``'s phase-input shim) — same
  failing-job extraction shape.
* The agent-runner Bash function (``classify_pr_rollup``) and the
  operator dashboard (``scripts/worker-status.sh``) — both invoke the
  CLI for the rollup verdict.

Pre-#4417 each spelled the rule out independently and the duplication
bit twice (#4407 / #4414).  This test asserts that every site resolves
the same canonical fixture set to the same verdict — drift breaks the
test loudly rather than waiting for a third recurrence.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_DISPATCHER_DIR = Path(__file__).resolve().parents[1]
_CLI_PATH = _DISPATCHER_DIR / "ci_classifier_cli.py"

if str(_DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR))

from phase_transitions import (  # noqa: E402
    _ci_rollup_state,
    extract_failing_jobs,
)


# ----- Shared canonical fixtures -----------------------------------------
# Mirror the four cases called out in the issue body, plus pending +
# dirty/unstable edges.

ROLLUP_GREEN = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
            "databaseId": 1,
        },
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "name": "deploy-vercel",
            "databaseId": 2,
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

ROLLUP_RED = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "name": "tests",
            "databaseId": 1,
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

ROLLUP_RED_PLUS_CANCELLED = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "name": "tests",
            "databaseId": 1,
        },
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "name": "deploy-vercel",
            "databaseId": 2,
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

ROLLUP_CANCELLED_ONLY = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "name": "deploy-vercel",
            "databaseId": 1,
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
}

ROLLUP_PENDING = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "IN_PROGRESS",
            "conclusion": "",
            "name": "tests",
            "databaseId": 1,
        },
    ],
    "mergeable": "UNKNOWN",
    "mergeStateStatus": "UNKNOWN",
}

ROLLUP_DIRTY = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
            "databaseId": 1,
        },
    ],
    "mergeable": "CONFLICTING",
    "mergeStateStatus": "DIRTY",
}

ROLLUP_UNSTABLE = {
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "name": "ci-passed",
            "databaseId": 1,
        },
    ],
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "UNSTABLE",
}


_FIXTURES = [
    pytest.param(ROLLUP_GREEN, "green", id="cancelled_plus_green"),
    pytest.param(ROLLUP_RED, "red", id="failure"),
    pytest.param(ROLLUP_RED_PLUS_CANCELLED, "red", id="failure_plus_cancelled"),
    pytest.param(ROLLUP_CANCELLED_ONLY, "green", id="cancelled_only_mergeable"),
    pytest.param(ROLLUP_PENDING, "pending", id="pending"),
    pytest.param(ROLLUP_DIRTY, "red", id="dirty_conflicting"),
    pytest.param(ROLLUP_UNSTABLE, "pending", id="unstable_recompute"),
]


def _cli_verdict(payload: dict) -> str:
    result = subprocess.run(
        [sys.executable, str(_CLI_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    return result.stdout.strip()


# --------------------------------------------------------------------------
# Cross-site verdict parity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload, expected", _FIXTURES)
def test_python_rule_matches_cli(payload: dict, expected: str) -> None:
    """``_ci_rollup_state`` (Python) and the CLI agree per fixture."""
    py_verdict = _ci_rollup_state(payload)
    cli_verdict = _cli_verdict(payload)
    assert py_verdict == expected
    assert cli_verdict == expected
    assert py_verdict == cli_verdict


@pytest.mark.parametrize("payload, expected", _FIXTURES)
def test_daemon_extract_failing_jobs_matches_shared_helper(
    payload: dict, expected: str
) -> None:
    """``DispatcherDaemon._extract_failing_jobs`` and the shared
    helper produce the same failing-job list — i.e. the daemon's
    method must be a pure thin wrapper around
    ``phase_transitions.extract_failing_jobs``.
    """
    # Late import — daemon pulls in heavy deps (boto3, psycopg).
    from scripts.dispatcher import daemon  # noqa: PLC0415

    shared = extract_failing_jobs(payload, max_jobs=daemon.FIX_CI_MAX_FAILING_JOBS)
    daemon_result = daemon.DispatcherDaemon._extract_failing_jobs(payload)
    assert shared == daemon_result


def test_shared_helper_excludes_cancelled() -> None:
    """``CANCELLED`` is intentionally non-blocking (#4414) — same
    behavior the daemon and the agent-runner have shipped since the
    Vercel cancel-in-progress drift surfaced.  Regression-asserted
    here so the rule stays anchored to a fixture."""
    failing = extract_failing_jobs(ROLLUP_RED_PLUS_CANCELLED)
    names = {f["name"] for f in failing}
    assert "tests" in names
    assert "deploy-vercel" not in names


def test_shared_helper_caps_at_max_jobs() -> None:
    """``max_jobs`` honoured — daemon and entrypoint both pass
    ``FIX_CI_MAX_FAILING_JOBS``."""
    payload = {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": f"job{i}"}
            for i in range(20)
        ]
    }
    failing = extract_failing_jobs(payload, max_jobs=5)
    assert len(failing) == 5


def test_shared_helper_unbounded_when_max_jobs_none() -> None:
    """Default (no cap) — caller can opt out by passing None."""
    payload = {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": f"job{i}"}
            for i in range(20)
        ]
    }
    failing = extract_failing_jobs(payload)
    assert len(failing) == 20


def test_shared_helper_handles_none_status() -> None:
    """Defensive — None / empty input → empty list (not raise)."""
    assert extract_failing_jobs(None) == []
    assert extract_failing_jobs({}) == []
    assert extract_failing_jobs({"statusCheckRollup": None}) == []
