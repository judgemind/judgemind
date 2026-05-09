"""Shared helper for the canonical ``Blocked by #N`` issue-body reference.

Three call sites historically maintained the same regex independently:

* ``scripts/dispatcher/daemon.py`` — ``DispatcherDaemon._parse_blocked_by``
  (the static-method shim used by the planning/enrichment paths to
  populate ``blocked_by`` in the issue bundle).
* ``scripts/dispatcher/daemon.py`` — the inline ``re.findall`` call inside
  ``_normalise_issue_record`` that builds the ``blockedBy`` enrichment
  for the blocked-snapshot codepath (issue #2989).
* ``scripts/dispatcher/agent-runner-entrypoint.sh`` — the inline Python
  shim that builds ``dispatcher-input/<phase>.json`` for the
  agent-runner ECS task (mirrors the daemon's ``_fetch_issue_bundle``
  shape so ECS-mode agents reach the same verdicts as subprocess-mode
  agents).

This module is the single source of truth for the regex shape and the
``parse_blocked_by(body) -> list[int]`` contract. All three call sites
delegate here. The hygiene check
``scripts/check-no-duplicate-blocked-by-regex.sh`` rejects future
re-introductions of the regex outside this file.

The regex accepts both ``Blocked by #N`` and ``Blocked by: #N`` forms
(the optional colon variant) — same convention
``scripts/unblock-dependents.sh`` uses, and the same convention
``test_daemon_enrichment.py`` exercises. Pre-extraction the
agent-runner-entrypoint shim used a stricter no-colon-only regex
(``r"(?im)^\\s*blocked by\\s+#(\\d+)\\s*$"``); migrating it to this
helper fixes that latent inconsistency between the two execution modes.

Issue #4514. Mirrors the same drift-prevention principle behind the
``parent_issue`` extraction (#4508 / PR #4511) and the
``framework.s3_keys`` extraction (#4447 / #4456).
"""

from __future__ import annotations

import re

# Canonical regex for the ``Blocked by #N`` body reference.
#
# Anchored with ``^...$`` plus ``re.MULTILINE`` so a line whose only
# content is the blocker reference is matched, even when surrounded by
# other body content. ``re.IGNORECASE`` is intentional — issue authors
# are inconsistent about ``Blocked by:`` vs ``blocked by:``.
#
# The ``:?`` allows both ``Blocked by #42`` (the canonical form
# ``scripts/block-issue.sh`` writes) and ``Blocked by: #42`` (the
# colon-suffixed form some humans write). Group 1 captures the issue
# number as a digit run; the caller converts to int. The trailing
# ``\s*$`` tolerates trailing whitespace before the line terminator
# (a common copy-paste artifact).
BLOCKED_BY_RE = re.compile(r"(?im)^\s*blocked by:?\s+#(\d+)\s*$")


def parse_blocked_by(body: str | None) -> list[int]:
    """Extract every ``Blocked by #N`` reference from an issue body.

    Returns the list of integer issue numbers in document order, or an
    empty list when no references are present (or when ``body`` is
    ``None`` / empty). All matches are returned — the issue body
    convention is one ``Blocked by`` line per blocker, and the daemon's
    enrichment paths consume the full list to populate
    ``dispatcher.agents.blocked_by`` and the
    ``planning.json::blocked_by`` field.
    """
    if not body:
        return []
    return [int(m) for m in BLOCKED_BY_RE.findall(body)]
