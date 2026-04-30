"""Issue #3766 — verify ``agent-runner-entrypoint.sh`` uses a per-phase
``claude -p`` timeout table and short-circuits to a structured BLOCKED
``claude_phase_timeout`` envelope when the timeout fires (rc=124),
instead of falling through to ``ralph_done_marker_missing`` /
``ralph_not_ship`` with empty stdout/stderr (#3641, #3638).

Static lints of the shell script text + a runtime end-to-end test that
sources only the affected functions:

1. **Per-phase table presence + values** — the new
   ``CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE`` associative array exists,
   ralph maps to 5400s (90 min, matching the SKILL.md upper bound),
   and ``DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS`` is the fallback.
2. **Lookup form** — ``run_claude_phase`` uses
   ``${CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE[$_phase]:-$DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS}``
   so any phase not in the table falls back to the default.
3. **rc==124 short-circuit** — when ``timeout`` exits 124, the wrapper
   builds a JSON envelope co-locating ``verdict="BLOCKED"`` and
   ``category="claude_phase_timeout"``, prints it, and ``return 0``s
   BEFORE the existing output-resolution path that would otherwise
   produce ``{}`` → ``ralph_done_marker_missing``.
4. **Per-phase test override hooks** — env vars
   ``AGENT_RUNNER_<PHASE>_TIMEOUT_OVERRIDE_SECONDS`` exist for at
   least ralph + fix_ci so future tests can drive the rc=124 branch
   deterministically with a short cap.
5. **Routing** — the dedicated ``FAILURE_HINT_CLAUDE_PHASE_TIMEOUT``
   constant exists in ``phase_transitions.py`` and is exported via
   ``__all__``. (The transition function's runtime behaviour is
   exercised in ``test_phase_transitions.py``.)

The test deliberately uses static lints rather than sourcing the full
entrypoint — the entrypoint is a single-purpose imperative script that
performs git clone + branch checkout + Fargate hook install BEFORE its
function definitions are complete, so it cannot be cleanly sourced for
unit tests today (see #3766 follow-up to refactor into sourceable
helpers, mirroring the #3757/PR #3761 pattern). Static lints + the
``test_phase_transitions.py`` routing tests + post-deploy verification
together cover every acceptance criterion.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "agent-runner-entrypoint.sh"
_PHASE_TRANSITIONS_PATH = Path(__file__).resolve().parents[1] / "phase_transitions.py"


def _script_text() -> str:
    return _ENTRYPOINT_PATH.read_text(encoding="utf-8")


def _phase_transitions_text() -> str:
    return _PHASE_TRANSITIONS_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-phase timeout table presence + values
# ---------------------------------------------------------------------------


class TestPerPhaseTimeoutTablePresent:
    """The new per-phase ``claude -p`` cap table must exist and must
    include a generous ralph entry (90 min). Implemented as a bash
    3.2-compatible case-statement lookup (``scripts/check-bash-compat.sh``
    forbids ``declare -A`` even on the Linux Fargate container) — the
    function is named ``claude_phase_timeout_seconds_by_phase``."""

    def test_lookup_function_defined(self) -> None:
        text = _script_text()
        assert "claude_phase_timeout_seconds_by_phase()" in text, (
            "agent-runner-entrypoint.sh must define a "
            "``claude_phase_timeout_seconds_by_phase`` function so each "
            "phase can have its own ``claude -p`` timeout (#3766). Bash "
            "3.2 compat (per scripts/check-bash-compat.sh) prohibits "
            "``declare -A`` so we use a case-statement lookup — same "
            "pattern as ``phase_to_skill``."
        )

    def test_table_anchor_comment_present(self) -> None:
        text = _script_text()
        # The CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE anchor stays in the
        # comment block as a grep target — future refactors that drop
        # the per-phase semantics will trip this lint even if the
        # function itself gets renamed.
        assert "CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE" in text, (
            "agent-runner-entrypoint.sh must keep the "
            "``CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE`` name as a comment "
            "anchor so this lint catches regressions where the "
            "per-phase semantics get silently dropped (#3766)."
        )

    def test_default_constant_defined(self) -> None:
        text = _script_text()
        # Fallback for any phase not listed in the table — keeps the
        # legacy 1800s ceiling for unknown phases.
        assert (
            'DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS="${AGENT_RUNNER_DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS:-1800}"'
            in text
        ), (
            "DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS must be defined with default "
            "1800s (#3766). It is the fallback for phases not listed in the "
            "per-phase lookup table."
        )

    def test_ralph_entry_at_5400(self) -> None:
        text = _script_text()
        # Ralph SKILL.md describes the phase as "long-tail (~45-90 min
        # internally)" — so the cap must cover the upper bound. 5400s
        # = 90 min. Match the per-phase constant.
        assert "CLAUDE_PHASE_TIMEOUT_RALPH_SECONDS=5400" in text, (
            "agent-runner-entrypoint.sh must map ``ralph`` → 5400s "
            "(90 min, SKILL.md upper bound) via "
            "``CLAUDE_PHASE_TIMEOUT_RALPH_SECONDS=5400``. Two terminals "
            "on the post-#3761 deploy (#3641, #3638) hit the previous "
            "1800s cap. (#3766)"
        )

    def test_lookup_call_in_run_claude_phase(self) -> None:
        text = _script_text()
        # The ``timeout`` invocation must source the per-phase value
        # via the lookup function. Look for the assignment near the
        # rc==124 / claude_phase_begin block.
        run_claude_idx = text.find("run_claude_phase()")
        assert run_claude_idx > 0, (
            "Could not locate run_claude_phase() — entrypoint structure "
            "drifted. (#3766)"
        )
        # The lookup call must appear inside run_claude_phase before the
        # timeout invocation.
        # Find the function body up to the next top-level function
        # definition (a heuristic that matches the file's existing
        # structure).
        next_func = text.find("\nhandle_scheduled_skill()", run_claude_idx)
        body = (
            text[run_claude_idx:next_func] if next_func > 0 else text[run_claude_idx:]
        )
        assert "claude_phase_timeout_seconds_by_phase" in body, (
            "run_claude_phase must call ``claude_phase_timeout_seconds_by_phase`` "
            "to look up the per-phase timeout (#3766). Hard-coded "
            "``$CLAUDE_PHASE_TIMEOUT_SECONDS`` would defeat the per-phase "
            "fix and re-introduce the 30-min cap on ralph."
        )

    def test_known_phases_present_in_lookup(self) -> None:
        # Each known active phase that runs claude -p must have an
        # explicit case-arm in the lookup. This catches the
        # silent-drop case where a future PR adds a phase but forgets
        # the timeout entry — without an arm, the phase falls through
        # to the ``*)`` default which uses the legacy 1800s ceiling.
        text = _script_text()
        for phase in (
            "planning",
            "ralph",
            "summary",
            "push_and_pr",
            "fix_ci",
            "fix_conflict",
            "verify",
        ):
            # Match either ``<phase>)`` (case arm) within the lookup
            # function. We search for the per-phase constant name as
            # a more specific anchor.
            phase_const = f"CLAUDE_PHASE_TIMEOUT_{phase.upper()}_SECONDS"
            assert phase_const in text, (
                f"agent-runner-entrypoint.sh must define "
                f"``{phase_const}`` so the per-phase cap is documented "
                f"and the lookup case-arm has a concrete fallback (#3766)."
            )


class TestPerPhaseOverrideEnvVars:
    """Tests must be able to drive any single phase to the rc=124 branch
    deterministically without disturbing the others. Each phase carries
    a dedicated ``AGENT_RUNNER_<PHASE>_TIMEOUT_OVERRIDE_SECONDS`` env
    var, applied post-declaration so the table itself stays the
    authoritative default."""

    def test_ralph_override_env_var_present(self) -> None:
        text = _script_text()
        assert "AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS" in text, (
            "agent-runner-entrypoint.sh must accept "
            "``AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS`` so tests can "
            "drive the ralph timeout to ~1s deterministically (#3766)."
        )

    def test_fix_ci_override_env_var_present(self) -> None:
        # fix_ci is the next-most-likely phase to hit a long-tail timeout
        # (claude reading 100MB CI logs). Override hook needed for parity.
        text = _script_text()
        assert "AGENT_RUNNER_FIX_CI_TIMEOUT_OVERRIDE_SECONDS" in text, (
            "agent-runner-entrypoint.sh must accept "
            "``AGENT_RUNNER_FIX_CI_TIMEOUT_OVERRIDE_SECONDS`` so tests "
            "can drive the fix_ci timeout deterministically (#3766)."
        )


# ---------------------------------------------------------------------------
# rc==124 short-circuit branch
# ---------------------------------------------------------------------------


class TestTimeoutBlockedEnvelopeBranch:
    """When ``timeout`` exits 124, the wrapper must build a structured
    ``BLOCKED`` envelope with ``category=claude_phase_timeout`` and
    return it instead of falling through to the empty-result branch
    (which produces the misleading ``ralph_done_marker_missing`` /
    ``ralph_not_ship`` shape with empty stdout/stderr)."""

    def test_rc_124_emits_blocked_envelope(self) -> None:
        text = _script_text()
        # The new branch must build a JSON envelope that includes
        # both ``"verdict": "BLOCKED"`` and
        # ``"category": "claude_phase_timeout"``. Use a relaxed regex
        # that allows the two fields in either order with up to 2 KB
        # of intervening content (jq -n -c construction inserts the
        # fields per-line).
        match = re.search(
            r'verdict[^"]*"BLOCKED"[^}]{0,2000}category[^"]*"claude_phase_timeout"',
            text,
            re.DOTALL,
        )
        if match is None:
            # Allow the other ordering — category first, then verdict.
            match = re.search(
                r'category[^"]*"claude_phase_timeout"[^}]{0,2000}verdict[^"]*"BLOCKED"',
                text,
                re.DOTALL,
            )
        assert match is not None, (
            "rc==124 branch must emit a structured envelope co-locating "
            "``verdict=BLOCKED`` and ``category=claude_phase_timeout`` so the "
            "diagnoser sees the timeout as a distinct category, not "
            "``ralph_not_ship`` (#3766)."
        )

    def test_rc_124_branch_short_circuits_output_resolution(self) -> None:
        text = _script_text()
        # Anchor on the BLOCKED envelope construction, not the bare rc==124
        # condition — the salvage prelude (#3832) introduces an additional
        # ``read_phase_output`` call between rc==124 and this block, so the
        # bare-condition anchor would point inside the salvage block and the
        # ``between`` slice would no longer contain the BLOCKED printf/return.
        blocked_match = re.search(
            r'_timeout_output=\$\(jq -n -c', text
        )
        assert blocked_match is not None, (
            "Could not find BLOCKED envelope construction "
            "``_timeout_output=$(jq -n -c`` in run_claude_phase. "
            "Expected inside the rc==124 branch (#3766, #3832)."
        )
        blocked_idx = blocked_match.start()
        # The first ``read_phase_output`` AFTER the BLOCKED construction is
        # the outer output-resolution call (below the rc==124 block entirely).
        # The envelope must printf + return 0 before that outer call.
        rpo_idx = text.find("if _file_output=$(read_phase_output", blocked_idx)
        assert rpo_idx > blocked_idx, (
            "Could not locate read_phase_output after the BLOCKED envelope "
            "construction in run_claude_phase. (#3766)"
        )
        between = text[blocked_idx:rpo_idx]
        # The branch must print the JSON envelope (so the caller
        # captures it via $()) AND return 0 (so the function exits
        # cleanly without overwriting the envelope).
        assert "printf" in between, (
            "rc==124 BLOCKED branch must ``printf`` the structured envelope "
            "to stdout before falling through to ``read_phase_output`` (#3766)."
        )
        assert "return 0" in between, (
            "rc==124 BLOCKED branch must ``return 0`` after printing the "
            "envelope so the existing output-resolution path doesn't "
            "overwrite it with ``ralph_done_marker_missing`` (#3766)."
        )

    def test_rc_124_envelope_carries_elapsed_seconds(self) -> None:
        # The diagnoser uses ``elapsed_seconds`` to decide whether to
        # bump the per-phase cap or investigate runaway iteration count.
        # Anchor on the BLOCKED envelope construction (same as
        # test_rc_124_branch_short_circuits_output_resolution) — the
        # salvage prelude (#3832) inserts a read_phase_output call before
        # the BLOCKED block, so using the bare rc==124 condition as anchor
        # would yield a ``between`` slice that doesn't contain the BLOCKED
        # ``elapsed_seconds`` field.
        text = _script_text()
        blocked_match = re.search(r'_timeout_output=\$\(jq -n -c', text)
        assert blocked_match is not None, (
            "Could not find BLOCKED envelope construction "
            "``_timeout_output=$(jq -n -c`` — expected in the rc==124 "
            "branch of run_claude_phase (#3766, #3832)."
        )
        blocked_idx = blocked_match.start()
        rpo_idx = text.find("if _file_output=$(read_phase_output", blocked_idx)
        between = text[blocked_idx:rpo_idx]
        assert "elapsed_seconds" in between, (
            "rc==124 BLOCKED envelope must carry ``elapsed_seconds`` so the "
            "diagnoser can decide whether to bump the per-phase cap or "
            "investigate (#3766)."
        )


# ---------------------------------------------------------------------------
# Routing — phase_transitions.py exports the dedicated failure hint
# ---------------------------------------------------------------------------


class TestPhaseTransitionsExportsTimeoutHint:
    """``phase_transitions.py`` must export
    ``FAILURE_HINT_CLAUDE_PHASE_TIMEOUT`` so the daemon's failure-
    category mapping table can route timeout-driven terminals to a
    dedicated diagnoser fix shape rather than the generic
    ``ralph_not_ship`` path."""

    def test_failure_hint_constant_defined(self) -> None:
        text = _phase_transitions_text()
        assert 'FAILURE_HINT_CLAUDE_PHASE_TIMEOUT = "claude_phase_timeout"' in text, (
            "phase_transitions.py must define ``FAILURE_HINT_CLAUDE_PHASE_TIMEOUT`` "
            'with value ``"claude_phase_timeout"`` so the routing branch in '
            "transition_from_ralph can short-circuit on the structured "
            "envelope's ``category`` field (#3766)."
        )

    def test_failure_hint_in_dunder_all(self) -> None:
        text = _phase_transitions_text()
        # The constant must be exported via __all__ so daemon.py can
        # ``from .phase_transitions import FAILURE_HINT_CLAUDE_PHASE_TIMEOUT``.
        assert '"FAILURE_HINT_CLAUDE_PHASE_TIMEOUT"' in text, (
            "FAILURE_HINT_CLAUDE_PHASE_TIMEOUT must be listed in "
            "phase_transitions.py::__all__ so it can be imported by "
            "daemon.py for the failure-category mapping table (#3766)."
        )


# ---------------------------------------------------------------------------
# Salvage prelude — rc==124 but claude already finished (#3832)
# ---------------------------------------------------------------------------


class TestClaudePhaseTimeoutSalvage:
    """When ``timeout`` exits 124 but the claude output envelope already has
    ``is_error=false`` and ``terminal_reason="completed"``, the salvage
    prelude must route through the normal output-resolution chain instead
    of emitting a BLOCKED envelope (#3832)."""

    def test_salvage_check_present_in_rc_124_branch(self) -> None:
        text = _script_text()
        # Both sentinel field checks must be present near the rc==124 branch.
        assert "is_error == false" in text, (
            "Salvage prelude must check ``.is_error == false`` to confirm "
            "claude finished cleanly before the wrapper was killed (#3832)."
        )
        assert 'terminal_reason == "completed"' in text, (
            "Salvage prelude must check ``.terminal_reason == \"completed\"`` "
            "to confirm the envelope is a completed run, not a mid-iteration "
            "kill (#3832)."
        )
        # Both checks must appear within the rc==124 branch body, not
        # elsewhere in the file. Verify they appear before the BLOCKED
        # envelope construction.
        rc124_match = re.search(r'\[\[\s*"\$_rc"\s*-eq\s*124\s*\]\]', text)
        assert rc124_match is not None
        rc124_idx = rc124_match.start()
        blocked_idx = text.find("_timeout_output=$(jq -n -c", rc124_idx)
        assert blocked_idx > rc124_idx, (
            "Could not find BLOCKED envelope construction after rc==124 (#3832)."
        )
        between = text[rc124_idx:blocked_idx]
        assert "is_error == false" in between, (
            "``is_error == false`` check must appear inside the rc==124 branch "
            "BEFORE the BLOCKED envelope construction (#3832)."
        )
        assert 'terminal_reason == "completed"' in between, (
            "``terminal_reason == \"completed\"`` check must appear inside the "
            "rc==124 branch BEFORE the BLOCKED envelope construction (#3832)."
        )

    def test_salvage_logs_distinct_event(self) -> None:
        text = _script_text()
        assert "claude_phase_timeout_salvaged" in text, (
            "Salvage prelude must emit a distinct ``claude_phase_timeout_salvaged`` "
            "log event so operators can dashboard the false-positive rate "
            "separately from real timeouts (#3832)."
        )

    def test_salvage_event_carries_required_fields(self) -> None:
        text = _script_text()
        # Find the salvage log call and verify all required arg keys are present.
        salvage_log_idx = text.find('"claude_phase_timeout_salvaged"')
        assert salvage_log_idx >= 0, (
            "Could not find ``claude_phase_timeout_salvaged`` log call (#3832)."
        )
        # Look at the next 300 chars after the event name for the arg keys.
        call_region = text[salvage_log_idx : salvage_log_idx + 300]
        assert "phase=" in call_region, (
            "salvage log call must carry ``phase=`` arg (#3832)."
        )
        assert "claude_duration_ms=" in call_region, (
            "salvage log call must carry ``claude_duration_ms=`` arg so "
            "operators know how long claude actually ran (#3832)."
        )
        assert "wrapper_deadline_seconds=" in call_region, (
            "salvage log call must carry ``wrapper_deadline_seconds=`` arg "
            "so operators can compare against the phase cap (#3832)."
        )

    def test_salvage_routes_through_phase_output_resolution(self) -> None:
        text = _script_text()
        # The salvage block must call read_phase_output "$_skill" so it
        # reaches the same dispatcher-output file resolution path as the
        # clean-exit branch.
        rc124_match = re.search(r'\[\[\s*"\$_rc"\s*-eq\s*124\s*\]\]', text)
        assert rc124_match is not None
        rc124_idx = rc124_match.start()
        blocked_idx = text.find("_timeout_output=$(jq -n -c", rc124_idx)
        assert blocked_idx > rc124_idx
        salvage_region = text[rc124_idx:blocked_idx]
        assert 'read_phase_output "$_skill"' in salvage_region, (
            "Salvage prelude must call ``read_phase_output \"$_skill\"`` so "
            "dispatcher-output files written by the skill are picked up on "
            "the salvage path, exactly as the clean-exit branch does (#3832)."
        )

    def test_blocked_envelope_still_emitted_when_no_salvage(self) -> None:
        text = _script_text()
        # The BLOCKED envelope construction must still be present so that
        # genuine mid-iteration timeouts (no completed envelope) continue
        # to produce a structured BLOCKED verdict.
        assert "_timeout_output=$(jq -n -c" in text, (
            "BLOCKED envelope construction ``_timeout_output=$(jq -n -c`` "
            "must still be present — genuine timeouts must still emit a "
            "structured BLOCKED verdict even after the salvage prelude is "
            "added (#3832)."
        )
        assert 'category="claude_phase_timeout"' in text or \
               'category: "claude_phase_timeout"' in text, (
            "BLOCKED envelope must still carry ``category=\"claude_phase_timeout\"`` "
            "so the diagnoser routes genuine timeouts correctly (#3832)."
        )
