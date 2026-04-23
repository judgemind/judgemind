"""Pure phase-transition rules for the dispatcher v2 pipeline.

This module is the **single-source-of-truth for "given the current
phase and the output that phase produced, what's the next phase?"**.
It is deliberately **pure**: no DB, no subprocess, no logging, no
GitHub API. Just data in, data out. This makes the state machine
unit-testable in isolation and — critically — **reusable by an
agent-runner process that does NOT have a daemon DB connection** (see
`#3086 <https://github.com/judgemind/judgemind/issues/3086>`_ Stage 1a).

Why this module exists
----------------------

Today the daemon (``scripts/dispatcher/daemon.py``, ~18k lines)
orchestrates every agent's phase pipeline in-process: for each phase
it spawns a ``claude -p /task-v2-<phase>`` subprocess, parses the JSON
output, writes DB rows, and decides the next phase inline. The
"decide the next phase" logic lives in ~20 scattered
``_update_agent_phase(agent_id, "<next>")`` call sites, embedded in
side-effect-heavy methods.

Issue #3078 chose a per-agent ECS migration (Option A): one ECS task
per agent runs the full pipeline internally, turning the daemon into a
pure scheduler. The agent-runner needs to know **exactly the same
phase-transition rules** the daemon uses today — but without
inheriting the daemon's DB / gh / logger dependencies.

This module extracts those rules as pure functions so both callers
import the same logic:

* **Today — daemon** calls :func:`next_phase_from_verdict` at each
  orchestration point (follow-up PRs will migrate the scattered
  ``_update_agent_phase`` sites to use this module).
* **Tomorrow — agent-runner** (new ECS task def, #3086 Stage 1b) runs
  a phase loop driven entirely by this module.

Design principles
-----------------

1. **Pure.** Every function in this module returns a value; none
   perform I/O, DB writes, subprocess calls, or logging.
2. **Total.** Unknown phases / unknown verdicts return a recognizable
   sentinel value (:class:`PhaseTransition` with
   ``action=UNRECOGNIZED``) rather than raising. The caller logs +
   flips the agent to ``status='crashed'``; that policy stays in the
   daemon.
3. **Conservative.** When in doubt, the transition points at a
   terminal failure phase so the agent stops rather than silently
   advancing to an inconsistent state. The daemon's pre-#3086 policy
   (fail-loud on unknown verdict) is preserved.
4. **No phase name drift.** Phase string constants and the forward
   flow list live in :mod:`scripts.dispatcher.phases` (the canonical
   list used by the admin UI). This module imports those constants
   rather than re-declaring them.

What this module does NOT cover
-------------------------------

* **Retry / backoff scheduling.** The daemon's retry-marker processor
  (``_process_retry_markers`` + friends) owns when a ``crashed`` or
  ``retrying`` agent re-enters the loop. Retry budget, cooldown, and
  circuit-breaker policy are daemon-level concerns, not phase-
  transition concerns.
* **Failure-category selection.** The
  ``FAILURE_CATEGORY_RALPH_NOT_SHIP`` / ``_FIX_CI_BLOCKED`` / etc.
  string mapping lives in the daemon because the diagnoser's routing
  table is a daemon policy artifact. This module surfaces the
  *intent* (e.g. "transition to terminal failure with a fix-ci
  blocked reason") via :class:`PhaseTransition.failure_hint`; the
  daemon (or agent-runner) maps the hint to the final category
  string at the call site.
* **Side effects on state-advance.** Stamping ``merged_at`` /
  ``verified_at``, writing ``dispatcher.phase_transitions`` rows,
  persisting ralph patches — all daemon responsibilities. This
  module only tells you "the next phase name is X".

Happy-path flow (for reference)
-------------------------------

::

    claiming → planning → setup → ralph → summary → push_and_pr
    → awaiting_ci → (fix_ci loop on red) → merge → awaiting_deploy
    → verify → retro → done / cleanup_done

Unhappy-path transitions are covered by the verdict rules below.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Phase name constants — mirror the daemon's internal strings verbatim so a
# call-site migration in daemon.py can swap ``"planning"`` (string literal)
# for ``PHASE_PLANNING`` (imported here) without changing behavior.
# ---------------------------------------------------------------------------

#: Initial phase written at claim time — before the plan phase runs.
PHASE_CLAIMING = "claiming"

#: Plan phase (``/task-v2-plan``). LLM read-only planning pass.
PHASE_PLANNING = "planning"

#: Dependency installation + worktree prep. Mechanical, no LLM.
PHASE_SETUP = "setup"

#: Ralph implementation loop (``/task-v2-ralph``). Worker + reviewer
#: iterations until SHIP or max-iterations.
PHASE_RALPH = "ralph"

#: Summary phase (``/task-v2-summary``). Maps implementation to AC and
#: produces commit message + PR body.
PHASE_SUMMARY = "summary"

#: Push + PR creation. Mechanical git + gh.
PHASE_PUSH_AND_PR = "push_and_pr"

#: CI watch after push. Polls PR status until green / red / timeout.
PHASE_AWAITING_CI = "awaiting_ci"

#: Fix-CI retry skill (``/task-v2-fix-ci``). Runs on red CI.
PHASE_FIX_CI = "fix_ci"

#: Merge step. Squash-merge the PR.
PHASE_MERGE = "merge"

#: Post-merge deploy watch. Polls deploy-dispatcher workflow until
#: rolling deploy lands on dev.
PHASE_AWAITING_DEPLOY = "awaiting_deploy"

#: Verify phase (``/task-v2-verify``). Exercises the deployed feature
#: on dev, posts evidence comment, stamps ``verified_at``.
PHASE_VERIFY = "verify"

#: Retro phase (``/task-v2-retro``). Files workflow-improvement
#: follow-ups.
PHASE_RETRO = "retro"

# Terminal phase names ------------------------------------------------------

#: Final terminal phase on the happy path. Status stays ``succeeded``.
PHASE_DONE = "done"

#: Retro completed successfully. Status stays ``succeeded``.
PHASE_RETRO_DONE = "retro_done"

#: Retro failed (timeout, non-zero exit, malformed output). Status
#: stays ``succeeded`` — retro is post-success bookkeeping.
PHASE_RETRO_FAILED = "retro_failed"

#: No-op SHIP terminal (ralph found no changes to make). Status stays
#: ``succeeded``. See #3039.
PHASE_NO_OP = "no_op"

#: Worktree cleanup completed. Final phase for a successful agent.
PHASE_CLEANUP_DONE = "cleanup_done"

#: Worktree cleanup refused (locked, no session log). Terminal.
PHASE_CLEANUP_BLOCKED = "cleanup_blocked"

#: Plan phase returned ``BLOCKED``. Terminal failure.
PHASE_PLAN_BLOCKED = "plan_blocked"

#: Operator force_stop terminal phase (see daemon.py #2884).
PHASE_FORCE_STOPPED = "force_stopped"

#: Daemon restart abandonment terminal phase.
PHASE_DAEMON_RESTART_ABANDONED = "daemon_restart_abandoned"

#: Killswitch engaged terminal phase.
PHASE_PAUSED_BY_KILLSWITCH = "paused_by_killswitch"

# ---------------------------------------------------------------------------
# Verdict constants — the string values produced by the phase-output JSONs.
# ---------------------------------------------------------------------------

#: Ralph SHIPped its review loop — proceed to summary + push.
VERDICT_SHIP = "SHIP"

#: Ralph / summary / verify identified a structurally-impossible AC.
#: Route to diagnoser.
VERDICT_AC_INFEASIBLE = "AC_INFEASIBLE"

#: Fix-CI applied a patch — loop back to CI watch.
VERDICT_PATCHED = "PATCHED"

#: Fix-CI thinks the failure was flaky — wait, re-poll.
VERDICT_FLAKY = "FLAKY"

#: Fix-CI / plan / any phase surfaced a non-retryable blocker. Route
#: to diagnoser / operator.
VERDICT_BLOCKED = "BLOCKED"

#: Verify failed post-merge. Regression signal.
VERDICT_FAILED = "FAILED"

#: Verify passed / skipped cleanly.
VERDICT_VERIFIED = "VERIFIED"
VERDICT_SKIPPED = "SKIPPED"

# ---------------------------------------------------------------------------
# Terminal-status enumeration. Mirrors ``TERMINAL_AGENT_STATUSES`` in
# daemon.py (the authoritative frozenset there also carries this set).
# ---------------------------------------------------------------------------


class AgentStatus(str, Enum):
    """Agent status values written to ``dispatcher.agents.status``.

    Mirrors the daemon's internal constants. ``str`` subclass so
    comparison against bare strings from DB reads still works.
    """

    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CRASHED = "crashed"
    PLAN_BLOCKED = "plan_blocked"
    NEEDS_REVIEW = "needs_review"


#: Terminal agent statuses that stop further phase advancement.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        AgentStatus.SUCCEEDED.value,
        AgentStatus.FAILED.value,
        AgentStatus.CRASHED.value,
        AgentStatus.PLAN_BLOCKED.value,
        AgentStatus.NEEDS_REVIEW.value,
    }
)

# ---------------------------------------------------------------------------
# Terminal phases — the agent's phase loop exits when the next phase is one
# of these. The daemon's supervisor tick uses this set to decide whether an
# agent row is done advancing. The agent-runner (#3086 Stage 1b) will use
# the same set to break out of its phase loop.
# ---------------------------------------------------------------------------

TERMINAL_PHASES: frozenset[str] = frozenset(
    {
        PHASE_DONE,
        PHASE_RETRO_DONE,
        PHASE_RETRO_FAILED,
        PHASE_NO_OP,
        PHASE_CLEANUP_DONE,
        PHASE_CLEANUP_BLOCKED,
        PHASE_PLAN_BLOCKED,
        PHASE_FORCE_STOPPED,
        PHASE_DAEMON_RESTART_ABANDONED,
        PHASE_PAUSED_BY_KILLSWITCH,
    }
)

# ---------------------------------------------------------------------------
# Transition action enumeration. Captures the shape of what the caller
# should do with the returned transition — advance to another phase,
# mark terminal, route to diagnoser, or fail with an unrecognized input.
# ---------------------------------------------------------------------------


class TransitionAction(str, Enum):
    """What the caller should do with a :class:`PhaseTransition`.

    Kept as an enum rather than overloading ``next_phase`` with magic
    values so the caller can ``match`` on action and branch explicitly.
    Every branch is exercised by the unit tests.
    """

    #: Advance to ``next_phase``. Status stays ``running`` /
    #: ``succeeded`` (depends on whether we've passed merge).
    ADVANCE = "advance"

    #: Advance to ``next_phase`` AND flip status to the terminal value
    #: in ``terminal_status``. Used for the "merge happened, status
    #: flips to succeeded, phase moves to awaiting_deploy" shape and
    #: for final success terminal (``done`` → ``retro_done`` etc.).
    ADVANCE_WITH_STATUS = "advance_with_status"

    #: Caller should route through ``_handle_agent_failure`` with the
    #: category hinted in ``failure_hint``. The phase stays where it
    #: is (the failure row + diagnoser decide next steps).
    ROUTE_TO_DIAGNOSER = "route_to_diagnoser"

    #: Verdict / phase combination was not recognized. Caller flips
    #: the agent to ``crashed`` and logs ``unrecognized_verdict``.
    #: This is a defensive sentinel — existing code should never hit
    #: this branch, but a phase-output drift (skill emits a new
    #: verdict name) would surface here.
    UNRECOGNIZED = "unrecognized"


# ---------------------------------------------------------------------------
# Failure hint strings. These are NOT the daemon's
# ``FAILURE_CATEGORY_*`` constants — the daemon maintains its own list
# because the diagnoser routing table is a daemon policy concern. This
# module emits a short hint string; the caller maps it to the final
# category at the call site.
# ---------------------------------------------------------------------------

#: Ralph returned a non-SHIP, non-AC_INFEASIBLE verdict
#: (REVISE-exhausted, worker-STUCK twice). Maps to
#: ``FAILURE_CATEGORY_RALPH_NOT_SHIP`` in daemon.py.
FAILURE_HINT_RALPH_NOT_SHIP = "ralph_not_ship"

#: Ralph surfaced a structurally-impossible AC. Maps to
#: ``FAILURE_CATEGORY_RALPH_AC_INFEASIBLE``.
FAILURE_HINT_RALPH_AC_INFEASIBLE = "ralph_ac_infeasible"

#: Summary surfaced an AC_INFEASIBLE. Maps to
#: ``FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE``.
FAILURE_HINT_SUMMARY_AC_INFEASIBLE = "summary_ac_infeasible"

#: Fix-CI returned ``BLOCKED``. Maps to
#: ``FAILURE_CATEGORY_FIX_CI_BLOCKED``.
FAILURE_HINT_FIX_CI_BLOCKED = "fix_ci_blocked"

#: Verify failed post-merge. Maps to
#: ``FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE``.
FAILURE_HINT_VERIFY_FAILED_POST_MERGE = "verify_failed_post_merge"

#: Plan phase returned BLOCKED. Terminal (``status='plan_blocked'``).
FAILURE_HINT_PLAN_BLOCKED = "plan_blocked"


# ---------------------------------------------------------------------------
# Transition dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseTransition:
    """Result of a phase-transition decision.

    Attributes:
        action: What the caller should do (see :class:`TransitionAction`).
        next_phase: The phase to advance to. ``None`` for
            :attr:`TransitionAction.ROUTE_TO_DIAGNOSER` (the caller
            writes a failure row but does not change phase — the
            diagnoser does on its next tick) and for
            :attr:`TransitionAction.UNRECOGNIZED`.
        terminal_status: When ``action=ADVANCE_WITH_STATUS``, the
            status string to write to ``dispatcher.agents.status``.
            ``None`` otherwise.
        failure_hint: When ``action=ROUTE_TO_DIAGNOSER``, a short
            string the caller maps to the daemon's
            ``FAILURE_CATEGORY_*`` constant. ``None`` otherwise.
        reason: Human-readable reason tag, primarily for logs +
            tests. Not load-bearing.
    """

    action: TransitionAction
    next_phase: str | None = None
    terminal_status: str | None = None
    failure_hint: str | None = None
    reason: str = ""
    #: Extra structured context the caller may want to preserve in
    #: the failure row (e.g. ``block_reason``). Kept as a mapping
    #: rather than exposing individual fields so new context types
    #: don't churn this dataclass.
    context: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure transition functions — one per phase. Each takes the phase's
# output JSON (or similar minimal state) and returns a PhaseTransition.
# ---------------------------------------------------------------------------


def _verdict(output: Mapping[str, Any] | None) -> str:
    """Extract and upper-case the verdict from a phase-output mapping.

    Defensive — returns empty string on missing / non-string verdict
    so callers can compare against known constants. Mirrors the
    defensive ``str(x.get("verdict") or "").upper()`` pattern used
    throughout daemon.py.
    """
    if not output:
        return ""
    raw = output.get("verdict")
    if raw is None:
        return ""
    return str(raw).strip().upper()


def transition_from_plan(output: Mapping[str, Any] | None) -> PhaseTransition:
    """Return the phase transition after ``/task-v2-plan`` runs.

    Happy path: plan completes → advance to ``ralph`` (setup is a
    mechanical phase the daemon does inline today, so we collapse
    ``setup`` into the plan→ralph edge for the agent-runner; the
    daemon's call sites still explicitly advance to ``setup`` so the
    admin cockpit renders the phase). The agent-runner (#3086 Stage
    1b) runs setup as part of its entrypoint preamble.

    Blocked path: plan emitted ``verdict='BLOCKED'`` — terminal failure
    with ``status='plan_blocked'``.
    """
    verdict = _verdict(output)
    if verdict == VERDICT_BLOCKED:
        return PhaseTransition(
            action=TransitionAction.ADVANCE_WITH_STATUS,
            next_phase=PHASE_PLAN_BLOCKED,
            terminal_status=AgentStatus.PLAN_BLOCKED.value,
            failure_hint=FAILURE_HINT_PLAN_BLOCKED,
            reason="plan returned BLOCKED verdict",
            context={
                "block_reason": (output or {}).get("block_reason"),
            },
        )
    # Any non-BLOCKED output from plan is treated as "plan done,
    # proceed to ralph". Plan does not have a SHIP / AC_INFEASIBLE
    # branch — it's either BLOCKED or done.
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_RALPH,
        reason="plan completed",
    )


def transition_from_ralph(output: Mapping[str, Any] | None) -> PhaseTransition:
    """Return the phase transition after ``/task-v2-ralph`` runs.

    Ralph verdicts:

    * ``SHIP`` — normal completion, advance to ``summary``.
    * ``AC_INFEASIBLE`` — ralph identified a structurally-impossible
      AC. Route to diagnoser (terminal failure row inserted with the
      ``ralph_ac_infeasible`` hint).
    * anything else (``REVISE``-exhausted, worker-STUCK twice) —
      non-SHIP terminal per #3054. Route through
      :func:`FAILURE_HINT_RALPH_NOT_SHIP`.
    """
    verdict = _verdict(output)
    if verdict == VERDICT_SHIP:
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_SUMMARY,
            reason="ralph SHIP",
        )
    if verdict == VERDICT_AC_INFEASIBLE:
        return PhaseTransition(
            action=TransitionAction.ROUTE_TO_DIAGNOSER,
            failure_hint=FAILURE_HINT_RALPH_AC_INFEASIBLE,
            reason="ralph AC_INFEASIBLE",
            context={
                "infeasible_acs": (output or {}).get("infeasible_acs") or [],
            },
        )
    # REVISE-exhausted, worker-STUCK-twice, or any other non-SHIP
    # terminal (#3054).
    return PhaseTransition(
        action=TransitionAction.ROUTE_TO_DIAGNOSER,
        failure_hint=FAILURE_HINT_RALPH_NOT_SHIP,
        reason=f"ralph non-SHIP verdict: {verdict or '(missing)'}",
        context={
            "verdict": verdict,
            "block_reason": (output or {}).get("block_reason"),
            "iterations_used": (output or {}).get("iterations_used"),
        },
    )


def transition_from_summary(output: Mapping[str, Any] | None) -> PhaseTransition:
    """Return the phase transition after ``/task-v2-summary`` runs.

    Summary verdicts:

    * ``AC_INFEASIBLE`` — summary surfaced an AC mismatch the ralph
      skill missed. Route to diagnoser.
    * anything else (including missing verdict) — advance to
      ``push_and_pr``. The summary skill's primary deliverable is the
      commit message + PR body, not a verdict gate, so the default
      action is "proceed".
    """
    verdict = _verdict(output)
    if verdict == VERDICT_AC_INFEASIBLE:
        return PhaseTransition(
            action=TransitionAction.ROUTE_TO_DIAGNOSER,
            failure_hint=FAILURE_HINT_SUMMARY_AC_INFEASIBLE,
            reason="summary AC_INFEASIBLE",
            context={
                "infeasible_acs": (output or {}).get("infeasible_acs") or [],
            },
        )
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_PUSH_AND_PR,
        reason="summary completed",
    )


def transition_from_push_and_pr(
    output: Mapping[str, Any] | None,
) -> PhaseTransition:
    """Return the phase transition after ``push_and_pr`` completes.

    push_and_pr is a mechanical phase (no LLM). Possible outputs:

    * ``no_op=True`` — ralph's #3039 no-op-SHIP guardrail fired; the
      working tree was clean on SHIP. Terminal success with phase
      ``no_op``, status ``succeeded``. No PR, no CI, no merge.
    * otherwise — PR was opened; advance to ``awaiting_ci``.
    """
    if output and output.get("no_op"):
        return PhaseTransition(
            action=TransitionAction.ADVANCE_WITH_STATUS,
            next_phase=PHASE_NO_OP,
            terminal_status=AgentStatus.SUCCEEDED.value,
            reason="push_and_pr no-op SHIP (#3039)",
        )
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_AWAITING_CI,
        reason="PR opened",
    )


def transition_from_awaiting_ci(
    pr_status: Mapping[str, Any] | None,
) -> PhaseTransition:
    """Return the phase transition after a CI poll.

    ``pr_status`` is the parsed ``gh pr view --json`` rollup (the
    shape produced by ``_fetch_pr_status`` in daemon.py). Possible
    results:

    * **Pending** (any check in_progress/queued, or mergeable is
      ``UNKNOWN``) — no-op; caller re-polls next tick. Returned as
      ``ADVANCE`` with ``next_phase=PHASE_AWAITING_CI`` (stays in
      place).
    * **Green** (all SUCCESS/SKIPPED + ``mergeable=MERGEABLE`` +
      ``mergeStateStatus=CLEAN``) — advance to ``merge``.
    * **Red** (any FAILURE/CANCELLED/TIMED_OUT/ACTION_REQUIRED) —
      advance to ``fix_ci``.
    """
    state = _ci_rollup_state(pr_status)
    if state == "green":
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_MERGE,
            reason="CI green",
        )
    if state == "red":
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_FIX_CI,
            reason="CI red",
        )
    # pending — stay in awaiting_ci.
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_AWAITING_CI,
        reason="CI pending",
    )


def transition_from_fix_ci(output: Mapping[str, Any] | None) -> PhaseTransition:
    """Return the phase transition after ``/task-v2-fix-ci`` runs.

    Fix-CI verdicts:

    * ``PATCHED`` — fix-ci wrote a patch; caller applies + pushes,
      then returns to ``awaiting_ci``.
    * ``FLAKY`` — no code change; caller stays in ``awaiting_ci``
      and re-polls.
    * ``BLOCKED`` (or unrecognized) — route to diagnoser.
    """
    verdict = _verdict(output)
    if verdict == VERDICT_PATCHED:
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_AWAITING_CI,
            reason="fix_ci PATCHED",
            context={"patch_applied": True},
        )
    if verdict == VERDICT_FLAKY:
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_AWAITING_CI,
            reason="fix_ci FLAKY",
            context={"patch_applied": False},
        )
    # BLOCKED or unrecognized -> diagnoser.
    return PhaseTransition(
        action=TransitionAction.ROUTE_TO_DIAGNOSER,
        failure_hint=FAILURE_HINT_FIX_CI_BLOCKED,
        reason=f"fix_ci non-retryable verdict: {verdict or '(missing)'}",
        context={
            "verdict": verdict,
            "block_reason": (output or {}).get("block_reason"),
        },
    )


def transition_from_merge(
    merge_succeeded: bool,
    *,
    is_self_deploy: bool = False,
) -> PhaseTransition:
    """Return the phase transition after ``gh pr merge`` completes.

    ``merge_succeeded`` is ``True`` when ``gh pr merge --squash``
    exited 0. ``is_self_deploy`` is ``True`` when the PR touched
    dispatcher source (``scripts/dispatcher/``) — in that case
    verify cannot meaningfully run against a process that is about to
    be replaced by its own deploy (see
    ``VERIFY_SKIP_REASON_SELF_DEPLOY`` in daemon.py); we skip
    directly from awaiting_deploy straight past verify.

    Merge-failure transitions are a daemon-side concern (the daemon
    handles merge conflicts with a rebase + re-push loop, not via
    phase transitions). If merge_succeeded is False, this function
    returns ``UNRECOGNIZED`` to force the caller to handle the
    failure path explicitly rather than silently advancing.
    """
    if not merge_succeeded:
        return PhaseTransition(
            action=TransitionAction.UNRECOGNIZED,
            reason="merge failed — caller must handle rebase/retry",
        )
    # Successful merge: status flips to succeeded (per #2953); next
    # phase is awaiting_deploy.
    return PhaseTransition(
        action=TransitionAction.ADVANCE_WITH_STATUS,
        next_phase=PHASE_AWAITING_DEPLOY,
        terminal_status=AgentStatus.SUCCEEDED.value,
        reason="merge succeeded" + (" (self-deploy)" if is_self_deploy else ""),
        context={"is_self_deploy": is_self_deploy},
    )


def transition_from_awaiting_deploy(
    deploy_succeeded: bool,
    *,
    is_self_deploy: bool = False,
) -> PhaseTransition:
    """Return the phase transition after a deploy-watch poll.

    ``deploy_succeeded`` is ``True`` when the deploy-dispatcher
    workflow run completed and its conclusion is ``success``.
    ``is_self_deploy`` matches the flag set at merge-time; when
    True, verify is skipped entirely and the agent advances straight
    to ``done``.

    A failed deploy today routes through ``_handle_agent_failure``
    with category ``deploy_failed`` (daemon.py). We surface the same
    intent via ``ROUTE_TO_DIAGNOSER``.
    """
    if not deploy_succeeded:
        return PhaseTransition(
            action=TransitionAction.ROUTE_TO_DIAGNOSER,
            failure_hint="deploy_failed",
            reason="deploy-dispatcher workflow did not succeed",
        )
    if is_self_deploy:
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_DONE,
            reason="self-deploy: skipping verify",
            context={"verify_skip_reason": "self_deploy"},
        )
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_VERIFY,
        reason="deploy succeeded",
    )


def transition_from_verify(output: Mapping[str, Any] | None) -> PhaseTransition:
    """Return the phase transition after ``/task-v2-verify`` runs.

    Verify verdicts:

    * ``VERIFIED`` / ``SKIPPED`` — stamp verified_at + advance to
      ``done``. Status stays ``succeeded`` (set at merge-time).
    * ``FAILED`` — genuine regression signal; route to diagnoser.
    """
    verdict = _verdict(output)
    if verdict == VERDICT_FAILED:
        return PhaseTransition(
            action=TransitionAction.ROUTE_TO_DIAGNOSER,
            failure_hint=FAILURE_HINT_VERIFY_FAILED_POST_MERGE,
            reason="verify FAILED post-merge",
            context={
                "failure_reason": (output or {}).get("failure_reason"),
                "evidence_md": (output or {}).get("evidence_md"),
            },
        )
    # VERIFIED, SKIPPED, or missing verdict -> advance to done.
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_DONE,
        reason=f"verify {verdict or '(missing verdict treated as VERIFIED)'}",
    )


def transition_from_retro(retro_succeeded: bool) -> PhaseTransition:
    """Return the phase transition after ``/task-v2-retro`` runs.

    Retro is post-success bookkeeping. Both success and failure of
    the retro skill itself leave the agent's success status intact;
    only the phase differs. Cleanup runs from either terminal.
    """
    if retro_succeeded:
        return PhaseTransition(
            action=TransitionAction.ADVANCE,
            next_phase=PHASE_RETRO_DONE,
            reason="retro succeeded",
        )
    return PhaseTransition(
        action=TransitionAction.ADVANCE,
        next_phase=PHASE_RETRO_FAILED,
        reason="retro failed",
    )


# ---------------------------------------------------------------------------
# CI-rollup classifier — pure function; accepts a ``gh pr view --json``
# rollup payload and returns "green" / "red" / "pending".
# ---------------------------------------------------------------------------

#: Check-run conclusions that count as red.
_CI_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {
        "FAILURE",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
    }
)

#: Check-run conclusions that count as green.
_CI_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})


def _ci_rollup_state(pr_status: Mapping[str, Any] | None) -> str:
    """Classify a ``gh pr view --json`` rollup as green / red / pending.

    Mirrors the rollup parsing logic embedded in
    ``_advance_awaiting_ci`` (daemon.py lines 10774+). Extracted so
    the agent-runner can use it identically without duplicating the
    check-run conclusion constants.

    Rules (short-circuit ordering):

    1. If any check has a RED conclusion (FAILURE / CANCELLED /
       TIMED_OUT / ACTION_REQUIRED / STARTUP_FAILURE) → red.
    2. If any check is not yet COMPLETED (status in_progress /
       queued / pending) → pending.
    3. If mergeable is not ``MERGEABLE`` or mergeStateStatus is not
       ``CLEAN`` → pending (the merge-conflict polling path; we
       treat it as "not ready yet" rather than red).
    4. Otherwise → green.
    """
    if not pr_status:
        return "pending"

    rollup = pr_status.get("statusCheckRollup") or []
    if not isinstance(rollup, list):
        return "pending"

    any_pending = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if status == "COMPLETED":
            if conclusion in _CI_FAILURE_CONCLUSIONS:
                return "red"
            if conclusion in _CI_SUCCESS_CONCLUSIONS:
                continue
            # COMPLETED with an unrecognized conclusion — treat as
            # red rather than silently green.
            return "red"
        # Not COMPLETED → still pending.
        any_pending = True

    if any_pending:
        return "pending"

    mergeable = str(pr_status.get("mergeable") or "").upper()
    merge_state = str(pr_status.get("mergeStateStatus") or "").upper()
    if mergeable != "MERGEABLE" or merge_state != "CLEAN":
        return "pending"
    return "green"


# ---------------------------------------------------------------------------
# Phase-loop driver helper — small convenience for callers (primarily
# the agent-runner entrypoint, #3086 Stage 1b) that want to ask "given
# the current phase and this phase's output, what's the transition?"
# without a 9-way if-elif ladder at the call site.
# ---------------------------------------------------------------------------

#: Mapping of phase → transition function, where the transition
#: function takes a single ``output`` argument (the phase's output
#: JSON). Phases with a different signature (awaiting_ci, merge,
#: awaiting_deploy, retro) are NOT in this table — callers handle them
#: explicitly because the input shape differs.
_VERDICT_DRIVEN_TRANSITIONS: dict[
    str,
    "callable",  # type: ignore[valid-type]
] = {
    PHASE_PLANNING: transition_from_plan,
    PHASE_RALPH: transition_from_ralph,
    PHASE_SUMMARY: transition_from_summary,
    PHASE_PUSH_AND_PR: transition_from_push_and_pr,
    PHASE_FIX_CI: transition_from_fix_ci,
    PHASE_VERIFY: transition_from_verify,
}


def next_phase_from_verdict(
    current_phase: str,
    output: Mapping[str, Any] | None,
) -> PhaseTransition:
    """Return the transition for a verdict-driven phase.

    Dispatcher convenience — looks up ``current_phase`` in the
    verdict-driven table and calls the matching transition function.
    Raises ``KeyError`` for phases that require a non-output signal
    (``awaiting_ci`` needs a PR status poll; ``merge`` needs a
    subprocess exit code; ``awaiting_deploy`` needs a workflow-run
    result; ``retro`` needs a subprocess exit code). Callers use the
    phase-specific function directly in those cases.

    For unknown phases, returns an ``UNRECOGNIZED`` transition so the
    caller can log + crash-the-agent rather than silently advancing.
    """
    handler = _VERDICT_DRIVEN_TRANSITIONS.get(current_phase)
    if handler is None:
        return PhaseTransition(
            action=TransitionAction.UNRECOGNIZED,
            reason=f"no verdict-driven transition for phase {current_phase!r}",
        )
    return handler(output)


def is_terminal_phase(phase: str) -> bool:
    """Return True when ``phase`` is a terminal (stop advancing) phase.

    Thin wrapper around the :data:`TERMINAL_PHASES` frozenset; exists
    so callers don't have to import the frozenset name and can use a
    one-line predicate in their while-loops.
    """
    return phase in TERMINAL_PHASES


def is_terminal_status(status: str) -> bool:
    """Return True when ``status`` is a terminal (stop advancing) status.

    Mirrors daemon.py's ``TERMINAL_AGENT_STATUSES`` frozenset. Thin
    wrapper for readability in agent-runner loops.
    """
    return status in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Forward-flow validator. The agent-runner will use this to sanity-check
# that the phase it just received from the DB is recognized; a stray
# typo in a migration or an operator UPDATE would surface here.
# ---------------------------------------------------------------------------


#: All active (non-terminal) phases this state machine knows about, in
#: daemon-internal naming. Distinct from
#: :data:`scripts.dispatcher.phases.PHASE_FLOW_FORWARD` which uses the
#: admin-UI naming convention (``ci_watch`` vs ``awaiting_ci``,
#: ``deploy_watch`` vs ``awaiting_deploy``). The daemon writes the
#: values below to ``dispatcher.agents.phase``; the admin UI module
#: re-labels them for display. A followup issue should reconcile the
#: two — today we carry both.
ACTIVE_PHASES: frozenset[str] = frozenset(
    {
        PHASE_CLAIMING,
        PHASE_PLANNING,
        PHASE_SETUP,
        PHASE_RALPH,
        PHASE_SUMMARY,
        PHASE_PUSH_AND_PR,
        PHASE_AWAITING_CI,
        PHASE_FIX_CI,
        PHASE_MERGE,
        PHASE_AWAITING_DEPLOY,
        PHASE_VERIFY,
        PHASE_RETRO,
    }
)


def is_known_phase(phase: str) -> bool:
    """Return True when ``phase`` is a recognized phase name.

    Recognized = either an active phase (in :data:`ACTIVE_PHASES`)
    or a terminal phase (in :data:`TERMINAL_PHASES`).
    """
    return phase in ACTIVE_PHASES or phase in TERMINAL_PHASES


__all__ = [
    # Phase constants
    "PHASE_CLAIMING",
    "PHASE_PLANNING",
    "PHASE_SETUP",
    "PHASE_RALPH",
    "PHASE_SUMMARY",
    "PHASE_PUSH_AND_PR",
    "PHASE_AWAITING_CI",
    "PHASE_FIX_CI",
    "PHASE_MERGE",
    "PHASE_AWAITING_DEPLOY",
    "PHASE_VERIFY",
    "PHASE_RETRO",
    "PHASE_DONE",
    "PHASE_RETRO_DONE",
    "PHASE_RETRO_FAILED",
    "PHASE_NO_OP",
    "PHASE_CLEANUP_DONE",
    "PHASE_CLEANUP_BLOCKED",
    "PHASE_PLAN_BLOCKED",
    "PHASE_FORCE_STOPPED",
    "PHASE_DAEMON_RESTART_ABANDONED",
    "PHASE_PAUSED_BY_KILLSWITCH",
    # Verdict constants
    "VERDICT_SHIP",
    "VERDICT_AC_INFEASIBLE",
    "VERDICT_PATCHED",
    "VERDICT_FLAKY",
    "VERDICT_BLOCKED",
    "VERDICT_FAILED",
    "VERDICT_VERIFIED",
    "VERDICT_SKIPPED",
    # Status
    "AgentStatus",
    "TERMINAL_STATUSES",
    "TERMINAL_PHASES",
    "ACTIVE_PHASES",
    # Failure hints
    "FAILURE_HINT_RALPH_NOT_SHIP",
    "FAILURE_HINT_RALPH_AC_INFEASIBLE",
    "FAILURE_HINT_SUMMARY_AC_INFEASIBLE",
    "FAILURE_HINT_FIX_CI_BLOCKED",
    "FAILURE_HINT_VERIFY_FAILED_POST_MERGE",
    "FAILURE_HINT_PLAN_BLOCKED",
    # Transition dataclass + enum
    "PhaseTransition",
    "TransitionAction",
    # Per-phase transition functions
    "transition_from_plan",
    "transition_from_ralph",
    "transition_from_summary",
    "transition_from_push_and_pr",
    "transition_from_awaiting_ci",
    "transition_from_fix_ci",
    "transition_from_merge",
    "transition_from_awaiting_deploy",
    "transition_from_verify",
    "transition_from_retro",
    # Convenience helpers
    "next_phase_from_verdict",
    "is_terminal_phase",
    "is_terminal_status",
    "is_known_phase",
]
