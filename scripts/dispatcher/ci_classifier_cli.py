#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""CLI wrapper around :func:`phase_transitions._ci_rollup_state`.

Single source of truth for non-Python callers (``agent-runner-entrypoint.sh``
and ``scripts/worker-status.sh``) that need to classify a ``gh pr view
--json`` rollup as ``green`` / ``red`` / ``pending``.  Reads the JSON
payload from stdin and prints one of:

* ``green``   — all checks complete + green, ``mergeable=MERGEABLE`` +
  ``mergeStateStatus=CLEAN``.
* ``red``     — at least one check failed (FAILURE / TIMED_OUT /
  ACTION_REQUIRED / STARTUP_FAILURE) or the PR is dirty / conflicting.
* ``pending`` — at least one check is incomplete or the merge state
  is transient (UNKNOWN / UNSTABLE / not-yet-recomputed).
* ``error``   — stdin was empty or malformed JSON.  Distinct from
  ``red`` so callers can decide whether to retry the fetch (network
  glitch) vs treat as a real failure.

#4417 unified four duplicated implementations of this rule into this
single CLI:

1. Python ``_ci_rollup_state`` in ``scripts/dispatcher/phase_transitions.py``.
2. Bash + jq ``classify_pr_rollup`` in
   ``scripts/dispatcher/agent-runner-entrypoint.sh``.
3. Awk regex in ``scripts/worker-status.sh``.
4. ``_extract_failing_jobs`` mirrors in ``daemon.py`` and
   ``agent-runner-entrypoint.sh`` (now both call
   :func:`phase_transitions.extract_failing_jobs`).

Two prior bug-class recurrences forced fixes across every site:
#4407 (``wait-for-ci.sh`` CANCELLED handling) and #4414 (the four sites
above).  This CLI eliminates the divergence vector.

Usage::

    echo '{"statusCheckRollup": [...], "mergeable": "MERGEABLE", ...}' \\
        | python3 scripts/dispatcher/ci_classifier_cli.py

Always exits 0 — the verdict is on stdout.  Errors during stdin parsing
print ``error`` and still exit 0 so callers (especially shell-pipe
consumers) get a deterministic single-token answer without needing to
distinguish exit codes from output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``python3 scripts/dispatcher/ci_classifier_cli.py`` to import the
# sibling module without requiring an installed package — the script is
# permanent (lives next to the daemon) and runs from the repo root in
# both subprocess and Fargate paths.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# pylint: disable=wrong-import-position  # path manipulation above is intentional
from phase_transitions import _ci_rollup_state  # noqa: E402


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("error")
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print("error")
        return 0
    if not isinstance(payload, dict):
        print("error")
        return 0
    print(_ci_rollup_state(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
