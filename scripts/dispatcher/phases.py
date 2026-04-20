"""Canonical dispatcher phase names and the forward flow string.

Single source of truth for the per-phase pipeline (#2899). The daemon's
scheduler loop (see ``docs/specs/dispatcher-v2-spec.md`` §6) drives an
agent through a fixed sequence of phases, and the admin cockpit's phase
tooltip surfaces that sequence to operators so they can answer "what
comes next?" without reading the daemon code.

Before #2899 the sequence was re-derived implicitly: Python code had
``PHASE_SEQUENCE = ("plan", "ralph", "summary")`` (the three LLM phases
3A orchestrates) but no end-to-end list including mechanical phases,
and the ``phase`` cell in the admin table was opaque. The tooltip
strings that serve the admin UI now derive from
:data:`PHASE_FLOW_FORWARD` here, and a parallel TS constant in
``packages/web/src/lib/dispatcher-phase-flow.ts`` mirrors it — a
parity test (``packages/web/src/lib/__tests__/dispatcher-phase-flow.test.ts``)
reads this module's source and asserts the two lists stay in sync.

Why a flat list instead of a graph
----------------------------------
The actual phase map in the daemon has one conditional branch
(``ci_watch → ci_fix`` on red CI, otherwise ``ci_watch → merge``) plus
retry loops — not a strict linear sequence. For an operator tooltip we
want a single human-readable string, not a diagram. The convention is:

- :data:`PHASE_FLOW_FORWARD` is the HAPPY path through the pipeline
  (CI green, deploy OK, verify OK). Every phase listed is a phase the
  admin table will ever display as the current ``phase`` value.
- :data:`CONDITIONAL_PHASES` names the phases that only appear on the
  unhappy branch (``ci_fix`` today). The tooltip renders them with a
  trailing ``*`` to signal "conditional".

Adding a new phase
------------------
1. Add the name here in :data:`PHASE_FLOW_FORWARD` at the correct
   position.
2. Add the matching entry to ``packages/web/src/lib/dispatcher-phase-flow.ts``.
3. The parity test fails on mismatch — CI catches drift.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

#: Happy-path phase sequence in daemon execution order. Matches
#: ``docs/specs/dispatcher-v2-spec.md`` §6 ("Scheduler Loop") and the
#: per-agent state machine in ``scripts/dispatcher/daemon.py``. The
#: mechanical phases (``claiming``, ``setup``, ``push_and_pr``, etc.)
#: are included because the admin table can display any of them as the
#: current ``phase`` value — an operator seeing ``push_and_pr`` in the
#: tooltip should see that phase in the flow and understand what comes
#: after.
#:
#: Order is deliberate: every entry is either the exit label of the
#: previous entry (e.g. ``planning`` → ``setup``) or the canonical
#: label for the phase that runs next. See the phase map in daemon.py
#: ``advance_per_agent_state_machine`` comment block.
PHASE_FLOW_FORWARD: tuple[str, ...] = (
    "claiming",
    "planning",
    "setup",
    "ralph",
    "summary",
    "push_and_pr",
    "ci_watch",
    "merge",
    "deploy_watch",
    "verify",
    "retro",
)

#: Phases that only appear on the unhappy branch. The tooltip
#: decorates these with a trailing ``*`` to signal they are conditional
#: — e.g. ``fix_ci*`` appears between ``ci_watch`` and ``merge`` when
#: CI fails, but is skipped on green CI.
CONDITIONAL_PHASES: frozenset[str] = frozenset({"fix_ci"})


def build_flow_string(current_phase: str | None = None) -> str:
    """Render the forward flow as a human-readable arrow-separated string.

    Used by the admin cockpit's phase tooltip. The string always
    includes every happy-path phase in order, plus the conditional
    ``fix_ci*`` marker between ``ci_watch`` and ``merge`` so operators
    can see that one failure branch exists. When ``current_phase`` is
    provided and matches a phase in the flow, that phase is surrounded
    by square brackets (``[current]``) so the operator's eye can land
    on "where am I now" at a glance.

    The returned string deliberately stays plain ASCII / unicode
    arrows — no HTML — because it is injected into the native
    ``title`` attribute (which does not parse markup).
    """
    # Assemble the full sequence including the single conditional
    # marker between ci_watch and merge.
    assembled: list[str] = []
    for name in PHASE_FLOW_FORWARD:
        assembled.append(name)
        if name == "ci_watch":
            # fix_ci* sits between ci_watch and merge on the red branch.
            assembled.append("fix_ci*")

    if current_phase:
        assembled = [
            f"[{name}]" if _matches_current(name, current_phase) else name
            for name in assembled
        ]

    return " \u2192 ".join(assembled)


def _matches_current(flow_entry: str, current_phase: str) -> bool:
    """Return True when ``flow_entry`` names the agent's current phase.

    The daemon sometimes writes phase names with hyphens instead of
    underscores (legacy alias — ``fix-ci`` vs ``fix_ci``), so the
    comparison normalizes both sides to a canonical form before
    matching. The conditional marker ``fix_ci*`` matches ``fix_ci``
    (or ``fix-ci``) as well.
    """
    canonical_entry = flow_entry.rstrip("*").replace("-", "_")
    canonical_current = current_phase.replace("-", "_")
    return canonical_entry == canonical_current


def tooltip_text(current_phase: str) -> str:
    """Return the two-line admin-tooltip text for a given phase.

    Matches the format in issue #2899::

        Currently in <phase> phase.
        Flow: <phase1> \u2192 <phase2> \u2192 ...

    The current phase is rendered both in the first line and
    highlighted in-line in the flow string so operators can locate
    their position without cross-referencing.
    """
    flow = build_flow_string(current_phase)
    return f"Currently in {current_phase} phase.\nFlow: {flow}"


__all__ = [
    "CONDITIONAL_PHASES",
    "PHASE_FLOW_FORWARD",
    "build_flow_string",
    "tooltip_text",
]
