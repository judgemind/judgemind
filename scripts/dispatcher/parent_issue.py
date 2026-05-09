"""Shared helper for the canonical ``Parent: #N`` issue-body reference.

Three call sites historically maintained the same regex independently:

* ``scripts/dispatcher/daemon.py`` — ``DispatcherDaemon._parse_parent_issue``
  (the daemon side, used during planning to record an agent's parent
  issue in ``dispatcher.agents.parent_issue``).
* ``scripts/dispatcher/agent-runner-entrypoint.sh`` — the inline Python
  shim that builds ``dispatcher-input/<phase>.json`` for the
  agent-runner ECS task (mirrors the daemon's ``_fetch_issue_bundle``
  shape so ECS-mode agents reach the same verdicts as subprocess-mode
  agents).
* ``scripts/_sweep_completed_parents.py`` — the auto-close meta-task
  helper (#4499) that groups child issues by parent.

This module is the single source of truth for the regex shape and the
``parse_parent_issue(body) -> int | None`` contract. All three call
sites delegate here. The hygiene check
``scripts/check-no-duplicate-parent-regex.sh`` rejects future
re-introductions of the regex outside this file.

Issue #4508. Mirrors the same drift-prevention principle behind the
``framework.s3_keys`` extraction (#4447 / #4456).
"""

from __future__ import annotations

import re

# Canonical regex for the ``Parent: #N`` body reference.
#
# Anchored with ``^...$`` plus ``re.MULTILINE`` so a line whose only
# content is the parent reference is matched, even when surrounded by
# other body content. ``re.IGNORECASE`` is intentional — issue authors
# are inconsistent about ``Parent:`` vs ``parent:``.
#
# Group 1 captures the issue number as a digit run; the caller converts
# to int. The trailing ``\s*$`` tolerates trailing whitespace before the
# line terminator (a common copy-paste artifact).
PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")


def parse_parent_issue(body: str | None) -> int | None:
    """Extract the first ``Parent: #N`` reference from an issue body.

    Returns the integer issue number, or ``None`` when no reference is
    present (or when ``body`` is ``None`` / empty). Only the first
    match is returned — the issue body convention is one parent per
    sub-task; multiple ``Parent:`` lines are not a recognised shape.
    """
    if not body:
        return None
    match = PARENT_RE.search(body)
    return int(match.group(1)) if match else None
