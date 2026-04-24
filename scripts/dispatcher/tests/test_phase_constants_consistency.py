"""Tests that PHASE_MAX_TURNS, PHASE_MODELS, and STUCK_TIMEOUT_SECONDS_BY_PHASE
share a consistent key set (modulo the intentional hyphen-vs-underscore drift
documented in daemon.PHASE_NAME_ALIASES).

Issue: #2889

The intentional drift: ``PHASE_MAX_TURNS`` and ``PHASE_MODELS`` use the
*hyphen* form (``fix-ci``) because those keys are passed directly to the
``claude -p /task-v2-fix-ci`` subprocess.  ``STUCK_TIMEOUT_SECONDS_BY_PHASE``
uses the *underscore* form (``fix_ci``) to match ``phases.PHASE_FLOW_FORWARD``
and the ``phase_transitions.phase`` DB column convention.  The
``PHASE_NAME_ALIASES`` constant maps hyphen keys to their underscore
equivalents so cross-dict assertions remain exact.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


def _normalize(phase: str) -> str:
    """Translate a phase name through PHASE_NAME_ALIASES if present."""
    return daemon.PHASE_NAME_ALIASES.get(phase, phase)


def test_phase_constants_cover_all_declared_phases() -> None:
    """Every key in PHASE_MAX_TURNS must map (via PHASE_NAME_ALIASES) to a key
    in STUCK_TIMEOUT_SECONDS_BY_PHASE.

    The daemon's ``_stuck_timeout_for_phase`` performs an exact-string lookup
    against ``STUCK_TIMEOUT_SECONDS_BY_PHASE``.  If a phase key from
    ``PHASE_MAX_TURNS`` has no entry there (even under the alias map), the
    phase silently falls back to the 30-min global default, potentially
    allowing a legitimate long-running phase to be incorrectly flagged as
    stuck.  Issue #2889.
    """
    missing = []
    for phase in daemon.PHASE_MAX_TURNS:
        normalized = _normalize(phase)
        if normalized not in daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE:
            missing.append(
                f"PHASE_MAX_TURNS key {phase!r} (normalized: {normalized!r}) "
                f"has no entry in STUCK_TIMEOUT_SECONDS_BY_PHASE"
            )
    assert not missing, "\n".join(missing)


def test_phase_models_keys_match_phase_max_turns() -> None:
    """PHASE_MODELS and PHASE_MAX_TURNS must share the same key set.

    ``_spawn_phase_subprocess`` looks up both dicts by the same ``phase``
    argument (lines that call ``PHASE_MAX_TURNS[phase]`` and
    ``_model_for_phase(phase, ...)``).  A key mismatch would raise a
    ``KeyError`` at runtime for the missing phase.
    """
    max_turns_keys = set(daemon.PHASE_MAX_TURNS)
    models_keys = set(daemon.PHASE_MODELS)
    assert max_turns_keys == models_keys, (
        f"PHASE_MAX_TURNS keys not equal to PHASE_MODELS keys.\n"
        f"  Only in PHASE_MAX_TURNS: {max_turns_keys - models_keys}\n"
        f"  Only in PHASE_MODELS:    {models_keys - max_turns_keys}"
    )


def test_phase_name_aliases_exported() -> None:
    """daemon.PHASE_NAME_ALIASES must equal the expected alias map.

    This test trips if a rename PR forgets to update the alias map, catching
    the drift before it can silently break ``_stuck_timeout_for_phase``.
    """
    assert daemon.PHASE_NAME_ALIASES == {"fix-ci": "fix_ci"}, (
        f"Expected PHASE_NAME_ALIASES == {{'fix-ci': 'fix_ci'}}, "
        f"got {daemon.PHASE_NAME_ALIASES!r}"
    )
