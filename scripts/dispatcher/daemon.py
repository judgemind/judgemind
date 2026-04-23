#!/usr/bin/env python3
"""Dispatcher v2 daemon entrypoint — Phase 2 shadow mode + Phase 3A wiring.

Long-running process that reads ``dispatcher.*`` state, observes the
GitHub ``agent/ready`` queue, writes snapshots to
``dispatcher.queue_snapshots``, and emits heartbeat metrics to
CloudWatch.

Phase 2 ran in shadow mode (``concurrency_cap=0``) with no subprocess
spawning. **Phase 3 sub-task A (#2783) wires the happy-path
orchestration** — claim an ``agent/ready`` issue, create a worktree,
spawn ``/task-v2-plan`` → ``/task-v2-ralph`` → ``/task-v2-summary`` as
``claude -p`` subprocesses, then ``git push`` + ``gh pr create``. The
orchestration path stays **cold** in production (``concurrency_cap`` is
still 0 until Phase 3E flips it to 1) but the code is exercised by
unit tests in ``scripts/dispatcher/tests/test_daemon_phase3a.py``.

Post-PR work (CI watch, fix-ci, merge, deploy watch, verify, retro,
retry markers, diagnoser) is deliberately out of scope for 3A — those
belong to 3B-3E.

Spec: ``docs/specs/dispatcher-v2-spec.md`` §6 (scheduler loop), §6a
(per-phase skill contracts), §7 (supervisor loop), §14 (deployment),
§15 (Phase 2 definition + gate), §17 Risk 2 (double-daemon race),
§17 Risk 4a (subprocess timeout), §18 (schema DDL). Issues #2768
(Phase 2), #2783 (Phase 3A).

Structured logging is JSON-per-line to stdout so CloudWatch Logs
Insights can query it without a regex layer.

Usage::

    DATABASE_URL=postgres://... python -m scripts.dispatcher.daemon \\
        --tick-scheduler-seconds 30 \\
        --tick-supervisor-seconds 120

Env vars read:
    DATABASE_URL                — Postgres connection string (required).
    GIT_SHA                     — short SHA baked into the image at build
                                  time (optional; falls back to ``unknown``).
    HOSTNAME                    — Fargate task ID lands here by default.
                                  Falls back to ``socket.gethostname()``.
    GITHUB_TOKEN                — scoped PAT (spike 0.7) used by the
                                  ``gh`` CLI in the queue-scan path. Wired
                                  into the container via ``secrets[]`` by
                                  the dispatcher-daemon module (#2700).
    GITHUB_REPO                 — owner/name the daemon watches
                                  (default ``judgemind/judgemind``).
    DISPATCHER_SERVICE_NAME     — used as the ``Service`` dimension on the
                                  ``HeartbeatAge`` CloudWatch metric.
    HEARTBEAT_METRIC_NAMESPACE  — CloudWatch metric namespace (default
                                  ``Judgemind/Dispatcher``).
    AWS_REGION / AWS_DEFAULT_REGION — boto3 region for the CloudWatch
                                  client (falls back to ``us-west-2``).

Exit codes:
    0  normal shutdown (SIGTERM / SIGINT).
    1  unrecoverable startup error (DB unreachable, lease contention).
    2  argparse failure.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — types only
    from psycopg import Connection


# The stream forwarder lives in a sibling module so tests can exercise
# it without pulling in the daemon's DB + GitHub dependencies. See
# ``scripts/dispatcher/stream_forwarder.py`` + issue #3017. Imported at
# module scope so monkeypatching
# (``dispatcher.daemon.stream_subprocess_output_async``) in tests is
# straightforward.
#
# Uses a relative import so the module works under both import styles:
# ``python -m scripts.dispatcher.daemon`` (Fargate production) AND
# ``from dispatcher import daemon`` after tests put ``scripts/`` on
# ``sys.path``. Both invocations load daemon.py as part of a
# ``dispatcher`` package, so ``from .stream_forwarder import ...``
# resolves in both. A hard-coded absolute import would require a
# try/except fallback that the
# ``scripts/check-dispatcher-image-deps.py`` CI guard flags as a
# missing pip dep (the fallback top-level ``dispatcher`` is not a pip
# package — it is a sibling module).
from .stream_forwarder import stream_subprocess_output_async  # noqa: E402


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Postgres schema that owns every table the daemon reads or writes.
DISPATCHER_SCHEMA = "dispatcher"

#: Lease window — another running daemon whose heartbeat is newer than
#: this many seconds blocks this one from spawning (§17 Risk 2). 60 s is
#: 2× the scheduler tick, so a healthy peer is always seen as active.
LEASE_HEARTBEAT_WINDOW_SECONDS = 60

#: Default tick cadence. Mirrors §6 / §7 of the spec.
DEFAULT_SCHEDULER_TICK_SECONDS = 30
DEFAULT_SUPERVISOR_TICK_SECONDS = 120

#: Default housekeeping tick cadence — hourly. Cheap DELETEs that prune
#: transient dispatcher tables. Separate from scheduler/supervisor so a
#: slow DELETE (or DB hiccup) does not delay the hot-path queue scan or
#: heartbeat. Issue #2778 (closes the TODO in migration 24).
DEFAULT_HOUSEKEEPING_TICK_SECONDS = 3600

#: Default retention window for ``dispatcher.queue_snapshots``. Matches
#: the ``dispatcher_daemon`` CloudWatch log group default retention and
#: covers the longest plausible multi-week post-mortem window. Overridable
#: per-environment via the ``queue_snapshot_retention_days`` row in
#: ``dispatcher.config`` (operator knob — no redeploy needed).
DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS = 30

#: Default retention window for ``dispatcher.phase_outputs``. Matches the
#: queue-snapshot default so every dispatcher housekeeping target shares
#: the same cutoff. Overridable via the ``phase_output_retention_days``
#: row in ``dispatcher.config``. Issue #2779.
DEFAULT_PHASE_OUTPUT_RETENTION_DAYS = 30

#: Default retention window for ``dispatcher.notifications``. Matches the
#: queue-snapshot default. Overridable via the
#: ``notification_retention_days`` row in ``dispatcher.config``. Issue #2779.
DEFAULT_NOTIFICATION_RETENTION_DAYS = 30

#: Default retention window for ``dispatcher.ralph_patches``. Safety-net
#: only — the happy-path cleanup is the ``DELETE`` at ``gh pr create``
#: success in :meth:`_push_and_open_pr`. Seven days catches pathological
#: cases (daemon crash between SHIP and PR create, operator force-stop)
#: without keeping large patch blobs indefinitely. Not operator-tunable
#: today — if that changes, add a ``ralph_patch_retention_days`` row in
#: ``dispatcher.config`` and thread it through ``_read_retention_days``
#: the same way the other targets do. Issue #3012.
DEFAULT_RALPH_PATCH_RETENTION_DAYS = 7

#: Default repo the daemon watches. Overridden by the ``GITHUB_REPO``
#: env var, wired in the terraform module.
DEFAULT_GITHUB_REPO = "judgemind/judgemind"

#: Queue-scan defaults. The ``gh issue list`` page size is capped at 200;
#: Phase 2's ``agent/ready`` queue is typically < 50, so one page is plenty.
QUEUE_SCAN_PAGE_LIMIT = 200

#: Hard timeout on the ``gh issue list`` subprocess. Short enough to avoid
#: leaking scheduler ticks if GitHub is slow; long enough that a normal
#: call (<2s) always finishes. The scheduler tick defaults to 30s, so
#: anything over ~10s here would conflict with the next tick's own call.
QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS = 10

#: How often the blocked-list scan runs, expressed as scheduler ticks.
#: With the 30s scheduler tick default, 4 ticks = 120s ≈ 2 minutes. The
#: blocked list changes slowly (new blockers added when PRs gate work;
#: removed by the ``unblock-issues`` workflow), so a per-tick scan would
#: waste GitHub budget. Issue #2820. Scheduler tick counts are 1-based
#: after the first tick, so tick_n % N == 0 fires on ticks 4, 8, 12…
#: Tick 1 (daemon boot) always runs the blocked scan so the admin page
#: has a populated blocked panel immediately; every subsequent scan
#: follows the modulo cadence.
BLOCKED_SCAN_EVERY_N_TICKS = 4

#: Max rows fetched by the blocked-list scan. The ``status/blocked``
#: queue is typically < 30 but the upper bound comes from operator
#: behaviour (bulk blocker spike) not normal workflow. Matches
#: ``QUEUE_SCAN_PAGE_LIMIT`` so a single ``gh`` call always suffices.
BLOCKED_SCAN_PAGE_LIMIT = 200

#: Default CloudWatch metric namespace. Matches the terraform module's
#: ``task_put_metric`` policy condition
#: (``cloudwatch:namespace = Judgemind/Dispatcher``).
DEFAULT_HEARTBEAT_METRIC_NAMESPACE = "Judgemind/Dispatcher"

#: Default AWS region when neither ``AWS_REGION`` nor ``AWS_DEFAULT_REGION``
#: is set in the task environment. Matches the dispatcher deployment region
#: (us-west-2).
DEFAULT_AWS_REGION = "us-west-2"

#: Phase 2 spawn-safety invariant: this value must be 0. The daemon
#: asserts this on every scheduler tick and logs a warning if the live
#: ``dispatcher.config.concurrency_cap`` is anything else. The actual
#: spawn path was added in Phase 3A (#2783) but remains gated on
#: ``concurrency_cap > 0`` — until Phase 3E flips it to 1, the
#: orchestration path stays cold in production and the Phase 2 guard
#: continues to warn on any non-zero value.
PHASE_2_REQUIRED_CONCURRENCY_CAP = 0

#: Killswitch terminal phase name (#2847). When the scheduler observes
#: ``concurrency_cap=0`` mid-orchestration and the killswitch engages
#: (not a #2884 graceful ``stop``), the worker thread aborts at the
#: next phase boundary and ``_mark_agent_terminal`` is called with
#: ``status='failed'`` + this phase name. The dedicated phase value
#: distinguishes operator killswitches from real failures in retro /
#: admin-page views and stops them from being mis-classified as
#: tier-1/2/3 failures by the diagnoser.
KILLSWITCH_TERMINAL_PHASE = "paused_by_killswitch"

#: #2884 terminal phase name for explicit operator ``force_stop``
#: commands. Distinct from ``paused_by_killswitch`` (which covers
#: cap=0 observed for any reason, including a direct config edit) so
#: the admin cockpit / retro can tell "operator fired the emergency
#: kill button" apart from "cap observed as 0 for some other reason".
#: Used by :meth:`DispatcherDaemon._check_killswitch_and_abort` when
#: :attr:`DispatcherDaemon._force_stop_requested` is set.
FORCE_STOP_TERMINAL_PHASE = "force_stopped"

#: Maximum time :meth:`DispatcherDaemon.run_forever` waits for the
#: orchestration worker thread to exit during shutdown (#2847). If the
#: thread is mid-``subprocess.run`` for a ``claude -p`` phase, we cannot
#: interrupt the subprocess cleanly — the thread will return only once
#: the subprocess exits (or is killed by Fargate's SIGKILL fallback on
#: task stop). This wait is a best-effort join; on timeout the daemon
#: proceeds with ``mark_stopped`` anyway so the process does not hang.
ORCHESTRATION_JOIN_TIMEOUT_SECONDS = 10

#: Hard wall-clock timeout for each ``claude -p`` subprocess spawned
#: from the orchestration path. Matches the 180-minute ceiling from
#: spec §17 Risk 4a and the ``dispatcher.config.subprocess_timeout_s``
#: seed value (10800s). Enforced via ``subprocess.run(..., timeout=...)``.
CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS = 180 * 60

#: Character cap on the ``stderr_tail`` field attached to
#: ``daemon.subprocess_failed`` structured events. Previously 500; raised
#: to 2000 (#2821) because the tail-oriented truncation loses signal for
#: noisy failures where the useful "failed: X" line appears early — e.g.
#: ralph running 1000 tests plus one real error, or terraform plan noise
#: plus one actual error. 2000 covers most real failure modes without
#: paying the cost of a full-log embed in the primary event. The full
#: log is still durably captured via :data:`PHASE_FAILURE_LOG_MAX_CHARS`
#: in the secondary ``phase_failure_log`` event and via
#: ``dispatcher.phase_outputs.log_text``.
PHASE_STDERR_TAIL_MAX_CHARS = 2000

#: Character cap on the ``stderr_tail`` field attached to structured-log
#: events that fire on the **rare error path** — e.g. ``git_push_failed``.
#: Larger than :data:`PHASE_STDERR_TAIL_MAX_CHARS` because these events
#: are emitted at most once per failure and the extra context is worth
#: the CloudWatch log-volume cost when the failure is unusual (network
#: hiccup, pre-push hook output, transient auth errors). Issue #2902.
STRUCTURED_LOG_STDERR_MAX = 4000

#: Character cap on the ``stderr_preview`` field attached to
#: ``daemon.phase_output_missing`` and retro-failure structured events.
#: Previously 200; raised to 2000 (#2821) for the same rationale as
#: :data:`PHASE_STDERR_TAIL_MAX_CHARS`. Unlike ``stderr_tail``, the
#: ``stderr_preview`` flows through
#: :meth:`DispatcherDaemon._extract_log_preview` which additionally
#: ``strip()``s the content, so the effective cap is still 2000 post-strip.
PHASE_STDERR_PREVIEW_MAX_CHARS = 2000

#: Character cap on the ``log_body`` field in the secondary
#: ``daemon.phase_failure_log`` CloudWatch event emitted alongside each
#: ``daemon.subprocess_failed`` (#2821). CloudWatch Logs allows up to
#: 256KB per event but 10k is plenty for triage in practice. The primary
#: failure event stays keyed on a small ``stderr_tail`` so common filter
#: queries don't double-scan the full log body; the secondary event is
#: the "full context when a human clicks in" payload. Both events share
#: ``agent_id`` so ``aws logs filter-log-events --filter-pattern
#: '<agent_id>'`` returns the pair.
PHASE_FAILURE_LOG_MAX_CHARS = 10000

#: Per-phase ``--max-turns`` values. Issue #2885 bumped every phase by
#: 10× on the operator directive "stop being parsimonious on these
#: limits; cost is not the constraint, success is." Two overnight
#: failure modes forced the bump:
#:
#: 1. Plan agents hit ``error_max_turns`` at 51/50 on complex scraper
#:    issues (#2564, #2565), burning ~$3 of opus with zero output then
#:    retrying — legitimate exploration on a large codebase just
#:    doesn't fit in 50 turns.
#: 2. Ralph's old 500 cap was adequate for typical work but left no
#:    slack for genuinely hard problems where the worker-reviewer loop
#:    runs long; on Max-plan billing the extra turns are effectively
#:    free and a successful ralph iteration beats a failed retry at
#:    any turn count.
#:
#: Original values (#2787 Phase 3B + #2798 Phase 3E) were calibrated
#: conservatively against observed medians; the new values are 10×
#: headroom above that calibration. Post-PR phases (fix-ci, verify)
#: and retro also bumped 10× for consistency; none of them had been
#: hitting the old cap but the parsimony was left over from when
#: every turn mattered.
PHASE_MAX_TURNS = {
    "plan": 500,
    "ralph": 5000,
    "summary": 300,
    "fix-ci": 1000,
    "verify": 500,
    "retro": 300,
}

#: Per-phase ``--model`` values. Matches ``dispatcher.config.model_by_phase``
#: seeded in migration 21. Fix-ci uses Sonnet — the CI-fixing skill's
#: own frontmatter selects it. Verify uses Haiku — it only reads the
#: deploy status and poses structured evidence, no complex reasoning.
#: Retro uses Haiku — a quick review of structured input that produces
#: structured output; no complex reasoning required.
#:
#: .. note::
#:    Ralph is **attempt-aware** (#2955). The value recorded here is the
#:    default used by attempts 1..``MAX_RETRY_ATTEMPTS-1``; the final
#:    budgeted attempt is upgraded to Opus by
#:    :func:`_ralph_model_for_attempt`, called from
#:    :meth:`DispatcherDaemon._spawn_phase_subprocess`. The admin UI,
#:    metering (``phase_outputs.model_used``), and CloudWatch logs all
#:    surface the *actually-used* model so operators can retroactively
#:    separate Sonnet-ralph from Opus-ralph runs.
PHASE_MODELS = {
    "plan": "opus",
    "ralph": "sonnet",
    "summary": "haiku",
    "fix-ci": "sonnet",
    "verify": "haiku",
    "retro": "haiku",
}


def _ralph_model_for_attempt(attempt_n: int) -> str:
    """Return the ``--model`` value for a ralph attempt (#2955).

    ``attempt_n`` is 1-indexed: the first run is attempt 1 and the
    final budgeted retry is :data:`MAX_RETRY_ATTEMPTS`. On the final
    attempt the model is upgraded from ``sonnet`` to ``opus`` — if
    Sonnet has already failed ``MAX_RETRY_ATTEMPTS-1`` times on the
    same issue, burning the last attempt on the same model yields
    the same result. Opus on the final attempt gives the queue a
    real shot at unblocking before the agent is given up on.

    Only ralph is upgraded (plan already uses Opus; summary, fix-ci,
    verify, and retro keep their static defaults). The "final attempt"
    calculation counts only budgeted retries — infra-preemption
    retries preserve ``retries_used`` via
    :meth:`DispatcherDaemon._process_retry_markers`, so an agent that
    caught two dispatcher redeploys mid-ralph does not skip straight
    to Opus on its next real spawn (issue #2936 + #2955).

    Defensive: attempts numerically past :data:`MAX_RETRY_ATTEMPTS`
    stay on Opus rather than silently downgrading. The 3-attempt cap
    itself is enforced by :meth:`_create_retry_marker`.
    """
    if attempt_n >= MAX_RETRY_ATTEMPTS:
        return "opus"
    return PHASE_MODELS["ralph"]


#: The three happy-path phases 3A orchestrates, in execution order.
#: Post-PR phases (``fix_ci``, ``verify``, ``retro``) are 3B-3E scope.
PHASE_SEQUENCE = ("plan", "ralph", "summary")

#: Maximum ``dispatcher.agents.retries_used`` before a fix-ci failure
#: stops retrying and marks the agent failed. 3 matches spec §8 which
#: also gates the diagnoser (``ci_red_after_retries``). After this
#: ceiling the 3C/3D diagnoser escalation takes over.
FIX_CI_MAX_RETRIES = 3

#: Deploy workflow names we watch after a merge. If the merged commit
#: triggers none of these (e.g. docs-only PR), the agent advances
#: straight to verify with a "no deploy applicable" signal. Must match
#: the ``name:`` field on each deploy workflow in ``.github/workflows/``.
DEPLOY_WORKFLOW_NAMES = frozenset(
    {
        "Deploy API",
        "Deploy Dispatcher",
        "Deploy Scraper",
        "Deploy Production",
        "Deploy Production (Web)",
        "Terraform",
    }
)

#: Hard timeout on the ``gh pr view`` / ``gh run list`` / ``gh run view``
#: / ``gh pr merge`` / ``gh issue comment`` subprocess calls used by
#: ``_advance_running_agents``. Short enough to avoid leaking a
#: supervisor tick (120s default) if GitHub is slow, long enough that
#: a normal call (<3s) always finishes.
GH_POLL_SUBPROCESS_TIMEOUT_SECONDS = 15

#: Cap on how many ``failing_jobs`` entries we hand the fix-ci skill.
#: Ten is already an unusually bad CI day and keeps the JSON payload
#: bounded so the skill's context stays small.
FIX_CI_MAX_FAILING_JOBS = 10

#: Character cap per failing-job log tail handed to fix-ci. ~200 lines
#: of CI output is typically well under this.
FIX_CI_LOG_TAIL_MAX_CHARS = 20000

#: Statuses that represent an in-flight claim or a prior-run row whose
#: PR is still open / awaiting operator action. Mirrors the partial
#: UNIQUE INDEX predicate in migration 25 plus two DB-only extensions
#: (``succeeded``, ``needs_review``) the picker uses to avoid
#: double-processing an issue whose artifact (PR) has not yet been
#: merged-and-cleaned.
#:
#: * ``running`` / ``retrying`` — uniqueness enforced in DB.
#: * ``succeeded`` — prior successful run whose PR has not yet been
#:   merged + cleaned. Added client-side here so we don't re-pick
#:   during the merge window.
#: * ``needs_review`` (#2856) — prior run that produced a DRAFT PR
#:   awaiting operator decision. Same logic: don't re-pick the issue
#:   while the draft PR is open, or two agent branches collide on the
#:   same issue. Operator either merges (→ PR close auto-unblocks),
#:   closes (→ dispatcher housekeeping reclaims), or keeps editing —
#:   all three paths eventually cycle the row out of this set.
ACTIVE_AGENT_STATUSES = ("running", "retrying", "succeeded", "needs_review")

#: GitHub label added to an issue on claim (by either the daemon or the
#: /task skill) and removed on terminal. Gives operators a GitHub-visible
#: "this issue is being worked on right now — don't dispatch another
#: agent on it" signal without querying ``dispatcher.agents``. Paired
#: with the DB-side partial UNIQUE INDEX (#2783) and the /task-side row
#: insert (#2866) for defense-in-depth against subagent↔daemon claim
#: collisions. The queue-scan filter in :meth:`_fetch_agent_ready_issues`
#: also excludes this label as a redundant safety — the DB row is the
#: atomic interlock; the label is human + queue-scan visible.
STATUS_IN_PROGRESS_LABEL = "status/in-progress"

#: ``dispatcher.agents.kind`` value used by the ``/task`` skill's claim
#: rows (issue #2866). Distinct from the daemon's default ``'task'``
#: value so :meth:`_atomic_claim`'s UniqueViolation handler can emit a
#: distinguishing ``already_claimed_by_task`` structured log — useful
#: for identifying subagent↔daemon races in CloudWatch Logs Insights.
TASK_SKILL_KIND = "task-skill"

#: Sentinel HTML comment embedded as line 1 of the automated
#: "plan returned go=false" issue comment. Lets the daemon detect that
#: the comment has already been posted and skip re-posting on a retry
#: of the plan_blocked handler (idempotence — see issue #2857).
#: HTML comments survive GitHub's markdown rendering pipeline and are
#: visible in ``gh issue view --json comments`` output.
PLAN_BLOCKED_COMMENT_SENTINEL = "<!-- dispatcher-plan-blocked -->"

#: Timeout for the ``gh issue view``/``gh issue comment``/``gh issue edit``
#: subprocess calls used by the plan_blocked handler. Tight enough that
#: a hung GitHub request can't stall the scheduler tick for multiple
#: minutes; loose enough that a normal call (<3s) always finishes.
PLAN_BLOCKED_GH_SUBPROCESS_TIMEOUT_SECONDS = 30

#: Sentinel HTML comment embedded as line 1 of the automated
#: "summary flagged unmet acceptance criteria" issue comment. Issue
#: #2856. Lets the daemon detect that the comment has already been
#: posted and skip re-posting on a retry of the needs_review handler
#: (idempotence). HTML comments survive GitHub's markdown rendering
#: pipeline and are visible in ``gh issue view --json comments`` output.
#: Parallel naming with :data:`PLAN_BLOCKED_COMMENT_SENTINEL` (#2857) —
#: ``dispatcher.agents.status`` is free-text so the two correct-outcome
#: terminals live alongside each other without a schema migration.
NEEDS_REVIEW_COMMENT_SENTINEL = "<!-- dispatcher-needs-review -->"

#: Timeout for the ``gh issue view``/``gh issue comment`` subprocess
#: calls used by the needs_review handler. Mirrors
#: :data:`PLAN_BLOCKED_GH_SUBPROCESS_TIMEOUT_SECONDS` — same tradeoffs
#: (don't stall scheduler ticks on GitHub slowdowns, but don't
#: false-positive a normal <3s call).
NEEDS_REVIEW_GH_SUBPROCESS_TIMEOUT_SECONDS = 30

#: Relative path under the repo root where per-agent worktrees land in
#: the local-dev fallback mode (``baseline_repo_root`` unset). Mirrors
#: the laptop-dispatcher convention from ``.claude/skills/task/SKILL.md``
#: so human operators can find a daemon-created worktree with the same
#: ``git worktree list`` command they use locally. When the Fargate
#: container sets ``DISPATCHER_BASELINE_REPO_ROOT`` (see
#: :data:`DEFAULT_BASELINE_REPO_ROOT`), worktrees are placed under
#: ``<baseline_repo_root>.parent / "worktrees" / agent-<uuid>`` instead
#: and this constant is unused.
WORKTREE_PARENT_DIR = Path(".claude/worktrees")

#: Length of the short UUID used in worktree + branch names. 8 hex
#: chars is ~4 billion distinct values — collision probability across
#: the lifetime of the dispatcher is negligible and the short form
#: keeps path lengths sane.
AGENT_SHORT_ID_HEX_CHARS = 8

#: Default absolute path for the daemon's baseline git clone inside the
#: Fargate container. When ``DISPATCHER_BASELINE_REPO_ROOT`` is set in
#: the environment (the Dockerfile wires this to the value below), the
#: daemon clones ``judgemind/judgemind`` here at boot via
#: :meth:`DispatcherDaemon.ensure_baseline_clone` and runs
#: ``git -C <baseline> worktree add <abs_path>`` from it so worktree
#: creation no longer depends on the container CWD having a ``.git``
#: directory (issue #2804). Per-agent worktrees live in the sibling
#: ``worktrees/`` directory so the baseline clone itself is never mixed
#: with per-agent state. Fargate ephemeral storage is 50 GiB per task
#: and the baseline shallow clone is ~100 MB; the combined footprint is
#: well under the quota even at concurrency_cap=5.
DEFAULT_BASELINE_REPO_ROOT = "/var/lib/dispatcher/repo"

#: Authenticated HTTPS URL used by :meth:`DispatcherDaemon.ensure_baseline_clone`
#: when the baseline clone is missing. The daemon runs ``gh auth setup-git``
#: at boot so the GITHUB_TOKEN secret (scoped PAT from spike 0.7 / #2700)
#: is consulted by git's credential helper automatically. The repo URL
#: hard-codes ``judgemind/judgemind`` — the daemon is only ever
#: responsible for that repo, and reading it from ``GITHUB_REPO`` would
#: let a future test-env mis-config clone a different repo into the
#: baseline path.
BASELINE_CLONE_URL = "https://github.com/judgemind/judgemind.git"

#: Hard wall-clock timeout on the boot-time ``git clone``. 300s is
#: generous even on a slow CI-to-GitHub link for a shallow (~100 MB)
#: clone; the daemon aborts startup on timeout (exit 1) so the ECS
#: task restart loop provides a natural retry.
BASELINE_CLONE_TIMEOUT_SECONDS = 300

#: Hard wall-clock timeout on the ``git fetch origin main`` we run
#: every time a baseline clone is reused (either at boot or just
#: before ``git worktree add``). Much shorter than the initial clone
#: because the delta since the last fetch is small.
BASELINE_FETCH_TIMEOUT_SECONDS = 120

#: Hard wall-clock timeout on the one-shot ``gh auth setup-git`` call
#: at daemon boot. The subprocess just writes a credential-helper line
#: to the container's git config — it is a local operation and should
#: finish in milliseconds.
GH_AUTH_SETUP_GIT_TIMEOUT_SECONDS = 10

#: Hard wall-clock timeout on the daemon-side ``git push`` subprocess
#: calls (post-summary push in ``_advance_awaiting_summary`` and the
#: fix-ci retry push in ``_advance_awaiting_ci``). 120s was too tight:
#: the pre-push hook runs each touched package's full test suite,
#: which for multi-package changes (e.g. scraper-framework + nlp-
#: pipeline) regularly exceeds 3 minutes before the actual git push
#: even starts. Two confirmed overnight failures on 2026-04-19 lost
#: ralph's entire output when the 120s ceiling tripped mid-hook (issue
#: #2882). 600s wasn't enough either: on 2026-04-21 agent c3a69458
#: (#2564) timed out at 600s with the hook still running the scraper-
#: framework suite — 7200 tests via ``pytest -n auto`` on 4 vCPU
#: Fargate exceeded 10 min on a cold-cache first-pre-push. 1800s
#: (30 min) covers the worst-case full suite while still catching a
#: genuinely-stuck push; it aligns with the ``push_and_pr`` stuck
#: timeout fallback of 30 min in :data:`STUCK_TIMEOUT_SECONDS`.
GIT_PUSH_TIMEOUT_SECONDS = 1800

#: Per-issue cooldown — skip an issue from candidate selection if its
#: most recent ``dispatcher.agents`` row (any status) was created
#: within this many seconds. Prevents a systemically-broken issue from
#: burning through the candidate queue in a failure loop when paired
#: with the partial UNIQUE INDEX (which only blocks re-claim for
#: ``running``/``retrying``, not ``failed``/``crashed`` — see spec
#: §17 Risk 2 and issue #2804). 3600s (60 min) gives the diagnoser a
#: clear window to file a follow-up or un-fail the agent before the
#: scheduler retries. Tuned for the cap=1 cutover; a higher cap may
#: want a shorter cooldown to avoid starving the queue on transient
#: failures.
FAILED_AGENT_COOLDOWN_SECONDS = 3600

# --------------------------------------------------------------------------
# Phase 3E — retro orchestration + worktree cleanup + diagnoser
# effectiveness tracking (issue #2798)
# --------------------------------------------------------------------------

#: Phase value written to ``dispatcher.agents.phase`` after a successful
#: ``/task-v2-retro`` invocation. The agent's ``status`` stays
#: ``succeeded`` — only the post-success phase advances. Any retro
#: issues the skill produced have already been filed by this point.
PHASE_RETRO_DONE = "retro_done"

#: Phase value written when ``/task-v2-retro`` fails (timeout, non-zero
#: exit, missing/malformed output). The agent itself succeeded, so
#: ``status='succeeded'`` is preserved — only the retro phase failed.
#: Cleanup still runs from this terminal-with-retro-failed state.
PHASE_RETRO_FAILED = "retro_failed"

#: Phase value written when :meth:`_push_and_open_pr` detects that
#: ralph's §2.5d no-op guardrail fired — the working tree was clean
#: on SHIP and no commit was created on top of ``origin/main``. The
#: deliverable for a no-op SHIP is ralph's evidence comment on the
#: issue (data-only tasks like SQL backfills); there is nothing to
#: push or PR. Distinct terminal phase so the admin cockpit can
#: filter / count these separately from the normal
#: ``done``/``retro_done`` success states, and so a post-hoc query
#: like ``SELECT count(*) FROM dispatcher.agents WHERE phase='no_op'``
#: trivially counts them. Status remains ``succeeded``. Issue #3039.
PHASE_NO_OP = "no_op"

#: Phase value written after ``scripts/cleanup_worktree.sh`` succeeds.
#: This is the final terminal phase for a successful agent — no further
#: supervisor advances apply.
PHASE_CLEANUP_DONE = "cleanup_done"

#: Phase value written when ``scripts/cleanup_worktree.sh`` refuses to
#: remove the worktree (locked, no session log, etc.). The daemon does
#: NOT bypass the safety check with ``--force`` — an operator sweep can
#: clean up the worktree manually. This is also a terminal phase.
PHASE_CLEANUP_BLOCKED = "cleanup_blocked"

#: Hard wall-clock timeout for the ``scripts/cleanup_worktree.sh``
#: subprocess. The script does at most a single ``git worktree remove``
#: + a JSONL inspection — 60s is generous.
CLEANUP_WORKTREE_SUBPROCESS_TIMEOUT_SECONDS = 60

#: Hard timeout on the ``gh issue create`` subprocess used by the retro
#: phase to file follow-up issues. Short — the call is a single write
#: against github.com with no pagination.
RETRO_GH_ISSUE_CREATE_TIMEOUT_SECONDS = 15

#: Cap on retro issues filed per agent. A retro that wants to file more
#: than this is a strong signal something is wrong (the skill is meant
#: to identify high-signal findings, not generate process theater) —
#: log + truncate to keep DB load bounded if the skill misbehaves.
MAX_RETRO_ISSUES_PER_AGENT = 20

#: Max length on retro issue body files passed via ``--body-file``.
#: Matches the skill's own "keep each issue body under 3000 characters
#: where possible" guidance with 5x headroom for edge cases. Strictly
#: a defensive cap — the daemon truncates with a "[truncated]" suffix
#: rather than failing the file write.
MAX_RETRO_ISSUE_BODY_CHARS = 15000

#: Default labels added to every retro-filed issue when the retro skill
#: omits ``labels``. Kept tight — the skill's own ``labels`` array is
#: the authoritative source; this is just the fallback.
DEFAULT_RETRO_LABELS = ("type/dx", "agent/ready", "priority/p2")

# --------------------------------------------------------------------------
# Phase 3C — failure detection + retry machinery (issue #2791)
# --------------------------------------------------------------------------

#: Supervisor-tick stuck-timeout window (fallback / default when no
#: per-phase override applies). Agents whose most-recent
#: ``dispatcher.phase_transitions.ts`` is older than this are flagged
#: as ``stuck_timeout`` and flipped to ``status='crashed'`` so the retry
#: marker processor can recover them. Matches spec §7 step 1. 30 min is
#: generous for the long-tail ralph iteration (#2513, #2628) while still
#: well under the 180-minute ``subprocess_timeout_s`` ceiling.
STUCK_TIMEOUT_SECONDS = 30 * 60

#: Per-phase stuck-timeout overrides. The #2872 restart cascade exposed
#: that a single global threshold is too coarse — supervisor fired
#: ``stuck_timeout`` on a 2.5-minute ralph and a 90-second plan because
#: the stale ``MAX(ts)`` read from a pre-retry phase_transitions row
#: was already 30+ minutes old. Two changes close that gap:
#:
#:  1. Retry reset now writes a fresh ``phase_transitions`` row so the
#:     supervisor's ``MAX(ts)`` restarts its clock (see
#:     :meth:`_process_retry_markers` + :meth:`_resume_retrying_agent`).
#:  2. This table lets each phase declare its own "stuck after N
#:     seconds" window, matching its expected runtime distribution.
#:
#: Values below are conservative upper bounds — honestly-stuck agents
#: still trip the timer, but routine phase work never does. Any phase
#: not listed here falls back to :data:`STUCK_TIMEOUT_SECONDS`.
#:
#: Operators can override via ``dispatcher.config.stuck_timeout_s_by_phase``
#: (JSONB object merged into this default at read time — see
#: :meth:`_stuck_timeout_for_phase`).
STUCK_TIMEOUT_SECONDS_BY_PHASE: dict[str, int] = {
    # Issue #2885 bumped every LLM-bearing phase by 10× after an
    # overnight race: agent ``821e96ee`` ran ralph 5834s and finished
    # normally, but the supervisor flagged it stuck at 5419s (old 90
    # min threshold) and fired a retry. Ralph exited cleanly 415s
    # later, but the retry had already started, producing
    # ``phase_output_missing`` and a failed agent despite ralph
    # actually succeeding. Operator directive: "stop being
    # parsimonious on these limits. Cost is not the constraint;
    # success is." The new values are effectively "never trip the
    # timer on a phase that's still running normally" — a truly
    # stuck agent (subprocess hung, daemon crashed mid-phase) will
    # still trip eventually. Non-LLM phases (``claiming``,
    # ``awaiting_ci``, ``awaiting_deploy``, terminal sweeps) keep
    # their original tight windows because their upper bounds are
    # set by external systems (gh API, GitHub Actions, ECS rolling
    # deploy) not by our subprocess runtime.
    "claiming": 5 * 60,  # 5 min — claim is a single gh + psycopg call
    "planning": 9000,  # 2.5 hr (was 30 min) — plan is read-only LLM, issue #2885
    "plan": 9000,  # alias for phase_transitions "plan" row — issue #2885
    "ralph": 54000,  # 15 hr (was 90 min) — ralph iterates, issue #2885
    "summary": 6000,  # 100 min (was 30 min) — summary single-pass LLM, issue #2885
    "awaiting_ci": 120 * 60,  # 2 hr — CI + any flaky retry headroom
    "awaiting_deploy": 45 * 60,  # 45 min — dev deploy rolling wait
    "fix_ci": 18000,  # 5 hr (was 30 min) — single /task-v2-fix-ci, issue #2885
    "verify": 3000,  # 50 min (was unset, fell back to 30 min global) — issue #2885
    "retro": 3000,  # 50 min (was 20 min) — single /task-v2-retro, issue #2885
    "paused_by_killswitch": 60,  # 1 min — terminal phase, should be swept quickly
    "force_stopped": 60,  # 1 min — #2884 operator force_stop terminal phase
    "daemon_restart_abandoned": 60,  # 1 min — terminal phase from restart recovery
}

#: Terminal statuses on ``dispatcher.agents.status`` — the worker thread
#: aborts at the next phase boundary if it observes one of these, even
#: if the killswitch isn't engaged. Closes the #2872 Bug F zombie state:
#: diagnoser / supervisor / circuit breaker can write a terminal status
#: from outside the worker, but the pre-#2872 worker had no check for
#: external terminal writes, so it kept executing phases against a
#: ``failed`` row. See :meth:`_check_killswitch_and_abort`.
TERMINAL_AGENT_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "crashed", "plan_blocked", "needs_review"}
)

# --------------------------------------------------------------------------
# Milestone columns on ``dispatcher.agents`` — split the single
# ``status='succeeded'`` terminal write into four independently-stamped
# milestones so the admin cockpit can distinguish "shipped" from
# "verified" from "retro completed" (issue #2953, migration 35).
# --------------------------------------------------------------------------

#: Verify-skip reason written to ``dispatcher.agents.verify_skip_reason``
#: when a dispatcher-self-PR (touches ``scripts/dispatcher/``) is
#: detected in push_and_pr. Verify cannot meaningfully run against a
#: process that is about to be replaced by its own deploy, so the
#: verify phase no-ops and the admin cockpit renders the row as
#: shipped-without-verify (not as an incomplete-verify warning).
#: Issue #2953.
VERIFY_SKIP_REASON_SELF_DEPLOY = "self_deploy"

#: Path prefixes whose presence in a PR's file list triggers
#: ``verify_skip_reason=self_deploy``. Today: dispatcher source only.
#: Future candidates (``docs/`` → ``docs_only``, ``.github/workflows/``
#: → ``ci_cd_only``) are not yet written — the migration column allows
#: them but the daemon code path doesn't emit them until a follow-up
#: issue lands.
_SELF_DEPLOY_PATH_PREFIXES: tuple[str, ...] = ("scripts/dispatcher/",)

#: Failure category written when the daemon restart-recovery sweep
#: reclaims a ``status='running'`` agent left behind by the previous
#: daemon run. Tier-1 mechanical retry category — the agent gets a
#: fresh worktree + a new run of the full phase pipeline. See
#: :meth:`_recover_abandoned_agents` and #2872 Bug A.
FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED = "daemon_restart_abandoned"

#: Failure-category mirror of :data:`KILLSWITCH_TERMINAL_PHASE` (issue
#: #2936). The string value matches the phase name verbatim so any
#: future retry-marker path fired by operator killswitch / circuit-
#: breaker cap-flip can tag itself with this category and be recognized
#: by :data:`_INFRA_PREEMPTION_CATEGORIES`. Today no retry-marker path
#: uses it — :meth:`_check_killswitch_and_abort` flips the agent to
#: ``status='failed' phase='paused_by_killswitch'`` terminally — but
#: introducing the constant now lets the preemption classifier handle
#: the category the moment a killswitch retry flow lands. Killswitch
#: events are infrastructure preemption, not agent-driven failures,
#: and must not burn the retry budget.
FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH = KILLSWITCH_TERMINAL_PHASE

#: GitHub API rate-limit threshold (spec §7 step 2). When ``gh api
#: rate_limit --jq '.resources.core.remaining'`` reports fewer than this
#: many requests remaining, the supervisor writes a
#: ``gh_rate_exhausted`` failure row and sets a daemon-level skip flag
#: that suppresses both ``_claim_and_orchestrate_one`` (scheduler tick)
#: and ``_advance_running_agents`` (supervisor tick) until the rate
#: window resets. 100 matches the CLAUDE.md §GitHub API Rate Limit
#: Awareness guidance.
GH_RATE_LIMIT_THRESHOLD = 100

#: Subprocess timeout for the ``gh api rate_limit`` rate-limit probe.
#: Short because the call is a single read against github.com with no
#: pagination or list-iteration.
GH_RATE_CHECK_TIMEOUT_SECONDS = 10

#: Hard cap on auto-retry attempts per agent+reason. Matches the CHECK
#: constraint on ``dispatcher.retry_markers.attempt`` (migration 21) and
#: spec §8 "give up after attempt 3".
MAX_RETRY_ATTEMPTS = 3

#: Fallback backoff schedule (seconds) when ``dispatcher.config
#: .backoff_seconds`` is missing or malformed. Matches the migration-21
#: seed ``[60, 300, 900]`` — 1 min, 5 min, 15 min. The Nth element is
#: the delay BEFORE the Nth attempt (attempt 1 = 60s from now; attempt
#: 2 = 300s from the marker-creation time of attempt 2; attempt 3 =
#: 900s from marker-creation time of attempt 3).
DEFAULT_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900)

#: Failure categories from spec §8 Tier-1 mechanical-fix table. The
#: daemon writes ``dispatcher.failures.category`` rows using these
#: exact strings so the weekly summary report (§7 step 4) and the
#: admin page can group them consistently.
FAILURE_CATEGORY_STUCK_TIMEOUT = "stuck_timeout"
FAILURE_CATEGORY_GH_RATE_EXHAUSTED = "gh_rate_exhausted"
FAILURE_CATEGORY_SUBPROCESS_CRASH = "subprocess_crash"
FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT = "subprocess_turn_limit"
FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL = "subprocess_auth_fail"

#: Tier-3 failure category from spec §8 (``ci_red_after_retries``).
#: Written by 3B's fix-CI exhaustion path when ``retries_used >=
#: FIX_CI_MAX_RETRIES`` before the agent is marked failed. 3D's
#: diagnoser picks it up on the next supervisor tick for immediate
#: (tier-3) diagnosis — there is no mechanical retry that reliably
#: fixes "the CI-fixing skill couldn't fix CI three times in a row".
FAILURE_CATEGORY_CI_RED_AFTER_RETRIES = "ci_red_after_retries"

#: Tier-3 failure categories from spec §8 for AC-infeasibility (issue
#: #3010). Both are written by post-exit parse of the structured phase
#: output JSON — ``ralph_ac_infeasible`` when ``ralph.json.verdict ==
#: "AC_INFEASIBLE"`` and ``summary_ac_infeasible`` when
#: ``summary.json.verdict == "AC_INFEASIBLE"``. Neither has a mechanical
#: retry (the AC is structurally wrong, not flaky), so both route
#: directly to the diagnoser for ``reissue`` / ``escalate`` / ``close``.
FAILURE_CATEGORY_RALPH_AC_INFEASIBLE = "ralph_ac_infeasible"
FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE = "summary_ac_infeasible"

#: Git-push failure categories for the ``push_and_pr`` phase (issue #2902).
#: ``push_failed`` is the generic catch-all; the two sub-kinds are
#: classifier-derived from stderr content via ``_classify_push_failure``.
#: Issue #3032 moved these from the tier-1 auto-retry set to the
#: tier-2 first-occurrence diagnoser set — the LLM now owns the
#: retry/escalate decision on push failures (remote rejections like
#: PAT scope need operator action, not blind retry).
FAILURE_CATEGORY_PUSH_FAILED = "push_failed"
FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED = "pre_push_hook_rejected"
FAILURE_CATEGORY_GIT_PUSH_NETWORK = "git_push_network"

#: Additional terminal-failure categories routed through the diagnoser
#: by the unified ``_handle_agent_failure`` path (issue #3032).
#: ``pr_create_failed`` — ``gh pr create`` non-zero exit or exception;
#: commonly caused by a duplicate PR from a prior crashed agent, bad
#: base ref, or a GitHub API hiccup — all benefit from Opus judgment.
#: ``phase_output_missing`` — a per-phase subprocess exited 0 but the
#: expected structured output JSON is missing (indicates prompt drift
#: or skill bug); hardcoded retry does not help.
FAILURE_CATEGORY_PR_CREATE_FAILED = "pr_create_failed"
FAILURE_CATEGORY_PHASE_OUTPUT_MISSING = "phase_output_missing"

#: Which failure categories auto-create a retry marker (tier 1 per
#: spec §8 table). ``subprocess_turn_limit`` (tier 2) and
#: ``subprocess_auth_fail`` (halt — no retry) are intentionally
#: excluded; 3D's diagnoser owns the escalation path for both.
#: ``stuck_timeout``, ``gh_rate_exhausted``, ``subprocess_crash``, and
#: ``daemon_restart_abandoned`` are the infra-preemption / crash
#: categories that retry mechanically. Push and PR-create failures
#: (formerly tier-1 auto-retry) were moved to the diagnoser path by
#: issue #3032 — see :data:`TIER_2_FIRST_OCCURRENCE_CATEGORIES`.
AUTO_RETRY_CATEGORIES = frozenset(
    {
        FAILURE_CATEGORY_STUCK_TIMEOUT,
        FAILURE_CATEGORY_GH_RATE_EXHAUSTED,
        FAILURE_CATEGORY_SUBPROCESS_CRASH,
        FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
    }
)

#: Retry reasons that represent **infrastructure preemption** — the
#: daemon container restarted mid-flight, the operator engaged the
#: killswitch, the circuit breaker flipped the concurrency cap to 0.
#: None of these indicate an actual problem with the agent's code or
#: runtime, so the retry they trigger must NOT count toward the
#: per-agent attempt budget surfaced by
#: :meth:`DispatcherDaemon._current_attempt_for` (read from
#: ``dispatcher.agents.retries_used``). Issue #2936 — during a stretch
#: of 5+ dispatcher redeploys on 2026-04-20 a single agent caught two
#: ``daemon_restart_abandoned`` events and burned 2 of its 3 retries
#: before executing any real work.
#:
#: :meth:`DispatcherDaemon._process_retry_markers` branches on this
#: set: when the marker's ``reason`` is in the frozenset, the reset
#: preserves ``retries_used`` (the agent re-enters the pipeline at the
#: same attempt number it was at when preempted); for any other
#: reason, ``retries_used`` increments as before. The
#: ``daemon.retry_processed`` log event records the decision in a
#: ``retry_counted`` boolean so CloudWatch queries can distinguish
#: free retries from budgeted ones.
_INFRA_PREEMPTION_CATEGORIES = frozenset(
    {
        FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
        FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH,
    }
)

#: Cross-runner subprocess exit-code → category table. Claude-p exits
#: with 1 for essentially every non-success case (see spec §8 intro +
#: spike 0.1 #2683), so exit code alone is insufficient — the stderr
#: regex fallback classifies further. Gemini CLI uses distinct codes
#: per spike 0.4 #2686: 41 = auth missing, 53 = turn limit.
GEMINI_EXIT_CODE_TO_CATEGORY: dict[int, str] = {
    41: FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL,
    53: FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT,
}

#: Subprocess stderr/stdout tail regexes. First match wins. Pattern is
#: compiled case-insensitive; the input is already the last ~500 chars
#: of the log (see ``_log_tail``). Order matters — auth-fail catches
#: the 401 before the generic crash category takes over.
_SUBPROCESS_STDERR_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"Invalid API key", FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL),
    (r"401\s+Unauthorized", FAILURE_CATEGORY_SUBPROCESS_AUTH_FAIL),
    (r"Reached max turns", FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT),
)


def _stderr_tail(stderr: str | bytes | None) -> str:
    """Return the trailing :data:`STRUCTURED_LOG_STDERR_MAX` chars of *stderr*.

    Handles ``bytes`` input (subprocess ``capture_output=True`` with
    ``text=False``), ``None``, and empty strings uniformly. No ellipsis
    marker is added — callers embed the tail verbatim in structured-log
    ``extra`` dicts where the truncation point is understood by convention.

    .. note::
        This helper is for **structured-log event fields** (rare, error-path
        only).  The per-phase ``stderr_tail`` fields in the normal subprocess
        flow use the named cap constants
        (:data:`PHASE_STDERR_TAIL_MAX_CHARS`, etc.) directly — do **not**
        replace those with this helper.

    Issue #2902.
    """
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    stderr = stderr.strip()
    if len(stderr) <= STRUCTURED_LOG_STDERR_MAX:
        return stderr
    return stderr[-STRUCTURED_LOG_STDERR_MAX:]


def _format_age_ago(seconds: float) -> str:
    """Render a compact human-readable age like ``2h 15m ago`` (#3026).

    Used to populate the ``{age_ago}`` placeholder in the RESUME WITH
    CONFLICT prompt block. Resolution is intentionally coarse — ralph
    only needs to know "is this patch stale or fresh" to make the
    continue-vs-abort judgment; exact seconds add noise.

    Buckets:

    * < 60 s   → ``"just now"``
    * < 60 min → ``"{N}m ago"``
    * < 24 h   → ``"{N}h {M}m ago"``
    * else     → ``"{N}d {H}h ago"``
    """
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m ago"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    return f"{days}d {hours}h ago"


def _classify_push_failure(stderr: str) -> str:
    """Classify a ``git push`` non-zero exit by inspecting stderr.

    Returns one of the ``FAILURE_CATEGORY_PUSH_*`` constants:

    * ``pre_push_hook_rejected`` — ``pre-push:`` hook output in stderr
      (e.g. ``.githooks/pre-push`` reporting a lint or test failure).
    * ``git_push_network`` — network-layer error: DNS, connection refused,
      or a 4xx/5xx from the remote (GitHub outage, auth).
    * ``push_failed`` — catch-all for any other non-zero exit.

    Modelled on :meth:`DispatcherDaemon._classify_subprocess_failure`.
    Issue #2902.
    """
    import re  # noqa: PLC0415 — lazy import; called only on error path

    tail = stderr or ""
    if re.search(r"pre-push:", tail, re.IGNORECASE):
        return FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED
    if re.search(
        r"could not resolve host|connection refused|The requested URL returned error",
        tail,
        re.IGNORECASE,
    ):
        return FAILURE_CATEGORY_GIT_PUSH_NETWORK
    return FAILURE_CATEGORY_PUSH_FAILED


# --------------------------------------------------------------------------
# Phase 3D — diagnoser (issue #2795, spec §8 "Diagnosis Step")
# --------------------------------------------------------------------------

#: Tier-2 categories that trigger the diagnoser ONLY after a tier-1
#: mechanical retry has already fired once and the same failure category
#: recurs for the same agent within a recent window. Matches spec §8:
#: "only after the mechanical fix has been tried once and the failure
#: recurs". ``stuck_timeout``, ``gh_rate_exhausted``, ``subprocess_crash``
#: are tier-1 retry categories whose **recurrence** escalates to tier 2;
#: ``subprocess_turn_limit`` is explicitly tier 2 on first occurrence
#: (see §8 table — "Retry once with narrower scope hint; second trip
#: escalates" — our policy is to diagnose on first occurrence so the
#: diagnoser can choose retry_with_hint vs escalate).
TIER_2_RECURRENCE_CATEGORIES: frozenset[str] = frozenset(
    {
        FAILURE_CATEGORY_STUCK_TIMEOUT,
        FAILURE_CATEGORY_GH_RATE_EXHAUSTED,
        FAILURE_CATEGORY_SUBPROCESS_CRASH,
    }
)

#: Tier-2 categories that diagnose on **first** occurrence (no mechanical
#: retry runs). Spec §8 table classifies ``subprocess_turn_limit`` as
#: tier 2 with mechanical hint = "retry once with narrower scope"; we
#: delegate that retry-vs-escalate choice to the diagnoser immediately
#: rather than hard-coding one mechanical retry.
#:
#: Issue #3032 extended this set: ``push_failed`` /
#: ``pre_push_hook_rejected`` / ``git_push_network`` /
#: ``pr_create_failed`` / ``phase_output_missing`` now diagnose on
#: first occurrence (they used to tier-1 auto-retry or skip the
#: failure-row write entirely). The cascade of 6 consecutive
#: ``git_push_failed`` events in 2026-04-22/23 on #3008 and #2610 —
#: all with a deterministic PAT-scope stderr that blind retry could
#: not resolve — made the case for routing every agent-terminal
#: failure through the LLM, not just tier-2 recurrences.
TIER_2_FIRST_OCCURRENCE_CATEGORIES: frozenset[str] = frozenset(
    {
        FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT,
        FAILURE_CATEGORY_PUSH_FAILED,
        FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED,
        FAILURE_CATEGORY_GIT_PUSH_NETWORK,
        FAILURE_CATEGORY_PR_CREATE_FAILED,
        FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
    }
)

#: Tier-3 categories that diagnose on first occurrence (spec §8).
#: ``ci_red_after_retries`` — the 3B fix-CI loop has already burned its
#: 3 mechanical retries, so there is no reliable mechanical remedy left.
#: ``ralph_ac_infeasible`` / ``summary_ac_infeasible`` (issue #3010) —
#: ralph or summary determined one or more ACs are structurally
#: impossible (non-existent symbol, self-contradiction, out-of-scope
#: dependency); no mechanical retry fixes a malformed AC, so the
#: diagnoser picks ``reissue`` / ``escalate`` / ``close`` immediately.
TIER_3_CATEGORIES: frozenset[str] = frozenset(
    {
        FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
        FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
        FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
    }
)

#: Window inside which a recurring tier-2 failure for the same agent +
#: category triggers the diagnoser. 24 h is generous enough to catch
#: same-day pattern recurrences (e.g. a flaky external API that breaks
#: again in the afternoon) while not re-triggering on unrelated failures
#: weeks later.
TIER_2_RECURRENCE_WINDOW_SECONDS = 24 * 60 * 60

#: Hard wall-clock timeout for the ``/diagnose-failure`` subprocess
#: (``claude -p``). Matches spec §8 "5-min hard wall-clock timeout".
#: Timeout, non-zero exit, or malformed recommendation JSON → fall
#: back to fixed mechanical escalation policy (spec §8 "Budget &
#: safety").
DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS = 5 * 60

#: ``--max-turns`` value for the diagnoser. 30 is generous for a
#: read-only reasoning task — the skill reads a JSONB context, maybe
#: fetches an issue/PR/CI log, and writes a recommendation. Matches
#: the frontmatter in ``.claude/skills/diagnose-failure/SKILL.md``.
DIAGNOSER_MAX_TURNS = 30

#: Default model for the diagnoser. Matches
#: ``dispatcher.config.model_by_phase.diagnose`` seed (``opus``) from
#: migration 21. The decision is low-frequency but high-impact —
#: Opus's reasoning headroom is cheap insurance against a wrong
#: ``close`` / ``reissue``.
DIAGNOSER_MODEL = "opus"

#: Valid ``action`` strings a diagnoser recommendation may set. The
#: daemon's deterministic consumer switches on these; any other value
#: falls through to ``escalate`` as a safe default and persists a row
#: to ``dispatcher.unrecognized_diagnoser_actions`` (logged as
#: ``daemon.diagnosis_action_unknown``) so operators can notice
#: patterns. Issue #3032 removed the closed-enum guardrail in the
#: diagnoser skill — the LLM may propose a novel action when none of
#: these fit, and the unrecognized-actions table is the review path.
#:
#: The first five (``retry`` through ``close``) are the original set
#: from issue #2795. The last three are added in #3032:
#:   - ``block_and_comment`` — apply ``status/blocked`` + remove
#:     ``agent/ready`` + post comment. For operator-action blockers
#:     (PAT scope, missing secret, infra gap) that do not warrant a
#:     tracking issue yet.
#:   - ``file_prerequisite_task`` — create a new issue via
#:     ``gh issue create`` with diagnoser-provided ``title`` + ``body``,
#:     then block the current issue on the new one.
#:   - ``block_on_existing_task`` — append ``Blocked by #<N>`` to the
#:     current issue body when the diagnoser identifies an already-open
#:     tracking issue for the root cause. Avoids duplicate tickets.
DIAGNOSER_ACTIONS: frozenset[str] = frozenset(
    {
        "retry",
        "retry_with_hint",
        "reissue",
        "escalate",
        "close",
        "block_and_comment",
        "file_prerequisite_task",
        "block_on_existing_task",
    }
)

#: Circuit-breaker bounds (spec §8 "Budget & safety"). When the
#: fallback rate (diagnoses with ``status='failed'``) over the last 24 h
#: exceeds the configured threshold AND at least this many diagnoses
#: have run, the daemon flips ``dispatcher.config.diagnoser_enabled``
#: to ``false``. Matches the spec's "≥5 diagnoses" floor so a single
#: early failure cannot trip the breaker.
CIRCUIT_BREAKER_MIN_DIAGNOSES = 5
CIRCUIT_BREAKER_WINDOW_SECONDS = 24 * 60 * 60

#: Default fallback-rate threshold when the ``dispatcher.config`` row
#: is missing or malformed. Matches the migration-26 seed ``0.30``.
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 0.30

# --------------------------------------------------------------------------
# Overnight-safety circuit breaker (#2860) — separate from the diagnoser
# circuit breaker above. This one trips on a streak of bad terminal agent
# outcomes (failed / crashed / plan_blocked / needs_review) and flips
# ``concurrency_cap`` to 0 so the dispatcher auto-pauses instead of
# burning $ all night on a cascading failure mode.
# --------------------------------------------------------------------------

#: Fallback "window minutes" when ``dispatcher.config.circuit_breaker_window_minutes``
#: is missing or malformed. Matches the migration-29 seed.
DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES = 30

#: Fallback "window size" (N in M-of-N) when the config row is missing
#: or malformed. Matches the migration-29 seed.
DEFAULT_OVERNIGHT_CB_WINDOW_SIZE = 10

#: Fallback "bad outcome threshold" (M in M-of-N). Matches the migration-29
#: seed. Trip opens when at least this many of the last ``window_size``
#: terminal outcomes in the rolling ``window_minutes`` are in
#: :data:`OVERNIGHT_CB_BAD_OUTCOME_STATUSES`.
DEFAULT_OVERNIGHT_CB_BAD_OUTCOME_THRESHOLD = 5

#: The classifier. Any terminal agent status NOT in this set counts as
#: "bad" for the circuit-breaker threshold. ``succeeded`` is the only
#: status treated as "good". This matches the spec's tolerant-classifier
#: contract (#2860): if a future correct-outcome terminal ships before
#: we update this set, it is conservatively counted as bad (better to
#: open the breaker on an unknown new state than to let a cascading
#: failure mode go undetected because the classifier didn't know about
#: its status string).
OVERNIGHT_CB_GOOD_OUTCOME_STATUSES: frozenset[str] = frozenset({"succeeded"})

#: Sentinel value for ``dispatcher.config.cap_flipped_by`` when the
#: overnight-safety circuit breaker opens. The scheduler tick's
#: auto-close path looks for this exact value to decide whether a cap
#: change back to ≥1 was operator-initiated (the flag gets cleared) vs
#: the breaker's own flip (no-op — the breaker has already flipped cap).
CAP_FLIPPED_BY_CIRCUIT_BREAKER = "circuit_breaker"

#: Path to the Telegram notification helper. Invoked as a subprocess
#: with ``--message-file <tmp>``. The helper exits 0 when Telegram is
#: unconfigured (no-op), 2 when all sends fail — see
#: ``scripts/notify-telegram.sh``. The daemon treats any non-zero exit
#: as a warning, not a failure: the circuit-breaker flip has already
#: happened regardless of whether the alert reaches the operator.
NOTIFY_TELEGRAM_SCRIPT_RELPATH = "scripts/notify-telegram.sh"

#: Hard timeout on the ``notify-telegram.sh`` subprocess. Short enough
#: that a Telegram outage can't stall a terminal transition; long
#: enough that AWS Secrets Manager + curl to the Bot API always fits
#: on a healthy network.
NOTIFY_TELEGRAM_SUBPROCESS_TIMEOUT_SECONDS = 30


# --------------------------------------------------------------------------
# Logging — structured JSON per line to stdout.
# --------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit one log record as a single JSON object per line.

    The dispatcher runs under Fargate → CloudWatch Logs → Logs Insights;
    Insights queries parse JSON-formatted log lines natively, which is
    the cheapest way to get structured queryability without a separate
    telemetry store. Any ``extra={...}`` passed to a ``logger.info`` call
    is merged into the envelope under its own keys.
    """

    #: Attributes ``LogRecord`` sets that we reformat into the envelope.
    _BUILTIN_ATTRS = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        envelope: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge in anything the caller attached via ``extra=``.
        for key, value in record.__dict__.items():
            if key in self._BUILTIN_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # ensure JSON-serializable
            except TypeError:
                value = repr(value)
            envelope[key] = value
        if record.exc_info:
            envelope["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(envelope, default=str)


def _configure_logging(level: str) -> logging.Logger:
    """Install the JSON formatter on stdout and return the daemon logger."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    # Replace any existing handlers (e.g. pytest's) so our output is
    # pure JSON when the daemon runs standalone. In tests this is
    # harmless; tests don't assert on log output.
    root.handlers = [handler]
    root.setLevel(level.upper())
    return logging.getLogger("dispatcher.daemon")


# --------------------------------------------------------------------------
# Configuration + context
# --------------------------------------------------------------------------


@dataclass
class DaemonConfig:
    """Parsed CLI + env configuration."""

    database_url: str
    tick_scheduler_seconds: int = DEFAULT_SCHEDULER_TICK_SECONDS
    tick_supervisor_seconds: int = DEFAULT_SUPERVISOR_TICK_SECONDS
    #: Housekeeping tick cadence (hourly by default). Controls how often
    #: :meth:`DispatcherDaemon._housekeeping_tick` prunes transient
    #: dispatcher tables. See ``DEFAULT_HOUSEKEEPING_TICK_SECONDS``.
    tick_housekeeping_seconds: int = DEFAULT_HOUSEKEEPING_TICK_SECONDS
    log_level: str = "INFO"
    version_sha: str = "unknown"
    host: str = ""
    pid: int = 0
    #: Repo the queue scan observes (``owner/name``). Falls back to
    #: ``DEFAULT_GITHUB_REPO`` when ``GITHUB_REPO`` is unset.
    github_repo: str = DEFAULT_GITHUB_REPO
    #: ECS service name used as the ``Service`` dimension on the
    #: ``HeartbeatAge`` CloudWatch metric. Populated from the
    #: ``DISPATCHER_SERVICE_NAME`` env var; falls back to the hostname.
    dispatcher_service_name: str = ""
    #: CloudWatch namespace for the heartbeat metric. Wired by the
    #: terraform module's ``HEARTBEAT_METRIC_NAMESPACE`` env var.
    heartbeat_metric_namespace: str = DEFAULT_HEARTBEAT_METRIC_NAMESPACE
    #: AWS region for the CloudWatch client.
    aws_region: str = DEFAULT_AWS_REGION
    #: Absolute path to the daemon's baseline git clone, or ``None`` when
    #: the daemon should fall back to ``os.getcwd()`` as the git parent
    #: (local-dev / unit-test mode). Populated from the
    #: ``DISPATCHER_BASELINE_REPO_ROOT`` env var by :func:`_build_config`;
    #: the Dockerfile wires this to :data:`DEFAULT_BASELINE_REPO_ROOT` so
    #: the Fargate daemon always runs in baseline-clone mode (issue #2804).
    #: When set, the daemon clones ``judgemind/judgemind`` here at boot
    #: and places per-agent worktrees at ``baseline_repo_root.parent /
    #: "worktrees" / agent-<uuid>`` (see :data:`DEFAULT_BASELINE_REPO_ROOT`).
    baseline_repo_root: Path | None = None
    # Optional override used by tests to avoid os.environ mutation.
    config_override: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# DB helpers — every call lives inside ``DispatcherDaemon`` so tests can
# substitute a fake psycopg module via ``sys.modules``.
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


#: Map priority label name → rank used for candidate ordering. Lower
#: ranks are picked first. Issue #2835: the daemon used to pick issues
#: in gh's default ``created_at desc`` order, ignoring priority labels.
#: Anything without a priority label sinks below p3 via
#: :data:`_PRIORITY_RANK_NO_LABEL` so priority-labeled work always wins
#: a head-to-head against unlabeled work, but unlabeled issues are
#: still eligible once the labeled ones have been exhausted.
_PRIORITY_LABEL_RANKS: dict[str, int] = {
    "priority/p0": 0,
    "priority/p1": 1,
    "priority/p2": 2,
    "priority/p3": 3,
}
_PRIORITY_RANK_NO_LABEL: int = 4


def _extract_priority(labels: Any) -> str | None:
    """Extract the priority label value (``'p0'`` | ``'p1'`` | ``'p2'`` | ``'p3'``).

    Issue #2899. Used at claim time to populate
    ``dispatcher.agents.priority`` from the per-issue ``labels`` array
    stored in ``dispatcher.queue_snapshots.issues_json``.

    When an issue carries more than one priority label (shouldn't
    happen, but defensive), the most urgent (lowest-numbered) wins —
    the same convention as :func:`_priority_rank`.

    Tolerates non-list input (returns None) so a malformed
    ``issues_json`` row cannot crash the claim path.
    """
    if not isinstance(labels, list):
        return None
    best: str | None = None
    best_rank = _PRIORITY_RANK_NO_LABEL
    for entry in labels:
        if not isinstance(entry, str):
            continue
        rank = _PRIORITY_LABEL_RANKS.get(entry)
        if rank is not None and rank < best_rank:
            best_rank = rank
            # The label is ``priority/pN``; the stored value is ``pN``.
            best = entry.split("/", 1)[1] if "/" in entry else None
    return best


def _priority_rank(labels: list[str] | Any) -> int:
    """Return the lowest (highest-priority) rank implied by ``labels``.

    Issue #2835. Used as the primary sort key in
    :meth:`DispatcherDaemon._latest_queue_snapshot_issues` so the
    daemon picks ``priority/p0`` issues before ``p1``, ``p1`` before
    ``p2`` etc.

    - ``priority/p0`` → 0
    - ``priority/p1`` → 1
    - ``priority/p2`` → 2
    - ``priority/p3`` → 3
    - No priority label → :data:`_PRIORITY_RANK_NO_LABEL` (4)

    When an issue carries more than one priority label (shouldn't
    happen, but defensive), the lowest rank wins — picking the most
    urgent interpretation matches operator intent.

    Tolerates non-list input (returns the no-label floor) so a
    malformed ``issues_json`` row cannot crash the sort.
    """
    if not isinstance(labels, list):
        return _PRIORITY_RANK_NO_LABEL
    best = _PRIORITY_RANK_NO_LABEL
    for entry in labels:
        if not isinstance(entry, str):
            continue
        rank = _PRIORITY_LABEL_RANKS.get(entry)
        if rank is not None and rank < best:
            best = rank
    return best


def _normalize_issue_enrichment(
    issue: dict[str, Any],
    *,
    include_body: bool = False,
) -> dict[str, Any]:
    """Project a ``gh issue list``-shaped dict into the enrichment record.

    The admin-page API reads these records directly out of
    ``dispatcher.queue_snapshots.issues_json`` /
    ``dispatcher.blocked_snapshots.issues_json`` — any change here must
    stay wire-compatible with
    ``packages/api/src/graphql/dispatcher/resolvers.ts`` (issue #2820).

    Output shape::

        {
          "number":    <int>,
          "title":     <str>,           # "" when missing
          "labels":    [<str>, ...],    # name-only, de-nested from gh's
                                        # {id, name, description, color}
                                        # label objects
          "createdAt": <str>,           # ISO-8601 string as returned by gh;
                                        # "" when missing
          # when include_body=True (blocked snapshot only):
          "body":      <str | null>,    # so the API can parse
                                        # ``Blocked by #N`` without a
                                        # GitHub call
        }

    ``gh`` sometimes omits keys on error responses; callers should
    already have filtered to dict-shaped entries before calling this.
    """
    number = issue.get("number")
    title = issue.get("title") if isinstance(issue.get("title"), str) else ""
    created_at = (
        issue.get("createdAt") if isinstance(issue.get("createdAt"), str) else ""
    )
    labels_raw = issue.get("labels") or []
    labels: list[str] = []
    for entry in labels_raw:
        if isinstance(entry, str):
            labels.append(entry)
            continue
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                labels.append(name)
    record: dict[str, Any] = {
        "number": number,
        "title": title,
        "labels": labels,
        "createdAt": created_at,
    }
    if include_body:
        body = issue.get("body")
        record["body"] = body if isinstance(body, str) else None
    return record


class LeaseError(RuntimeError):
    """Another daemon is holding the active-heartbeat lease."""


class CommandError(RuntimeError):
    """A command handler received an invalid or inapplicable payload.

    Raised by ``_handle_*`` methods when the command cannot be applied
    (e.g. ``retry`` issued for an already-running agent, a per-agent
    ``force_stop`` naming an unknown ``agentId``). The caller catches
    this, rolls back, and records a ``dispatcher.failures`` row so the
    command is left unconsumed for visibility rather than silently
    discarded.
    """


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------


class DispatcherDaemon:
    """Dispatcher daemon — Phase 2 shadow mode + Phase 3A orchestration.

    Responsibilities:
        * Connect to Postgres via ``DATABASE_URL``.
        * Run the lease check — bail out if another daemon is active
          within the heartbeat window.
        * INSERT a ``dispatcher.runs`` row on boot.
        * Run the scheduler loop every ``tick_scheduler_seconds``:
            - consume unconsumed ``dispatcher.commands`` via
              :meth:`_consume_commands`; each dispatched to its handler
              with consumed_at set AFTER the handler (#2801);
            - read ``dispatcher.config.concurrency_cap``;
            - enforce the Phase 2 spawn-safety guard (warn if
              ``concurrency_cap != 0``);
            - scan the GitHub ``agent/ready`` queue via ``gh issue list``
              and INSERT a row into ``dispatcher.queue_snapshots``;
            - **Phase 3A (#2783):** if ``concurrency_cap > 0`` AND no
              active agent exists, claim one trusted candidate issue
              and orchestrate ``/task-v2-plan`` → ``/task-v2-ralph`` →
              ``/task-v2-summary`` → ``git push`` + ``gh pr create``.
              The orchestration path stays cold in production because
              ``concurrency_cap`` is still 0 (Phase 3E flips it).
        * Run the supervisor loop every ``tick_supervisor_seconds``:
            - UPDATE ``dispatcher.runs.heartbeat_ts``;
            - count recent ``dispatcher.failures`` rows;
            - publish a ``HeartbeatAge`` CloudWatch metric under the
              configured namespace (default ``Judgemind/Dispatcher``)
              so the alarm defined in the terraform module
              (``infra/terraform/modules/dispatcher-daemon``) sees fresh
              data and does not fire.
        * Run the housekeeping loop every ``tick_housekeeping_seconds``
          (hourly by default):
            - prune ``dispatcher.queue_snapshots`` rows older than
              ``queue_snapshot_retention_days`` (default 30;
              ``dispatcher.config`` overridable). Issue #2778.
            - prune ``dispatcher.phase_outputs`` rows older than
              ``phase_output_retention_days`` (default 30). Issue #2779.
            - prune ``dispatcher.notifications`` rows older than
              ``notification_retention_days`` (default 30). Issue #2779.
        * On SIGTERM / SIGINT, UPDATE ``dispatcher.runs.stopped_at`` and
          exit 0.

    What this daemon still does **NOT** do:
        * Flip ``concurrency_cap`` from 0 to 1 — that is an explicit
          operator action documented in
          ``docs/agent/infrastructure-reference.md``. Phase 3E added
          all the orchestration plumbing but left the flip to a
          human-approved cutover.

    **Phase 3E (#2798)** completes the per-agent lifecycle by adding
    two more advance branches to ``_advance_running_agents`` for
    ``status='succeeded'`` agents:

        * ``phase='done'`` → spawn ``/task-v2-retro`` and file every
          retro issue it produced via ``gh issue create``. Transition
          to ``phase='retro_done'`` on success or ``phase='retro_failed'``
          on subprocess failure. ``status='succeeded'`` is preserved.
        * ``phase IN ('retro_done', 'retro_failed')`` → run
          ``scripts/cleanup_worktree.sh``. Transition to
          ``phase='cleanup_done'`` on success or
          ``phase='cleanup_blocked'`` on safety-check refusal. Per
          CLAUDE.md §Critical Rules the daemon never bypasses with
          ``--force`` — operators sweep blocked worktrees manually.

    Phase 3E also adds **diagnoser effectiveness tracking** (spec §8
    line 305) — ``_mark_agent_terminal`` writes the resolved outcome
    back to any pending ``dispatcher.diagnoses`` rows for that agent
    so the weekly report can measure "diagnoser recommended X →
    outcome Y". Idempotent under repeated terminal transitions.

    **Phase 3B (#2787)** adds the post-PR transitions via
    ``_advance_running_agents``, invoked from the supervisor tick:

        * ``phase='awaiting_ci'`` → one-shot ``gh pr view`` poll:
          pending → no-op; green → merge; red → spawn
          ``/task-v2-fix-ci`` (up to ``FIX_CI_MAX_RETRIES`` times).
        * ``phase='awaiting_deploy'`` → find the deploy runs triggered
          by the merge commit, poll: in_progress → no-op;
          success (or no applicable deploy) → spawn
          ``/task-v2-verify`` and post evidence comment on the issue;
          failure → mark agent failed for 3C/3D to handle.

    Per-agent failures inside ``_advance_running_agents`` are caught
    and logged as ``daemon.advance_failed``; the supervisor tick then
    moves on to the next agent so one bad row cannot stall the
    whole daemon.
    """

    def __init__(self, cfg: DaemonConfig, logger: logging.Logger):
        self._cfg = cfg
        self._log = logger
        # ``_conn`` is exposed as a property (below) that resolves to the
        # current thread's psycopg connection. The main thread uses
        # ``_main_conn``; the orchestration worker thread (#2847) opens
        # its own connection in :meth:`_orchestration_worker_entry` and
        # stashes it on ``_thread_state.conn``. This lets scheduler_tick
        # on the main thread observe ``concurrency_cap`` at its 30s
        # cadence without contending with the worker thread's per-phase
        # DB transactions. Each thread owns its own psycopg Connection
        # because psycopg3 Connections are not thread-safe.
        self._main_conn: Connection[Any] | None = None
        self._thread_state = threading.local()
        self._run_id: str | None = None
        self._stop = threading.Event()
        self._scheduler_ticks = 0
        self._supervisor_ticks = 0
        self._housekeeping_ticks = 0
        self._last_heartbeat_at: datetime | None = None
        # CloudWatch client is created lazily on first publish so tests can
        # mock it via ``_make_cloudwatch_client``. Shared across supervisor
        # ticks — boto3 clients are thread-safe and reusing one avoids
        # repeated credential lookups.
        self._cloudwatch_client: Any | None = None
        # Phase 3A: within-tick handoff between phase helpers. Reset at
        # the start of each orchestration run so a previous failure's
        # partial state cannot leak into the next attempt.
        self._agent_plan_output: dict[str, Any] | None = None
        self._agent_ralph_output: dict[str, Any] | None = None
        self._agent_summary_output: dict[str, Any] | None = None
        #: Summary-phase ``unmet_criteria`` list. Populated by
        #: :meth:`_run_summary_phase` when the summary skill flagged
        #: criteria the ralph diff did not satisfy (#2856). When
        #: non-empty, :meth:`_push_and_open_pr` opens a DRAFT PR with
        #: the unmet list in the body and the agent terminates as
        #: ``status='needs_review'`` rather than ``succeeded``. Reset
        #: at the start of each orchestration run.
        self._agent_unmet_criteria: list[str] | None = None
        # Phase 3C: GitHub rate-limit skip window. When set, ``now() <
        # self._gh_rate_skip_until`` → scheduler + supervisor ticks both
        # skip their hot paths. Cleared by
        # ``_gh_rate_skip_active`` once the reset epoch elapses.
        # Represented as a UTC-aware datetime (not an epoch seconds) so
        # all time comparisons go through the same ``datetime.now(UTC)``
        # plumbing the rest of the daemon uses (see #2791 §2b).
        self._gh_rate_skip_until: datetime | None = None
        # Killswitch plumbing (#2847). The orchestration worker thread
        # runs ``_claim_and_orchestrate_one`` off the main loop so the
        # scheduler observation cadence stays strict at 30s regardless
        # of how long a phase takes. ``_pause_requested`` is set when
        # scheduler_tick observes ``concurrency_cap=0`` and checked by
        # the worker thread before each phase — a set event aborts the
        # pipeline at the next phase boundary, marking the agent
        # ``status='failed' phase='paused_by_killswitch'``. The lock
        # guards only the ``_orchestration_thread`` handle (start,
        # replace, join) — the actual orchestration work does not hold
        # it.
        self._orchestration_thread: threading.Thread | None = None
        self._orchestration_thread_lock = threading.Lock()
        self._pause_requested = threading.Event()
        # #2884 command-taxonomy flags. In-memory only — a daemon
        # restart loses them, which is acceptable: on restart the
        # in-flight worker thread doesn't exist yet, so there's
        # nothing to abort-vs-let-finish. Both flags read by
        # ``scheduler_tick`` (to decide whether cap=0 engages the
        # killswitch) and by ``_check_killswitch_and_abort`` (to
        # pick the terminal phase name — ``paused_by_killswitch`` vs
        # ``force_stopped``).
        #
        # ``_graceful_stop_requested`` — set by the ``stop`` handler.
        # Tells ``scheduler_tick`` to NOT auto-set ``_pause_requested``
        # when it observes cap==0. The in-flight worker keeps running
        # through its current phase pipeline; new spawns are still
        # blocked (cap=0). Cleared by the ``start`` handler and by the
        # ``force_stop`` handler.
        #
        # ``_force_stop_requested`` — set by the ``force_stop``
        # handler (global variant — no ``agentId`` in payload). Tells
        # ``_check_killswitch_and_abort`` to mark the aborting agent
        # with ``phase='force_stopped'`` instead of the default
        # ``phase='paused_by_killswitch'``. Cleared by the ``start``
        # handler.
        self._graceful_stop_requested: bool = False
        self._force_stop_requested: bool = False
        # Observation record: the most recent ``concurrency_cap`` value
        # read by scheduler_tick and when (monotonic seconds since
        # boot). Used by tests and by the ``daemon.scheduler_tick``
        # structured log event to show the cadence + cap in one line
        # for CloudWatch review.
        self._last_cap_observed: int | None = None
        self._last_cap_observed_monotonic: float | None = None

    # ------------------------------------------------------------------
    # Thread-aware connection accessor (#2847).
    #
    # The orchestration worker thread runs with its own psycopg
    # Connection, stashed on ``_thread_state.conn`` at thread entry.
    # Every existing ``self._conn`` reference in the codebase goes
    # through this property and resolves to the correct connection for
    # the current thread — no signature changes needed. When the main
    # thread touches ``self._conn`` it still sees ``self._main_conn``.
    # The setter preserves the ``self._conn = psycopg.connect(...)``
    # call site in :meth:`connect` / :meth:`close` without a rename.
    # ------------------------------------------------------------------

    @property
    def _conn(self) -> Connection[Any] | None:
        thread_conn = getattr(self._thread_state, "conn", None)
        if thread_conn is not None:
            return thread_conn
        return self._main_conn

    @_conn.setter
    def _conn(self, value: Connection[Any] | None) -> None:
        # Only the main thread ever assigns ``self._conn`` (in
        # ``connect`` / ``close``). The worker thread installs its
        # connection via ``self._thread_state.conn`` directly so that
        # its lifecycle is scoped to the thread entry-point's
        # try/finally — not this setter.
        self._main_conn = value

    # ── lifecycle ──────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the psycopg connection. Imported lazily so tests can mock."""
        import psycopg  # noqa: PLC0415  — lazy import

        self._log.info(
            "daemon.connect",
            extra={
                "event": "connect",
                "database_url_set": bool(self._cfg.database_url),
            },
        )
        self._conn = psycopg.connect(self._cfg.database_url, connect_timeout=10)
        # Autocommit off — every daemon step is one small transaction.
        self._conn.autocommit = False

    def check_lease_and_register_run(self) -> str:
        """Claim the active-daemon lease and INSERT ``dispatcher.runs``.

        Raises :class:`LeaseError` if another daemon's heartbeat is
        within ``LEASE_HEARTBEAT_WINDOW_SECONDS`` of now.

        Returns the new ``run_id``.
        """
        assert self._conn is not None, "connect() must run before registering"

        # Under SERIALIZABLE we would be free of races here, but the
        # check + insert happens fast enough that two daemons booting
        # simultaneously during a rolling deploy (§17 Risk 2) still race
        # on the millisecond window. That is acceptable: the second
        # daemon's supervisor tick will observe the first one's heartbeat
        # within the next 120s and can be watchdog-killed. The lease
        # here is a best-effort "don't start if we can see another
        # active peer".
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, heartbeat_ts "
                "FROM dispatcher.runs "
                "WHERE stopped_at IS NULL "
                "  AND heartbeat_ts > now() - make_interval(secs => %s) "
                "ORDER BY heartbeat_ts DESC "
                "LIMIT 1",
                (LEASE_HEARTBEAT_WINDOW_SECONDS,),
            )
            row = cur.fetchone()
            if row is not None:
                existing_run_id, existing_hb = row
                self._conn.rollback()
                raise LeaseError(
                    f"another dispatcher run is active: run_id={existing_run_id} "
                    f"heartbeat_ts={existing_hb!s}"
                )

            cur.execute(
                "INSERT INTO dispatcher.runs "
                "    (started_at, heartbeat_ts, version_sha, host, pid) "
                "VALUES (now(), now(), %s, %s, %s) "
                "RETURNING run_id",
                (self._cfg.version_sha, self._cfg.host, self._cfg.pid),
            )
            new_row = cur.fetchone()
            if (
                new_row is None
            ):  # pragma: no cover — INSERT ... RETURNING always returns
                raise RuntimeError("INSERT dispatcher.runs returned no row")
            self._run_id = str(new_row[0])
        self._conn.commit()

        self._log.info(
            "daemon.run_registered",
            extra={
                "event": "run_registered",
                "run_id": self._run_id,
                "version_sha": self._cfg.version_sha,
                "host": self._cfg.host,
                "pid": self._cfg.pid,
            },
        )
        return self._run_id

    def mark_stopped(self) -> None:
        """UPDATE ``dispatcher.runs.stopped_at`` on shutdown."""
        if self._conn is None or self._run_id is None:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.runs SET stopped_at = now() "
                    "WHERE run_id = %s AND stopped_at IS NULL",
                    (self._run_id,),
                )
            self._conn.commit()
            self._log.info(
                "daemon.run_stopped",
                extra={"event": "run_stopped", "run_id": self._run_id},
            )
        except Exception:  # pragma: no cover — best-effort on shutdown
            self._log.exception("daemon.mark_stopped_failed")
            try:
                self._conn.rollback()
            except Exception:
                pass

    def close(self) -> None:
        """Close the psycopg connection. Safe to call multiple times."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass
            self._conn = None

    # ── scheduler tick (every ``tick_scheduler_seconds``) ───────────────

    def scheduler_tick(self) -> dict[str, int]:
        """Run one scheduler tick. Phase 2 = queue scan + spawn-safety guard.

        Steps (in order; DB work is one transaction per step for isolation):
            1. Consume any pending ``dispatcher.commands`` via
               :meth:`_consume_commands`. Each command is dispatched to
               its handler; consumed_at is set AFTER the handler so a
               crash leaves the command unconsumed for next-tick retry
               (#2801).
            2. Read ``dispatcher.config.concurrency_cap``, record the
               observation, and SET (cap==0) or CLEAR (cap>0) the
               ``_pause_requested`` killswitch event for the worker thread
               to observe between phases (#2847).
            3. Fire the Phase 2 spawn-safety guard (log-only warning).
            4. Drain due retry markers whose reason is in
               :data:`_INFRA_PREEMPTION_CATEGORIES` via
               :meth:`_process_retry_markers(only_infra_preemption=True)`
               (#2949). Those markers re-add ``agent/ready`` to the
               interrupted issue, so running this step before the queue
               scan below lands the issue in the same tick's snapshot —
               it would otherwise wait for the next scheduler tick and
               lose its slot to fresh claims of equal priority. Budgeted
               retries (``subprocess_crash``, ``stuck_timeout``,
               ``gh_rate_exhausted``, ``operator_retry``) stay on the
               supervisor-tick drain.
            5. Scan the GitHub ``agent/ready`` queue and write a row to
               ``dispatcher.queue_snapshots``; run the blocked-list scan
               on its slower cadence.
            6. If the gate allows, spawn the orchestration worker thread
               (#2847). The spawn is non-blocking — the worker runs on a
               dedicated thread with its own psycopg connection so this
               tick returns promptly, preserving the 30s cadence even
               when a multi-minute phase is in flight.

        **Cadence invariant (#2847).** Before this change the tick
        called ``_claim_and_orchestrate_one`` inline, which ran the
        whole plan → ralph → summary → push pipeline synchronously and
        blocked the main run loop for up to 180 minutes per phase. The
        effect was that an operator cap=0 flip was not observed until
        the next free tick — often 5-10 minutes later, sometimes much
        longer. After this change the orchestration runs on a thread;
        the tick always returns in milliseconds and the cap observation
        happens on the strict ``tick_scheduler_seconds`` cadence
        regardless of worker state.

        Returns a small summary dict for logging + tests. Keys:

            * ``commands_consumed``: int, rowcount from step 1.
            * ``concurrency_cap``: int, or ``-1`` sentinel if unset.
            * ``queue_depth``: int, ``-1`` if the scan failed.
            * ``blocked_depth``: int, ``-1`` if the scan did not run
              this tick or failed.
            * ``orchestration_attempted``: 1 if a worker thread was
              spawned this tick, 0 otherwise.
            * ``orchestration_thread_alive``: 1 iff a worker thread is
              still alive at end-of-tick (#2847).
            * ``pause_requested``: 1 iff the killswitch event is set
              at end-of-tick (#2847).
            * ``retry_markers_prioritized``: int, count of
              infra-preemption retry markers drained before the queue
              scan this tick (#2949). Non-zero means an interrupted
              agent was reclaimed ahead of any fresh queue claim.
        """
        assert self._conn is not None, "connect() must run before ticks"

        # 1. Consume any pending commands. Each command is dispatched to
        # its handler; consumed_at is set AFTER the handler returns so
        # a mid-handler crash leaves the command unconsumed for retry.
        commands_consumed = self._consume_commands()

        concurrency_cap: int | None = None

        with self._conn.cursor() as cur:
            # 2. Read concurrency_cap (for log observability + spawn guard).
            cur.execute(
                "SELECT value FROM dispatcher.config WHERE key = %s",
                ("concurrency_cap",),
            )
            row = cur.fetchone()
            if row is not None:
                # Stored as JSONB — a bare number comes back as int.
                concurrency_cap = int(row[0]) if row[0] is not None else None
        self._conn.commit()

        # Killswitch observation (#2847, amended #2884). Update the
        # observation state and the ``_pause_requested`` event so the
        # orchestration worker thread (if one is running) can abort at
        # the next phase boundary within ≤60s of an operator cap=0
        # commit.
        #
        # #2884 amendment: when the operator issued a graceful ``stop``
        # command (``_graceful_stop_requested`` flag set), cap==0 is
        # observed but ``_pause_requested`` is NOT set — the in-flight
        # worker is allowed to finish its current phase pipeline.
        # ``force_stop`` (or any other path that sets cap=0 — e.g. a
        # direct config edit, the circuit breaker) continues to engage
        # the killswitch as before.
        #
        # Any positive cap value CLEARS ``_pause_requested`` (and the
        # #2884 graceful-stop / force-stop flags as belt-and-braces) so
        # a cap=0 → cap=1 flip lets a fresh orchestration proceed
        # normally on the next tick.
        self._last_cap_observed = concurrency_cap
        self._last_cap_observed_monotonic = time.monotonic()
        if concurrency_cap == 0:
            if self._graceful_stop_requested:
                # Graceful stop: observe cap=0, do NOT engage the
                # killswitch. Log a distinct event so CloudWatch can
                # distinguish "operator asked for graceful stop" from
                # "cap=0 engaged killswitch". Idempotent on repeated
                # ticks — this is informational, not state.
                self._log.info(
                    "daemon.graceful_stop_in_progress",
                    extra={
                        "event": "graceful_stop_in_progress",
                        "run_id": self._run_id,
                        "observed_concurrency_cap": concurrency_cap,
                        "orchestration_in_flight": self._orchestration_thread_alive(),
                    },
                )
            else:
                if not self._pause_requested.is_set():
                    self._log.info(
                        "daemon.killswitch_engaged",
                        extra={
                            "event": "killswitch_engaged",
                            "run_id": self._run_id,
                            "observed_concurrency_cap": concurrency_cap,
                            "orchestration_in_flight": self._orchestration_thread_alive(),
                        },
                    )
                self._pause_requested.set()
        else:
            if self._pause_requested.is_set():
                self._log.info(
                    "daemon.killswitch_cleared",
                    extra={
                        "event": "killswitch_cleared",
                        "run_id": self._run_id,
                        "observed_concurrency_cap": concurrency_cap,
                    },
                )
            self._pause_requested.clear()
            # #2884: positive cap also clears the stop-intent flags so
            # a stale ``stop`` / ``force_stop`` from a prior cycle
            # doesn't leak into the next one. The ``start`` handler
            # already clears these, but an operator who sets cap back
            # to ≥1 via a direct config edit (bypassing the ``start``
            # command) must also get a clean slate.
            self._graceful_stop_requested = False
            self._force_stop_requested = False

        # Overnight-safety circuit breaker auto-close (#2860). When the
        # breaker previously opened it set ``cap_flipped_by='circuit_breaker'``
        # and flipped ``concurrency_cap`` to 0. If the operator has
        # since flipped cap back up to ≥1, log the close event and
        # clear the flag. Runs only on cap>0 ticks so the common
        # cap=0 path (Phase 2 steady state, most tests) adds zero
        # cursor reads. Wrapped in try/except so a failure here cannot
        # stall the scheduler tick.
        if concurrency_cap is not None and concurrency_cap >= 1:
            try:
                self._check_circuit_breaker_auto_close(concurrency_cap)
            except Exception:
                self._log.exception(
                    "daemon.circuit_breaker_auto_close_error",
                    extra={
                        "event": "circuit_breaker_auto_close_error",
                        "run_id": self._run_id,
                    },
                )

        # Phase 2 spawn-safety guard: the spawn path does not exist yet,
        # but a future Phase 3 wiring mistake could activate it. Warn if
        # the live config disagrees with the Phase 2 invariant. The
        # guard is advisory only — no subprocess is spawned regardless of
        # what ``concurrency_cap`` is set to in the database.
        if (
            concurrency_cap is not None
            and concurrency_cap != PHASE_2_REQUIRED_CONCURRENCY_CAP
        ):
            self._log.warning(
                "daemon.phase2_concurrency_cap_nonzero",
                extra={
                    "event": "phase2_concurrency_cap_nonzero",
                    "run_id": self._run_id,
                    "observed_concurrency_cap": concurrency_cap,
                    "required_concurrency_cap": PHASE_2_REQUIRED_CONCURRENCY_CAP,
                    "detail": (
                        "Phase 2 is shadow mode — daemon must not spawn "
                        "subprocesses. concurrency_cap should be 0 until "
                        "Phase 3 cut-over."
                    ),
                },
            )

        # Issue #2949 — process infra-preemption retry markers BEFORE
        # scanning the ``agent/ready`` queue. When a daemon restart
        # abandons an in-flight agent, PR #2944's terminal-and-reclaim
        # path marks the old row ``failed`` and re-adds ``agent/ready``
        # to the issue. Draining those markers here means the queue
        # scan below picks them up on the SAME tick; draining only in
        # the supervisor tick (120s cadence) would let up to four
        # scheduler ticks claim fresh work ahead of the interrupted
        # issue, which burns operator time on agents that paid the
        # plan cost already. Budgeted retries (``subprocess_crash``,
        # ``stuck_timeout``, ``gh_rate_exhausted``, ``operator_retry``)
        # are left for the supervisor drain — their reset path hands
        # off through ``_resume_retrying_agent`` so claim ordering is
        # unaffected by their processing cadence.
        #
        # Exceptions here are caught + logged but not re-raised — the
        # scheduler tick must survive any marker-scan failure so the
        # downstream queue scan + claim gate still fire this tick.
        retry_markers_prioritized = 0
        try:
            retry_markers_prioritized = self._process_retry_markers(
                only_infra_preemption=True
            )
        except Exception:
            self._log.exception(
                "daemon.retry_prioritized_pass_failed",
                extra={
                    "event": "retry_prioritized_pass_failed",
                    "run_id": self._run_id,
                },
            )
        if retry_markers_prioritized > 0:
            # Distinct event from ``retry_processed`` /
            # ``retry_terminal_and_reclaim`` so a CloudWatch Insights
            # filter can confirm the #2949 ordering kicked in on the
            # post-restart tick without scanning every retry event.
            self._log.info(
                "daemon.retry_prioritized_over_fresh_claim",
                extra={
                    "event": "retry_prioritized_over_fresh_claim",
                    "run_id": self._run_id,
                    "markers_processed": retry_markers_prioritized,
                },
            )

        # 3. Scan the ``agent/ready`` queue and persist a snapshot.
        #
        # Failures here (rate limit, network, auth) log + return -1 but
        # do NOT raise — the daemon must survive GitHub API hiccups,
        # and the next tick will try again (§15).
        queue_depth = self._scan_queue_and_snapshot()

        # 3b. Scan the ``status/blocked`` list on a slower cadence
        # (every ``BLOCKED_SCAN_EVERY_N_TICKS`` ticks — ~2min at the
        # default 30s tick). The blocked list changes slowly, so per-
        # tick polling would waste GitHub budget. Runs on the first
        # tick so the admin page has a populated blocked panel
        # immediately after daemon boot. Issue #2820.
        blocked_depth = -1  # sentinel: "scan not attempted this tick"
        if self._should_run_blocked_scan():
            blocked_depth = self._scan_blocked_and_snapshot()

        # 4. Phase 3A orchestration gate (#2783) — threaded spawn (#2847).
        #
        # Only enter the claim + orchestrate path when (a) the live
        # ``concurrency_cap`` is >0 AND (b) no agent is currently in
        # flight for this daemon run AND (c) Phase 3C's GitHub
        # rate-limit skip flag is not active AND (d) no orchestration
        # worker thread from a prior tick is still alive. Phase 3 runs
        # at ``concurrency_cap=1`` (one subprocess at a time); Phase 3E
        # flips the value from 0 to 1. Until then the gate stays
        # closed and this branch is a no-op.
        #
        # Orchestration runs on a dedicated worker thread so this tick
        # returns quickly (spawn + return = ~ms) and the main run loop
        # can re-fire ``scheduler_tick`` at its 30s cadence while the
        # worker runs its 5-90min phase pipeline. The worker checks
        # ``self._pause_requested`` between phases; an operator cap=0
        # flip propagates to an in-flight orchestration within one
        # phase boundary (≤60s for plan; worst-case ~one ralph
        # iteration for ralph).
        #
        # Exceptions here are caught + logged but not re-raised — the
        # scheduler tick must survive any spawn failure so the next
        # tick can try again. Worker-thread exceptions are caught in
        # ``_orchestration_worker_entry``.
        orchestration_attempted = False
        rate_skip_active = self._gh_rate_skip_active()
        if rate_skip_active:
            self._log.info(
                "daemon.claim_skipped_rate_limited",
                extra={
                    "event": "claim_skipped_rate_limited",
                    "run_id": self._run_id,
                    "skip_until": self._gh_rate_skip_until.isoformat()
                    if self._gh_rate_skip_until is not None
                    else None,
                },
            )
        elif (
            concurrency_cap is not None
            and concurrency_cap > 0
            and not self._is_paused()
            and not self._has_active_agent()
        ):
            try:
                orchestration_attempted = self._maybe_spawn_orchestration_thread()
            except Exception:
                # Daemon survival takes precedence over any single
                # orchestration spawn. The helper logs specific failures
                # internally; this is the belt-and-braces catch.
                self._log.exception(
                    "daemon.orchestration_spawn_failed",
                    extra={
                        "event": "orchestration_spawn_failed",
                        "run_id": self._run_id,
                    },
                )

        self._scheduler_ticks += 1
        orchestration_thread_alive = self._orchestration_thread_alive()
        pause_requested = self._pause_requested.is_set()
        self._log.info(
            "daemon.scheduler_tick",
            extra={
                "event": "scheduler_tick",
                "run_id": self._run_id,
                "tick_n": self._scheduler_ticks,
                "commands_consumed": commands_consumed,
                "concurrency_cap": concurrency_cap,
                "queue_depth": queue_depth,
                "blocked_depth": blocked_depth,
                "orchestration_attempted": orchestration_attempted,
                # #2847: orchestration now runs on a worker thread; these
                # two fields let CloudWatch Insights verify the cadence
                # holds regardless of worker state.
                "orchestration_thread_alive": orchestration_thread_alive,
                "pause_requested": pause_requested,
                # #2949: count of infra-preemption retry markers drained
                # before the queue scan this tick. Non-zero means an
                # interrupted agent was reclaimed ahead of fresh work.
                "retry_markers_prioritized": retry_markers_prioritized,
            },
        )
        return {
            "commands_consumed": commands_consumed,
            "concurrency_cap": -1 if concurrency_cap is None else concurrency_cap,
            "queue_depth": queue_depth,
            "blocked_depth": blocked_depth,
            "orchestration_attempted": 1 if orchestration_attempted else 0,
            # #2847 observability fields for tests and CloudWatch.
            "orchestration_thread_alive": 1 if orchestration_thread_alive else 0,
            "pause_requested": 1 if pause_requested else 0,
            # #2949 observability — count of infra-preemption retry
            # markers drained before the queue scan.
            "retry_markers_prioritized": retry_markers_prioritized,
        }

    # ── command consumption (#2801) ────────────────────────────────────

    def _consume_commands(self) -> int:
        """Drain unconsumed ``dispatcher.commands`` rows.

        SELECTs all rows with ``consumed_at IS NULL`` ordered by
        ``issued_at ASC``, dispatches each to its per-command handler,
        and marks ``consumed_at = now()`` AFTER the handler returns so
        that a mid-handler exception leaves the row unconsumed for
        next-tick retry.

        Handler exceptions are caught per-row: the transaction is
        rolled back and a ``dispatcher.failures`` row with
        ``category='handler_error'`` is inserted (best-effort) before
        continuing to the next command.

        Returns the number of commands successfully consumed.
        """
        assert self._conn is not None, "connect() must run before ticks"

        # #2884 simplified taxonomy: three global commands + retry.
        # Removed: drain, pause/resume, force_kill (per-agent variant
        # now lives as force_stop with an agentId payload).
        _HANDLERS = {
            "start": self._handle_start,
            "stop": self._handle_stop,
            "force_stop": self._handle_force_stop,
            "retry": self._handle_retry,
        }

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT command_id, command, payload "
                    "FROM dispatcher.commands "
                    "WHERE consumed_at IS NULL "
                    "ORDER BY issued_at ASC",
                )
                rows = cur.fetchall()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.commands_scan_failed",
                extra={
                    "event": "commands_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return 0

        consumed = 0
        for row in rows:
            command_id = int(row[0])
            command = str(row[1])
            raw_payload = row[2]
            payload: dict[str, Any] = (
                raw_payload if isinstance(raw_payload, dict) else {}
            )

            handler = _HANDLERS.get(command)
            try:
                if handler is None:
                    raise CommandError(
                        f"unknown command {command!r} — no handler registered"
                    )
                with self._conn.cursor() as cur:
                    handler(cur, payload)
                    cur.execute(
                        "UPDATE dispatcher.commands "
                        "SET consumed_at = now() "
                        "WHERE command_id = %s",
                        (command_id,),
                    )
                self._conn.commit()
                self._log.info(
                    "daemon.command_consumed",
                    extra={
                        "event": "command_consumed",
                        "run_id": self._run_id,
                        "command_id": command_id,
                        "command": command,
                    },
                )
                consumed += 1
            except Exception as exc:
                self._log.exception(
                    "daemon.command_handler_failed",
                    extra={
                        "event": "command_handler_failed",
                        "run_id": self._run_id,
                        "command_id": command_id,
                        "command": command,
                        "detail": str(exc),
                    },
                )
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover — best-effort
                    pass
                # Record failure row so operator can see it.
                category = (
                    "invalid_command"
                    if isinstance(exc, CommandError)
                    else "handler_error"
                )
                self._write_failure(
                    agent_id=None,
                    category=category,
                    detected_by="scheduler",
                    details={
                        "command_id": command_id,
                        "command": command,
                        "detail": str(exc),
                    },
                )
        return consumed

    def _handle_start(self, cur: Any, payload: dict[str, Any]) -> None:
        """Handle ``start`` command — flip ``concurrency_cap`` from 0 to 1.

        Only applies when the current value is 0 (the killswitch state);
        if the cap is already > 0 this is a safe no-op (rowcount == 0).
        Also clears the in-memory ``_graceful_stop_requested`` and
        ``_force_stop_requested`` flags so a prior ``stop`` / ``force_stop``
        does not leak into the new cycle (#2884).
        """
        cur.execute(
            "UPDATE dispatcher.config "
            "SET value = '1', updated_at = now(), updated_by = 'daemon' "
            "WHERE key = 'concurrency_cap' AND value::int = 0",
        )
        # Clear #2884 stop-intent flags. scheduler_tick will also clear
        # _pause_requested on the next cap>0 observation via the
        # existing #2847 machinery — doing it here is belt-and-braces.
        self._graceful_stop_requested = False
        self._force_stop_requested = False
        self._log.info(
            "daemon.command_start_applied",
            extra={
                "event": "command_start_applied",
                "run_id": self._run_id,
                "rows_updated": cur.rowcount,
            },
        )

    def _handle_stop(self, cur: Any, payload: dict[str, Any]) -> None:
        """Handle ``stop`` command — graceful stop (#2884).

        Sets ``concurrency_cap`` to 0 so no new agents spawn, and marks
        the in-memory ``_graceful_stop_requested`` flag so the next
        ``scheduler_tick`` does NOT engage the killswitch
        (``_pause_requested``) on its cap=0 observation. Any in-flight
        orchestration worker thread therefore keeps running through
        its current phase pipeline and completes normally — graceful
        stop = block new work but don't yank the rug from under
        existing work.

        Replaces the former ``drain`` command (same SQL, clearer
        semantic boundary vs. ``force_stop``). Replaces the former
        ``stop`` command (which mapped to the immediate-abort
        semantic — that role now belongs to ``force_stop``).
        """
        # Set the flag BEFORE the UPDATE so there is no brief window
        # where scheduler_tick could observe cap=0 without seeing the
        # graceful-stop intent. Worst case: an extra scheduler_tick
        # observes cap=0 with the flag already set, which is harmless.
        self._graceful_stop_requested = True
        self._force_stop_requested = False
        cur.execute(
            "UPDATE dispatcher.config "
            "SET value = '0', updated_at = now(), updated_by = 'daemon' "
            "WHERE key = 'concurrency_cap'",
        )
        self._log.info(
            "daemon.command_stop_applied",
            extra={
                "event": "command_stop_applied",
                "run_id": self._run_id,
                "rows_updated": cur.rowcount,
                "mode": "graceful",
            },
        )

    def _handle_force_stop(self, cur: Any, payload: dict[str, Any]) -> None:
        """Handle ``force_stop`` command (#2884).

        Unified command with two behaviours, selected by payload:

        - **Global (no ``agentId``)** — set ``concurrency_cap`` to 0
          AND immediately set ``_pause_requested`` so the worker
          thread aborts at the next phase boundary via
          ``_check_killswitch_and_abort``. Also set
          ``_force_stop_requested = True`` so the terminal phase is
          marked ``force_stopped`` (distinct from the generic
          ``paused_by_killswitch`` used when cap=0 is observed by
          any other means, e.g. a direct config edit).

        - **Per-agent (``payload['agentId']`` present)** — kill just
          that agent: update its ``status`` to ``crashed``, set
          ``ended_at`` to now(), and SIGKILL the pid if it is local.
          Does NOT touch ``concurrency_cap`` or any global flag —
          other in-flight agents and new claims are unaffected.
          Replaces the former ``force_kill`` command.
        """
        agent_id = payload.get("agentId")

        if agent_id:
            # Per-agent force_stop — narrow scope, no global effects.
            cur.execute(
                "SELECT pid FROM dispatcher.agents WHERE agent_id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise CommandError(
                    f"force_stop: agent {agent_id!r} not found in dispatcher.agents"
                )
            pid = int(row[0]) if row[0] is not None else None

            cur.execute(
                "UPDATE dispatcher.agents "
                "SET status = 'crashed', ended_at = now() "
                "WHERE agent_id = %s",
                (agent_id,),
            )

            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                    self._log.info(
                        "daemon.force_stop_signal_sent",
                        extra={
                            "event": "force_stop_signal_sent",
                            "run_id": self._run_id,
                            "agent_id": agent_id,
                            "pid": pid,
                        },
                    )
                except ProcessLookupError:
                    # Process already gone — not an error.
                    pass
                except PermissionError:
                    # Foreign host pid — not an error either.
                    pass

            self._log.info(
                "daemon.command_force_stop_agent_applied",
                extra={
                    "event": "command_force_stop_agent_applied",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pid": pid,
                },
            )
            return

        # Global force_stop — cap=0 + engage killswitch immediately.
        # Clear the graceful flag so a pending graceful stop doesn't
        # override the force-stop intent.
        self._graceful_stop_requested = False
        self._force_stop_requested = True
        cur.execute(
            "UPDATE dispatcher.config "
            "SET value = '0', updated_at = now(), updated_by = 'daemon' "
            "WHERE key = 'concurrency_cap'",
        )
        # Engage the killswitch immediately — don't wait for the next
        # scheduler_tick to observe cap=0. The orchestration worker
        # checks this event before each phase and aborts; see
        # :meth:`_check_killswitch_and_abort`.
        self._pause_requested.set()
        self._log.info(
            "daemon.command_force_stop_applied",
            extra={
                "event": "command_force_stop_applied",
                "run_id": self._run_id,
                "rows_updated": cur.rowcount,
                "mode": "global",
            },
        )

    def _handle_retry(self, cur: Any, payload: dict[str, Any]) -> None:
        """Handle ``retry`` command — create a retry marker for a failed agent.

        Requires ``payload['agentId']``. Verifies the agent exists with
        ``status='failed'``; raises ``CommandError`` for any other status
        (including already-running, already-retrying, or non-existent).
        Flips the agent to ``status='retrying'`` and inserts a
        ``dispatcher.retry_markers`` row so ``_process_retry_markers``
        picks it up on the next supervisor tick.
        """
        agent_id = payload.get("agentId")
        if not agent_id:
            raise CommandError("retry command missing required payload.agentId")

        cur.execute(
            "SELECT status, retries_used FROM dispatcher.agents WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise CommandError(
                f"retry: agent {agent_id!r} not found in dispatcher.agents"
            )
        status = str(row[0])
        retries_used = int(row[1]) if row[1] is not None else 0
        if status != "failed":
            raise CommandError(
                f"retry: agent {agent_id!r} has status {status!r}; "
                "can only retry agents with status='failed'"
            )

        cur.execute(
            "UPDATE dispatcher.agents SET status = 'retrying' WHERE agent_id = %s",
            (agent_id,),
        )
        cur.execute(
            "INSERT INTO dispatcher.retry_markers "
            "    (agent_id, reason, attempt, retry_after_ts) "
            "VALUES (%s, 'operator_retry', %s, now())",
            (agent_id, retries_used + 1),
        )
        self._log.info(
            "daemon.command_retry_applied",
            extra={
                "event": "command_retry_applied",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "attempt": retries_used + 1,
            },
        )

    def _is_paused(self) -> bool:
        """Read ``dispatcher.config.paused`` and coerce to bool.

        Returns ``True`` when the ``paused`` key exists and its value
        is the JSON string ``'true'`` or boolean ``true``. Missing key,
        ``null``, ``'false'``, or any other value is treated as
        ``False`` so an absent row never blocks claims.

        Called from the claim gate in :meth:`scheduler_tick` before
        spawning an orchestration thread.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("paused",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return False

        if row is None or row[0] is None:
            return False
        raw = row[0]
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, bool):
                    return parsed
            except json.JSONDecodeError:
                pass
            # Handle bare strings 'true'/'false' without JSON quotes.
            return raw.lower() == "true"
        return False

    # ── orchestration worker thread (#2847) ────────────────────────────

    def _orchestration_thread_alive(self) -> bool:
        """Return True iff an orchestration worker thread is currently alive.

        Cheap — just checks the thread handle without acquiring the
        lock. Callers that need a consistent snapshot (e.g.
        :meth:`_maybe_spawn_orchestration_thread`) should take the
        lock first. Safe to call from any thread.
        """
        thread = self._orchestration_thread
        return thread is not None and thread.is_alive()

    def _maybe_spawn_orchestration_thread(self) -> bool:
        """Spawn the orchestration worker thread if the gate allows (#2847).

        Returns True iff this call started a new thread. Returns False
        when:
            * the prior thread is still alive (caller will log
              ``orchestration_in_progress``);
            * a dead thread handle is present and cleaned up.

        The lock scopes only the thread-handle read + swap — the
        orchestration work itself does not hold the lock. This lets
        ``scheduler_tick`` continue to run on its 30s cadence while
        the worker thread is mid-``subprocess.run`` for a multi-minute
        phase.

        The worker thread receives ``daemon=True`` so it cannot block
        process exit past :meth:`run_forever`'s join window — if a
        SIGTERM arrives mid-phase and the ``claude -p`` subprocess
        ignores cooperative pause, the thread dies when the process
        does.
        """
        with self._orchestration_thread_lock:
            thread = self._orchestration_thread
            if thread is not None and thread.is_alive():
                self._log.info(
                    "daemon.orchestration_in_progress",
                    extra={
                        "event": "orchestration_in_progress",
                        "run_id": self._run_id,
                        "thread_name": thread.name,
                    },
                )
                return False
            # Clean up any dead handle so next tick's ``is_alive`` check
            # does not re-log.
            if thread is not None and not thread.is_alive():
                self._orchestration_thread = None

            new_thread = threading.Thread(
                target=self._orchestration_worker_entry,
                name=f"orchestration-{self._scheduler_ticks + 1}",
                daemon=True,
            )
            self._orchestration_thread = new_thread

        # Start the thread outside the lock — ``Thread.start`` is cheap
        # but we do not want to hold the handle lock across the actual
        # kickoff, and the is_alive check above already committed to
        # owning the next slot.
        new_thread.start()
        self._log.info(
            "daemon.orchestration_spawned",
            extra={
                "event": "orchestration_spawned",
                "run_id": self._run_id,
                "thread_name": new_thread.name,
            },
        )
        return True

    def _orchestration_worker_entry(self) -> None:
        """Entry point for the orchestration worker thread (#2847).

        Opens a fresh psycopg connection, stashes it on
        ``self._thread_state.conn`` so every existing ``self._conn``
        reference inside the claim + phase helpers resolves to this
        thread's connection rather than the main thread's. Runs
        :meth:`_claim_and_orchestrate_one`. On any exception, logs a
        structured ``orchestration_worker_failed`` event — the daemon
        must stay up.

        The connection is always closed in the ``finally`` block. Even
        a lingering exception from a DB error or a ``ctrl+C`` during
        the ``claude -p`` subprocess cannot leak the connection or
        starve the connection pool on the Fargate task.
        """
        import psycopg  # noqa: PLC0415 — lazy import; matches ``connect()``

        worker_conn: Connection[Any] | None = None
        try:
            worker_conn = psycopg.connect(self._cfg.database_url, connect_timeout=10)
            worker_conn.autocommit = False
            self._thread_state.conn = worker_conn
            self._claim_and_orchestrate_one()
        except Exception:
            # Never let a thread crash leak out silently — emitting a
            # structured event here makes the failure observable in
            # CloudWatch even when the main tick already moved on.
            self._log.exception(
                "daemon.orchestration_worker_failed",
                extra={
                    "event": "orchestration_worker_failed",
                    "run_id": self._run_id,
                },
            )
        finally:
            self._thread_state.conn = None
            if worker_conn is not None:
                try:
                    worker_conn.close()
                except Exception:  # pragma: no cover — best-effort close
                    pass

    def _should_run_blocked_scan(self) -> bool:
        """Return True when this tick should run the blocked-list scan.

        Fires on the very first tick (so the admin page has a populated
        blocked panel immediately after daemon boot) and every
        :data:`BLOCKED_SCAN_EVERY_N_TICKS` ticks thereafter. Checked
        against the pre-increment :attr:`_scheduler_ticks` counter —
        the counter is incremented at the end of the tick, so inside
        the tick it reads as the index of the tick that just completed
        successfully. First tick → counter is 0 on entry; fires.
        """
        if self._scheduler_ticks == 0:
            return True
        # +1 because we haven't incremented yet — so when the counter
        # is currently 3, this is the 4th tick, which is when we want
        # to fire at N=4.
        return (self._scheduler_ticks + 1) % BLOCKED_SCAN_EVERY_N_TICKS == 0

    # ── queue scan (scheduler-tick step 3) ─────────────────────────────

    def _fetch_agent_ready_issues(self) -> list[dict[str, Any]]:
        """Call ``gh issue list`` to observe the ``agent/ready`` queue.

        Returns a list of issue dicts as parsed from ``gh``'s JSON output.
        Each dict has at minimum ``number``, ``title``, ``labels``,
        ``createdAt``. Filters out issues that also carry
        ``status/blocked`` (defensive — the label-filter flag on ``gh``
        already excludes them in practice, but a label name change on
        the server side should not silently inflate the queue count).

        Raises :class:`RuntimeError` on subprocess failure so the caller
        (``_scan_queue_and_snapshot``) can log + return -1 for the tick.

        This is a thin wrapper so tests can monkeypatch it cleanly.
        """
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            self._cfg.github_repo,
            "--label",
            "agent/ready",
            "--state",
            "open",
            "--json",
            "number,title,labels,createdAt",
            "--limit",
            str(QUEUE_SCAN_PAGE_LIMIT),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"gh CLI not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"gh issue list timed out after "
                f"{QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS}s"
            ) from exc

        if result.returncode != 0:
            # Never include stderr verbatim in the daemon's structured log
            # at info level — it may echo the PAT on auth errors. A short
            # prefix is safe and sufficient for triage.
            stderr_preview = (result.stderr or "").strip().splitlines()[:1]
            raise RuntimeError(
                f"gh issue list exit={result.returncode}: "
                f"{stderr_preview[0] if stderr_preview else '<no stderr>'}"
            )

        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh issue list returned invalid JSON: {exc}") from exc

        if not isinstance(issues, list):
            raise RuntimeError(
                f"gh issue list returned non-list JSON: {type(issues).__name__}"
            )

        # Defensive filter: drop rows that also carry ``status/blocked``
        # or ``status/in-progress``. The ``gh --label agent/ready`` call
        # returns issues that carry the label; a blocked or in-progress
        # issue that still has ``agent/ready`` attached (e.g. mid-
        # transition) should not inflate the queue depth because the
        # daemon would never spawn on it. ``status/in-progress`` is the
        # /task-skill↔daemon interlock signal — after #2927 the /task
        # subagent uses label-only coordination (add
        # ``status/in-progress`` BEFORE removing ``agent/ready``), so
        # the label here is the sole race-defense primitive. The
        # daemon's pre-claim recheck in ``_atomic_claim`` re-reads the
        # label set at claim time to close the ~100ms propagation race.
        filtered: list[dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            labels = issue.get("labels") or []
            label_names = {
                entry.get("name") for entry in labels if isinstance(entry, dict)
            }
            if "status/blocked" in label_names:
                continue
            if STATUS_IN_PROGRESS_LABEL in label_names:
                continue
            filtered.append(issue)
        return filtered

    def _scan_queue_and_snapshot(self) -> int:
        """One queue scan + ``dispatcher.queue_snapshots`` INSERT.

        Returns the observed queue depth, or ``-1`` if the scan failed.
        Exceptions are swallowed + logged so a transient GitHub outage
        does not crash the daemon.
        """
        assert self._conn is not None, "connect() must run before scanning"

        try:
            issues = self._fetch_agent_ready_issues()
        except RuntimeError as exc:
            # Daemon must survive GitHub API hiccups (§15). Log + return
            # -1 so the caller knows the scan failed without crashing.
            self._log.warning(
                "daemon.queue_scan_failed",
                extra={
                    "event": "queue_scan_failed",
                    "run_id": self._run_id,
                    "detail": str(exc),
                },
            )
            return -1

        issue_numbers: list[int] = []
        issues_enriched: list[dict[str, Any]] = []
        for issue in issues:
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            issue_numbers.append(number)
            issues_enriched.append(_normalize_issue_enrichment(issue))
        queue_depth = len(issue_numbers)

        # Persist the snapshot. One INSERT per tick; the table is
        # append-only and the daemon is a singleton, so no race.
        # ``issues_json`` is written alongside ``issue_numbers`` so the
        # API's admin-page resolvers can render titles/labels/createdAt
        # straight from the DB (issue #2820). The two columns MUST
        # describe the same issues in the same order — they are derived
        # from the same ``issues`` list here.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.queue_snapshots "
                    "    (observed_at, queue_depth, issue_numbers, "
                    "     issues_json, run_id) "
                    "VALUES (now(), %s, %s, %s::jsonb, %s)",
                    (
                        queue_depth,
                        issue_numbers,
                        json.dumps(issues_enriched),
                        self._run_id,
                    ),
                )
            self._conn.commit()
        except Exception:
            # DB failure on the snapshot insert is a tick-level failure;
            # log and rollback so the next tick's work is not poisoned.
            self._log.exception(
                "daemon.queue_snapshot_insert_failed",
                extra={
                    "event": "queue_snapshot_insert_failed",
                    "run_id": self._run_id,
                    "queue_depth": queue_depth,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return -1

        self._log.info(
            "daemon.queue_scan",
            extra={
                "event": "queue_scan",
                "run_id": self._run_id,
                "count": queue_depth,
                "issues": issue_numbers,
            },
        )
        return queue_depth

    # ── blocked-list scan (scheduler-tick step 3b; #2820) ───────────────

    def _fetch_blocked_issues(self) -> list[dict[str, Any]]:
        """Call ``gh issue list`` for the ``status/blocked`` slice.

        Returns a list of issue dicts with ``number, title, labels,
        createdAt, body``. ``body`` is included (unlike the queue scan)
        so the API can parse ``Blocked by #N`` lines without a
        secondary GitHub call — the admin page renders the blockers
        inline. Raises :class:`RuntimeError` on subprocess failure; the
        caller wraps + logs.

        Issue #2820. This is a thin wrapper so tests can monkeypatch it
        cleanly, matching :meth:`_fetch_agent_ready_issues`.
        """
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            self._cfg.github_repo,
            "--label",
            "status/blocked",
            "--state",
            "open",
            "--json",
            "number,title,labels,createdAt,body",
            "--limit",
            str(BLOCKED_SCAN_PAGE_LIMIT),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"gh CLI not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"gh issue list (blocked) timed out after "
                f"{QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS}s"
            ) from exc

        if result.returncode != 0:
            stderr_preview = (result.stderr or "").strip().splitlines()[:1]
            raise RuntimeError(
                f"gh issue list (blocked) exit={result.returncode}: "
                f"{stderr_preview[0] if stderr_preview else '<no stderr>'}"
            )

        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"gh issue list (blocked) returned invalid JSON: {exc}"
            ) from exc

        if not isinstance(issues, list):
            raise RuntimeError(
                f"gh issue list (blocked) returned non-list JSON: "
                f"{type(issues).__name__}"
            )

        filtered: list[dict[str, Any]] = []
        for issue in issues:
            if isinstance(issue, dict):
                filtered.append(issue)
        return filtered

    def _scan_blocked_and_snapshot(self) -> int:
        """One blocked-list scan + ``dispatcher.blocked_snapshots`` INSERT.

        Returns the observed blocked depth, or ``-1`` if the scan
        failed. Matches the error semantics of
        :meth:`_scan_queue_and_snapshot` — no exceptions escape to the
        scheduler tick. Issue #2820.
        """
        assert self._conn is not None, "connect() must run before scanning"

        try:
            issues = self._fetch_blocked_issues()
        except RuntimeError as exc:
            self._log.warning(
                "daemon.blocked_scan_failed",
                extra={
                    "event": "blocked_scan_failed",
                    "run_id": self._run_id,
                    "detail": str(exc),
                },
            )
            return -1

        issue_numbers: list[int] = []
        issues_enriched: list[dict[str, Any]] = []
        for issue in issues:
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            issue_numbers.append(number)
            issues_enriched.append(
                _normalize_issue_enrichment(issue, include_body=True)
            )
        blocked_depth = len(issue_numbers)

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.blocked_snapshots "
                    "    (observed_at, blocked_depth, issue_numbers, "
                    "     issues_json, run_id) "
                    "VALUES (now(), %s, %s, %s::jsonb, %s)",
                    (
                        blocked_depth,
                        issue_numbers,
                        json.dumps(issues_enriched),
                        self._run_id,
                    ),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.blocked_snapshot_insert_failed",
                extra={
                    "event": "blocked_snapshot_insert_failed",
                    "run_id": self._run_id,
                    "blocked_depth": blocked_depth,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return -1

        self._log.info(
            "daemon.blocked_scan",
            extra={
                "event": "blocked_scan",
                "run_id": self._run_id,
                "count": blocked_depth,
                "issues": issue_numbers,
            },
        )
        return blocked_depth

    # ── Phase 3A orchestration (scheduler-tick step 4) ──────────────────
    #
    # The happy-path orchestration flow (issue #2783):
    #
    #   1. Pick a trusted, never-attempted candidate issue from the most
    #      recent ``dispatcher.queue_snapshots`` row.
    #   2. Atomic claim via INSERT on ``dispatcher.agents`` — the
    #      partial UNIQUE INDEX (migration 25) catches races.
    #   3. Create a per-agent worktree at ``.claude/worktrees/agent-<id>``.
    #   4. Fetch the issue bundle from GitHub (body + comments +
    #      blocked_by) and write ``plan.json`` input.
    #   5. Spawn ``claude -p '/task-v2-plan <agent_id>'``. Parse output;
    #      write to ``dispatcher.phase_outputs`` + ``phase_transitions``.
    #   6. Branch on ``plan.go`` — ``false`` ends the agent as
    #      ``succeeded`` or ``failed`` depending on ``block_reason``.
    #   7. Write ``ralph.json`` input; spawn ralph; parse output.
    #      BLOCKED verdict ends as ``failed``.
    #   8. Write ``summary.json`` input; spawn summary; parse output.
    #   9. ``git add -A``, ``git commit``, ``git push`` on the
    #      per-agent branch.
    #  10. ``gh pr create`` with the summary-generated title + body;
    #      capture PR number into ``dispatcher.agents.pr_number``.
    #  11. Leave ``agents.status='running'``, ``phase='awaiting_ci'`` so
    #      Phase 3B knows where to pick up. (3B: CI watch + fix-ci +
    #      merge. 3C: retry markers. 3D: diagnoser. 3E: retro +
    #      cleanup + cap flip.)
    #
    # Subprocess model: in-container ``claude -p`` (spike 0.1). One at
    # a time per daemon (``concurrency_cap=1`` is the Phase 3
    # production value). Stdout+stderr captured to
    # ``{worktree}/tmp/claude-p-<phase>.log``.

    def _has_active_agent(self) -> bool:
        """Return True when an agent row is already ``running`` for this run.

        Phase 3 concurrency cap is 1 per daemon: no new claim while any
        existing agent is still in flight. Uses the
        ``idx_dispatcher_agents_running`` partial index.

        After #2927 the /task skill no longer writes to
        ``dispatcher.agents`` (label-only coordination), so every
        ``status='running'`` row here is daemon-owned by construction
        — no ``kind`` filter needed. Historical ``kind='task-skill'``
        rows the /task skill wrote pre-#2927 were cleaned up at
        deploy time (see #2927 cleanup commit).
        """
        assert self._conn is not None, "connect() must run before checking"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM dispatcher.agents WHERE status = 'running' LIMIT 1",
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.has_active_agent_failed",
                extra={
                    "event": "has_active_agent_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            # Fail closed — treat as "active agent" so we skip this tick
            # rather than double-spawn on a confused DB state.
            return True
        return row is not None

    def _latest_queue_snapshot_issues(self) -> list[int]:
        """Return issue numbers from the most recent queue snapshot, priority-sorted.

        Issue #2835. The snapshot is written by
        :meth:`_scan_queue_and_snapshot` earlier in the same tick, so
        this is almost always the list we just observed.

        Ordering: ``(priority_rank(labels) asc, createdAt asc)``. That
        puts ``priority/p0`` before ``p1`` before ``p2`` before ``p3``
        before unlabeled, with the older issue winning any tie (the
        "waiting longest" issue gets next pick). The daemon used to
        iterate the snapshot in ``gh issue list`` order — which is
        ``created_at desc`` by default — so a freshly-filed p2 would
        beat an older p0. With #2820's ``issues_json`` column now
        carrying per-issue labels + createdAt, we can derive the sort
        key without a second GitHub round-trip.

        Falls back to the raw ``issue_numbers`` column (unsorted) when
        ``issues_json`` is empty or malformed — e.g. pre-#2820 snapshot
        rows, or a future schema hiccup. Operator visibility is
        preserved by the log event's ``sorted`` field.

        Returns an empty list if there is no snapshot yet (first-tick
        edge case) or the read fails.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issues_json, issue_numbers "
                    "FROM dispatcher.queue_snapshots "
                    "ORDER BY observed_at DESC "
                    "LIMIT 1",
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.latest_snapshot_failed",
                extra={
                    "event": "latest_snapshot_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return []
        if row is None:
            return []
        issues_json, issue_numbers = row[0], row[1]

        # Primary path: sort by ``issues_json`` (per-issue labels +
        # createdAt). psycopg returns jsonb as a parsed python list of
        # dicts, but tests may stub with a JSON string — handle both.
        if isinstance(issues_json, str):
            try:
                issues_json = json.loads(issues_json)
            except json.JSONDecodeError:
                issues_json = None

        if isinstance(issues_json, list) and issues_json:
            candidates: list[dict[str, Any]] = [
                entry for entry in issues_json if isinstance(entry, dict)
            ]
            candidates.sort(
                key=lambda entry: (
                    _priority_rank(entry.get("labels")),
                    entry.get("createdAt") or "",
                )
            )
            sorted_numbers: list[int] = []
            for entry in candidates:
                number = entry.get("number")
                if isinstance(number, int):
                    sorted_numbers.append(number)
            return sorted_numbers

        # Fallback: pre-#2820 snapshot row with no ``issues_json``. Use
        # the raw ``issue_numbers`` array in whatever order it was
        # stored (no labels available to sort on without a GitHub
        # round-trip, which we explicitly don't want here — see #2835
        # rationale).
        if not isinstance(issue_numbers, list):
            return []
        return [int(n) for n in issue_numbers if isinstance(n, int)]

    def _latest_queue_snapshot_title_for(self, issue_number: int) -> str | None:
        """Return the title for ``issue_number`` from the latest snapshot.

        Reads ``dispatcher.queue_snapshots.issues_json`` (written by
        :meth:`_scan_queue_and_snapshot` earlier in the same tick) and
        picks out the title. Returns ``None`` when the issue is not in
        the snapshot, the snapshot has no JSON (pre-#2820 rows), or the
        read fails — the caller falls back to storing NULL in
        ``dispatcher.agents.issue_title``. Issue #2820.

        Using psycopg's native jsonb → python parsing: the cursor
        returns a python list of dicts for jsonb columns, so we scan
        in-Python without a round-trip ``->>`` query. The snapshot is
        always short (<200 rows) so the O(n) scan is cheap.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issues_json FROM dispatcher.queue_snapshots "
                    "ORDER BY observed_at DESC "
                    "LIMIT 1",
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.latest_snapshot_title_lookup_failed",
                extra={
                    "event": "latest_snapshot_title_lookup_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None:
            return None
        issues = row[0]
        # psycopg returns jsonb as the parsed python value. Tests may
        # also stub with a JSON string — handle both defensively.
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except json.JSONDecodeError:
                return None
        if not isinstance(issues, list):
            return None
        for entry in issues:
            if not isinstance(entry, dict):
                continue
            if entry.get("number") == issue_number:
                title = entry.get("title")
                if isinstance(title, str) and title:
                    return title
                return None
        return None

    def _latest_queue_snapshot_priority_for(self, issue_number: int) -> str | None:
        """Return the priority label for ``issue_number`` from the latest snapshot.

        Reads the same ``dispatcher.queue_snapshots.issues_json`` blob
        as :meth:`_latest_queue_snapshot_title_for` and picks the
        priority label out of the per-issue ``labels`` array. Returns
        ``'p0'`` | ``'p1'`` | ``'p2'`` | ``'p3'`` or None when the
        issue carries no ``priority/pN`` label, is absent from the
        snapshot, or the read fails.

        Written for the admin cockpit's table-unification pass (#2899).
        Keeping the read separate from :meth:`_latest_queue_snapshot_title_for`
        is deliberate: each call is one lightweight cursor round-trip
        (~1 ms against RDS), the two lookups happen once per claim, and
        a combined helper would force every future caller that only
        needs one field to fetch both.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issues_json FROM dispatcher.queue_snapshots "
                    "ORDER BY observed_at DESC "
                    "LIMIT 1",
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.latest_snapshot_priority_lookup_failed",
                extra={
                    "event": "latest_snapshot_priority_lookup_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None:
            return None
        issues = row[0]
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except json.JSONDecodeError:
                return None
        if not isinstance(issues, list):
            return None
        for entry in issues:
            if not isinstance(entry, dict):
                continue
            if entry.get("number") == issue_number:
                labels = entry.get("labels")
                return _extract_priority(labels)
        return None

    def _pick_candidate_issue(self, candidates: list[int]) -> int | None:
        """Pick the first candidate that is trusted and not already claimed.

        For each issue number in ``candidates`` (priority order as
        observed by the queue scan — the ``gh issue list`` default is
        already priority-sorted by the GitHub API):

        1. Skip if ``dispatcher.agents`` has a row for it with
           ``status IN ('running', 'retrying', 'succeeded')``. A
           ``failed`` or ``crashed`` row does NOT block re-claim via
           the partial UNIQUE INDEX — manual retry is a documented
           operator flow.
        2. **Per-issue cooldown (issue #2804):** skip if any
           ``dispatcher.agents`` row for this issue was created within
           the last :data:`FAILED_AGENT_COOLDOWN_SECONDS`. Guards
           against the failure-loop amplifier where a systemically-
           broken issue (e.g. permanently-impossible worktree create,
           flaky external dep) burns through dozens of agents per hour
           because the partial UNIQUE INDEX only blocks ``running`` /
           ``retrying``. The diagnoser gets a clean 60 min window to
           make a judgment call (escalate / close / un-fail) before
           the scheduler touches the issue again.
        3. Run the trust check (``scripts/check-issue-author.sh``). Skip
           on any non-zero exit.

        Returns the chosen issue number, or ``None`` if no candidate is
        eligible.
        """
        for issue_number in candidates:
            if self._issue_already_attempted(issue_number):
                self._log.info(
                    "daemon.candidate_skipped",
                    extra={
                        "event": "candidate_skipped",
                        "run_id": self._run_id,
                        "issue_number": issue_number,
                        "reason": "already_attempted",
                    },
                )
                continue
            if self._issue_in_cooldown(issue_number):
                self._log.info(
                    "daemon.candidate_skipped",
                    extra={
                        "event": "candidate_skipped",
                        "run_id": self._run_id,
                        "issue_number": issue_number,
                        "reason": "cooldown",
                        "cooldown_seconds": FAILED_AGENT_COOLDOWN_SECONDS,
                    },
                )
                continue
            if not self._issue_author_trusted(issue_number):
                self._log.info(
                    "daemon.candidate_skipped",
                    extra={
                        "event": "candidate_skipped",
                        "run_id": self._run_id,
                        "issue_number": issue_number,
                        "reason": "untrusted_author",
                    },
                )
                continue
            return issue_number
        return None

    def _issue_in_cooldown(self, issue_number: int) -> bool:
        """True if this issue's most recent ``dispatcher.agents`` row is fresh.

        Delegates to ``dispatcher.issue_cooldown_remaining_seconds`` (SQL
        function, migration 37).  Returns ``True`` when the function returns a
        positive integer (cooldown still running), ``False`` when it returns
        ``0`` (elapsed) or ``NULL`` (never attempted).

        Returns True (fail-closed — treat as in cooldown) on DB error
        to avoid the failure-loop amplification the cooldown exists to
        prevent.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT dispatcher.issue_cooldown_remaining_seconds(%s, %s) > 0",
                    (issue_number, FAILED_AGENT_COOLDOWN_SECONDS),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.cooldown_check_failed",
                extra={
                    "event": "cooldown_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            # Fail closed — treat as in cooldown on a DB hiccup so we
            # don't amplify a failure loop on top of a DB problem.
            return True
        # row[0] is True (in cooldown), False (elapsed), or None (NULL > 0
        # evaluates to NULL in SQL, which fetchone returns as Python None).
        # NULL means never attempted → not in cooldown.
        return bool(row[0]) if row and row[0] is not None else False

    def _issue_already_attempted(self, issue_number: int) -> bool:
        """True if ``dispatcher.agents`` has any active/succeeded row for this issue.

        Delegates to ``dispatcher.issue_has_active_agent`` (SQL function,
        migration 37).  The function mirrors :data:`ACTIVE_AGENT_STATUSES`
        (``running``, ``retrying``, ``succeeded``, ``needs_review``).

        The partial UNIQUE INDEX (migration 25) enforces uniqueness on
        ``running`` and ``retrying``. The extra ``succeeded`` / ``needs_review``
        check is so a successful prior run (not yet cleaned up) doesn't
        get double-processed before the PR merges.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT dispatcher.issue_has_active_agent(%s)",
                    (issue_number,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.issue_attempted_check_failed",
                extra={
                    "event": "issue_attempted_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            # Fail closed — pretend the issue is already attempted so
            # we don't double-claim on a DB hiccup.
            return True
        return bool(row[0]) if row else False

    def _issue_author_trusted(self, issue_number: int) -> bool:
        """Run ``scripts/check-issue-author.sh`` and return True iff exit 0.

        The script prints ``TRUSTED: ...`` on stdout for exit 0 and
        ``UNTRUSTED: ...`` for exit 1. Exit 2 indicates a transient API
        error — treat as untrusted (fail closed) and let the next tick
        retry.
        """
        cmd = [
            "scripts/check-issue-author.sh",
            str(issue_number),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            self._log.warning(
                "daemon.trust_check_missing",
                extra={
                    "event": "trust_check_missing",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            return False
        except subprocess.TimeoutExpired:
            self._log.warning(
                "daemon.trust_check_timeout",
                extra={
                    "event": "trust_check_timeout",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            return False
        if result.returncode == 0:
            return True
        self._log.info(
            "daemon.trust_check_rejected",
            extra={
                "event": "trust_check_rejected",
                "run_id": self._run_id,
                "issue_number": issue_number,
                "exit_code": result.returncode,
                "detail": _stderr_tail(result.stdout or result.stderr),
            },
        )
        return False

    def _atomic_claim(
        self,
        issue_number: int,
        agent_id: str,
        worktree_path: str,
        issue_title: str | None = None,
        priority: str | None = None,
    ) -> bool:
        """INSERT a new agent row; return True on success, False on race.

        The partial UNIQUE INDEX on
        ``dispatcher.agents (issue_number) WHERE status IN ('running',
        'retrying')`` (migration 25) turns a concurrent second daemon's
        INSERT into a ``psycopg.errors.UniqueViolation``. Catching that
        is the race-lost signal — do NOT pre-check + insert, which is
        not atomic.

        ``issue_title`` is optional (nullable in the schema — migration
        28, #2820). When provided, the daemon populates it at claim
        time from the enrichment stored in
        ``dispatcher.queue_snapshots.issues_json`` so the admin-page
        recent-completions panel can render a title after the agent has
        finished without a GitHub round-trip.

        ``priority`` is optional (nullable in the schema — migration
        33, #2899). Values are ``'p0'`` | ``'p1'`` | ``'p2'`` | ``'p3'``
        | None, parsed from the issue's label set at claim time. The
        admin cockpit's Active-agents and Recently-completed panels
        render this as a coloured badge; pre-migration-33 rows render
        an em-dash placeholder.
        """
        assert self._conn is not None, "connect() must run before claiming"

        # Import lazily so tests that run without psycopg installed can
        # still import the daemon module.
        import psycopg  # noqa: PLC0415 — lazy import

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.agents "
                    "    (agent_id, parent_run_id, kind, issue_number, "
                    "     issue_title, worktree_path, phase, status, "
                    "     priority) "
                    "VALUES (%s, %s, 'task', %s, %s, %s, 'claiming', "
                    "        'running', %s)",
                    (
                        agent_id,
                        self._run_id,
                        issue_number,
                        issue_title,
                        worktree_path,
                        priority,
                    ),
                )
            self._conn.commit()
        except psycopg.errors.UniqueViolation:
            # Another daemon claimed this issue first. Roll back and
            # return False so the caller skips to the next candidate.
            # Look up the existing row's ``kind`` so the log
            # distinguishes a daemon↔daemon race (``claim_lost``) from
            # a historical task-skill collision
            # (``already_claimed_by_task``). After #2927 the /task
            # skill no longer writes ``kind='task-skill'`` rows, so
            # the task-skill branch only fires if a pre-#2927
            # historical row is still active in ``dispatcher.agents``
            # — preserved as belt-and-suspenders for deploy-window
            # races and log-archaeology clarity.
            # Owner lookup is best-effort; failure to read it just
            # falls back to the generic log.
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            owner_kind = self._lookup_active_owner_kind(issue_number)
            if owner_kind == TASK_SKILL_KIND:
                self._log.info(
                    "daemon.candidate_skipped",
                    extra={
                        "event": "candidate_skipped",
                        "run_id": self._run_id,
                        "issue_number": issue_number,
                        "agent_id": agent_id,
                        "reason": "already_claimed_by_task",
                    },
                )
            else:
                self._log.info(
                    "daemon.claim_lost",
                    extra={
                        "event": "claim_lost",
                        "run_id": self._run_id,
                        "issue_number": issue_number,
                        "agent_id": agent_id,
                        "owner_kind": owner_kind,
                    },
                )
            return False
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            self._log.exception(
                "daemon.claim_failed",
                extra={
                    "event": "claim_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "agent_id": agent_id,
                },
            )
            return False

        # Atomic DB claim succeeded. Add the ``status/in-progress``
        # label so operators see the issue is being worked on AND so
        # the /task subagent (which uses label-only coordination
        # post-#2927) observes a claimed issue and bails out. For the
        # daemon the label is still only the human-visible signal —
        # the DB row is the atomic primitive for daemon↔daemon races.
        # Label-write failure is logged but does NOT roll back the DB
        # claim — ``_mark_agent_terminal`` will still remove the label
        # on completion even if the add failed (idempotent).
        self._gh_issue_add_labels(issue_number, [STATUS_IN_PROGRESS_LABEL])

        self._log.info(
            "daemon.claim_succeeded",
            extra={
                "event": "claim_succeeded",
                "run_id": self._run_id,
                "issue_number": issue_number,
                "agent_id": agent_id,
            },
        )
        return True

    def _lookup_active_owner_kind(self, issue_number: int) -> str | None:
        """Return the ``kind`` of the current active row for an issue, or None.

        Used by :meth:`_atomic_claim`'s UniqueViolation handler to
        distinguish daemon↔daemon races (``kind='task'``) from
        daemon↔/task-skill races (``kind='task-skill'``). Best-effort —
        returns None on DB error so the caller can fall back to the
        generic ``claim_lost`` log event rather than crashing the tick.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT kind FROM dispatcher.agents "
                    "WHERE issue_number = %s "
                    "  AND status IN ('running', 'retrying') "
                    "LIMIT 1",
                    (issue_number,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None:
            return None
        kind = row[0]
        if not isinstance(kind, str):
            return None
        return kind

    def _repo_root(self) -> Path:
        """Legacy "repo-shaped directory" used by non-git callers.

        Returns ``os.getcwd()``. The Dockerfile places the daemon code
        at ``/app/scripts/dispatcher/`` so the container CWD is ``/app``;
        for unit tests the CWD is the repo-root worktree. The daemon
        never ``chdir``s away from it.

        **Not** the git parent for worktree ops. Phase 3A (#2783)
        originally had ``_create_worktree`` run ``git -C <cwd> worktree
        add ...``, but the container's ``/app`` has no ``.git`` child
        and every worktree add failed at cap=1 cutover (#2804). The
        baseline clone lives at :data:`DEFAULT_BASELINE_REPO_ROOT`
        instead, and :meth:`_git_parent_root` returns that path when
        :attr:`DaemonConfig.baseline_repo_root` is set. This method is
        preserved for callers that need a repo-shaped directory for
        non-git purposes (tmp file paths, optional script lookups) —
        migrating every caller would be a larger refactor with the
        same operational outcome.
        """
        return Path(os.getcwd())

    def _git_parent_root(self) -> Path:
        """Absolute path of the git directory ``git -C`` should target.

        When ``baseline_repo_root`` is configured (production Fargate
        path — issue #2804), this is the daemon's baseline clone at
        :data:`DEFAULT_BASELINE_REPO_ROOT`. When unset, fall back to
        :meth:`_repo_root` (the legacy local-dev / unit-test path that
        uses ``os.getcwd()``).

        Separate from :meth:`_repo_root` because existing callers (the
        diagnoser, cleanup_worktree.sh invocations, etc.) already pass
        ``self._repo_root()`` as a generic "repo-shaped directory" —
        they do not all need to switch to the baseline clone. The git
        parent is the narrow concept "where worktree adds run from".
        """
        if self._cfg.baseline_repo_root is not None:
            return self._cfg.baseline_repo_root
        return self._repo_root()

    def _compute_worktree_path(self, short_id: str) -> Path:
        """Absolute path for a new per-agent worktree.

        Must match the path passed to ``git worktree add`` so the DB's
        ``dispatcher.agents.worktree_path`` row points at the same
        directory the supervisor / cleanup code later looks for
        (otherwise the worktree is "orphaned from the DB's perspective"
        — see spec §17 Risk 1).

        When ``baseline_repo_root`` is set, the worktree lives in the
        sibling ``worktrees/`` directory next to the baseline clone
        (``/var/lib/dispatcher/worktrees/agent-<short_id>``). When unset,
        fall back to the legacy ``<cwd>/.claude/worktrees/`` convention.
        """
        baseline = self._cfg.baseline_repo_root
        if baseline is not None:
            return baseline.parent / "worktrees" / f"agent-{short_id}"
        return self._repo_root() / WORKTREE_PARENT_DIR / f"agent-{short_id}"

    def _setup_git_credentials(self) -> None:
        """Run ``gh auth setup-git`` once so git uses the GITHUB_TOKEN.

        The Fargate task environment exposes a scoped PAT via
        ``GITHUB_TOKEN`` (spike 0.7 / #2700). ``gh auth setup-git``
        writes a credential-helper line to the container's git config
        that makes subsequent ``git clone`` / ``git fetch`` / ``git push``
        calls over HTTPS consult ``gh`` for credentials, which in turn
        reads ``GITHUB_TOKEN``. This is idempotent — re-running it is
        a no-op — so it is safe to call on every boot.

        Any failure here is logged but does NOT abort startup: the
        baseline clone may still succeed on a pre-auth'd container
        image or via cached credentials, and the ECS task restart loop
        will re-run this on the next boot if credentials genuinely are
        broken. Only the baseline clone / fetch failing is a fatal
        startup error.
        """
        cmd = ["gh", "auth", "setup-git"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_AUTH_SETUP_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            self._log.warning(
                "daemon.gh_setup_git_missing",
                extra={
                    "event": "gh_setup_git_missing",
                    "run_id": self._run_id,
                    "detail": "gh CLI not on PATH",
                },
            )
            return
        except subprocess.TimeoutExpired:
            self._log.warning(
                "daemon.gh_setup_git_timeout",
                extra={
                    "event": "gh_setup_git_timeout",
                    "run_id": self._run_id,
                },
            )
            return

        if result.returncode == 0:
            self._log.info(
                "daemon.gh_setup_git_ok",
                extra={
                    "event": "gh_setup_git_ok",
                    "run_id": self._run_id,
                },
            )
            return

        self._log.warning(
            "daemon.gh_setup_git_failed",
            extra={
                "event": "gh_setup_git_failed",
                "run_id": self._run_id,
                "exit_code": result.returncode,
                "detail": _stderr_tail(result.stderr or result.stdout),
            },
        )

    def ensure_required_labels(self) -> int:
        """Idempotently create labels the daemon's fallback paths depend on.

        Issue #2872 Bug D. The diagnoser's escalate path adds
        ``status/needs-human`` to flag an issue for an operator. If the
        label doesn't exist in the repo, ``gh issue edit --add-label``
        exits non-zero and the operator-visible signal is silently
        lost (the DB terminal still lands, so correctness is preserved,
        but nobody notices until the admin page is manually checked).

        This method runs once at boot and creates any missing labels
        using ``gh label create --force`` — idempotent: pre-existing
        labels with identical colour/description are no-ops, different
        ones are updated in place. Failures are logged but do not
        block startup; the diagnoser path still works (the DB write is
        authoritative), just without the GitHub-visible hint.

        Returns the number of labels the method attempted to create.
        """
        required = [
            (
                "status/needs-human",
                "B60205",
                "Diagnoser flagged — needs operator review",
            ),
        ]
        attempted = 0
        for name, colour, description in required:
            try:
                subprocess.run(
                    [
                        "gh",
                        "label",
                        "create",
                        name,
                        "--repo",
                        self._cfg.github_repo,
                        "--color",
                        colour,
                        "--description",
                        description,
                        "--force",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                    check=False,
                )
                attempted += 1
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                self._log.warning(
                    "daemon.ensure_label_failed",
                    extra={
                        "event": "ensure_label_failed",
                        "run_id": self._run_id,
                        "label": name,
                        "detail": str(exc),
                    },
                )
        self._log.info(
            "daemon.ensure_required_labels",
            extra={
                "event": "ensure_required_labels",
                "run_id": self._run_id,
                "labels_attempted": attempted,
            },
        )
        return attempted

    def recover_abandoned_agents(self) -> int:
        """On daemon boot, reclaim ``status='running'`` agents from prior runs.

        Issue #2872 Bug A. Before this method existed, a daemon restart
        mid-phase left the in-flight agent's row at ``status='running'``
        with stale ``phase_transitions`` entries. The new daemon had no
        explicit recovery path — instead, the supervisor's 30-minute
        ``stuck_timeout`` eventually caught the abandoned agent (correct
        intent), but the stale ``MAX(ts)`` made the retry loop cascade
        (see #2872 Bugs B+E). Even with those fixed, depending on
        stuck_timeout to reclaim restart-orphans is wrong: the agent's
        phase subprocess died with the old daemon, so the retry is
        knowable immediately — not 30 minutes later.

        Call contract:

        - Invoked ONCE at daemon startup, AFTER
          :meth:`check_lease_and_register_run` has registered the new
          ``dispatcher.runs`` row. Running this before the lease check
          is wrong — the lease check is what guarantees we are the
          sole daemon, and therefore authorized to reclaim running
          agents.
        - Finds every ``dispatcher.agents`` row with ``status='running'``
          whose ``parent_run_id`` differs from the current run (or is
          NULL). These are the abandoned agents.
        - For each: write a ``dispatcher.failures`` row with
          ``category='daemon_restart_abandoned'`` (a new tier-1
          auto-retry category — see :data:`AUTO_RETRY_CATEGORIES`),
          flip status to ``crashed``, set
          ``phase='daemon_restart_abandoned'``, and enqueue a retry
          marker. The standard retry flow then picks the agent up on
          the next supervisor tick with a fresh worktree.

        Returns the number of agents reclaimed. On any DB error the
        method logs and returns 0 — startup must not block on a
        recovery sweep failure. The existing stuck_timeout path is the
        backstop; a reclaim miss just means the agent gets the 30m
        treatment instead of the immediate one.
        """
        assert self._conn is not None, "connect() must run before recovery"
        assert self._run_id is not None, "register run before recovery"

        candidates: list[tuple[str, int | None, str | None]] = []
        try:
            with self._conn.cursor() as cur:
                # #2927: /task subagents no longer write to
                # ``dispatcher.agents`` (label-only coordination), so
                # every ``status='running'`` row found here is
                # daemon-owned. No ``kind`` filter needed.
                cur.execute(
                    "SELECT agent_id, issue_number, phase "
                    "FROM dispatcher.agents "
                    "WHERE status = 'running' "
                    "  AND (parent_run_id IS NULL "
                    "       OR parent_run_id <> %s)",
                    (self._run_id,),
                )
                for row in cur.fetchall():
                    candidates.append(
                        (
                            str(row[0]),
                            int(row[1]) if row[1] is not None else None,
                            str(row[2]) if row[2] is not None else None,
                        )
                    )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.recover_scan_failed",
                extra={
                    "event": "recover_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return 0

        if not candidates:
            self._log.info(
                "daemon.recover_no_abandoned",
                extra={
                    "event": "recover_no_abandoned",
                    "run_id": self._run_id,
                },
            )
            return 0

        reclaimed = 0
        for agent_id, issue_number, prior_phase in candidates:
            try:
                self._write_failure(
                    agent_id=agent_id,
                    category=FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
                    detected_by="boot_recovery",
                    details={
                        "prior_phase": prior_phase,
                        "issue_number": issue_number,
                        "new_run_id": self._run_id,
                    },
                )
                self._mark_agent_terminal(
                    agent_id,
                    status="crashed",
                    phase="daemon_restart_abandoned",
                    exit_code=None,
                    issue_number=issue_number,
                )
                self._create_retry_marker(
                    agent_id=agent_id,
                    reason=FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
                )
                self._log.warning(
                    "daemon.agent_recovered_from_restart",
                    extra={
                        "event": "agent_recovered_from_restart",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "issue_number": issue_number,
                        "prior_phase": prior_phase,
                    },
                )
                reclaimed += 1
            except Exception:
                self._log.exception(
                    "daemon.agent_recover_failed",
                    extra={
                        "event": "agent_recover_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                    },
                )
        return reclaimed

    def ensure_baseline_clone(self) -> None:
        """Clone or fetch the baseline git repo used for worktree creation.

        Issue #2804. The dispatcher container image (``Dockerfile.dispatcher``)
        does not bake in a git checkout; Phase 3A assumed the daemon's
        CWD already had a ``.git`` parent, which is false in Fargate. At
        boot the daemon must either clone ``judgemind/judgemind`` into
        :attr:`DaemonConfig.baseline_repo_root` (first deploy or
        ephemeral-storage wipe) or fetch ``origin/main`` (every
        subsequent boot on the same task, though Fargate restarts always
        start fresh — defensive either way).

        Only runs when ``baseline_repo_root`` is configured. Local dev
        and unit tests (where :meth:`_repo_root` points at an existing
        worktree) skip this entirely and leave git operations on
        whatever repo the developer is running inside.

        Raises :class:`RuntimeError` on subprocess failure so
        :func:`main` can log + exit 1 — the ECS task then restart-loops,
        which is the correct handling for a transient network blip.
        """
        baseline = self._cfg.baseline_repo_root
        if baseline is None:
            self._log.info(
                "daemon.baseline_clone_skipped",
                extra={
                    "event": "baseline_clone_skipped",
                    "run_id": self._run_id,
                    "detail": "baseline_repo_root unset (local-dev mode)",
                },
            )
            return

        self._setup_git_credentials()

        if (baseline / ".git").exists():
            # Existing clone — unshallow if needed, then fetch so
            # worktrees branch from up-to-date ``origin/main``. Prevents
            # "worktree branched from a stale main" bugs as the daemon
            # stays up across many merges.
            #
            # Issue #3039: pre-#3039 the baseline was cloned shallow
            # (``--depth=1 --no-tags``), which produced orphan-commit
            # PRs on the ralph "no-op SHIP" path (``git commit --amend``
            # against HEAD dropped the unreachable parent and ``gh pr
            # create`` failed with "no history in common with main").
            # Any container image built before the fix still has a
            # shallow on-disk baseline from a prior boot; unshallow it
            # here so the correctness fix applies without a full
            # re-clone or container rebuild.
            self._unshallow_baseline_if_needed()
            self._baseline_fetch_origin_main()
            self._log.info(
                "daemon.baseline_clone_ready",
                extra={
                    "event": "baseline_clone_ready",
                    "run_id": self._run_id,
                    "baseline_repo_root": str(baseline),
                    "action": "fetch",
                },
            )
            return

        # Fresh clone. Create the parent directory (e.g. /var/lib/dispatcher/)
        # so the sibling worktrees directory can also be created later.
        #
        # Issue #3039: DO NOT pass ``--depth=1`` / ``--no-tags`` here.
        # The shallow boundary commit has no accessible parent; a later
        # ``git commit --amend`` against that commit (ralph's no-op SHIP
        # path in :meth:`_push_and_open_pr`) produces an orphan root
        # commit and ``gh pr create`` fails with "no history in common
        # with main". A full clone of judgemind/judgemind is under 200MB
        # and finishes in ~30-60s on the ECS task cold path — tasks run
        # for hours, so the one-time cost is invisible and the
        # correctness win is absolute.
        baseline.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "git",
            "clone",
            BASELINE_CLONE_URL,
            str(baseline),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=BASELINE_CLONE_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"git CLI not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"git clone timed out after {BASELINE_CLONE_TIMEOUT_SECONDS}s"
            ) from exc

        if result.returncode != 0:
            stderr_preview = _stderr_tail(result.stderr)
            raise RuntimeError(f"git clone exit={result.returncode}: {stderr_preview}")

        self._log.info(
            "daemon.baseline_clone_ready",
            extra={
                "event": "baseline_clone_ready",
                "run_id": self._run_id,
                "baseline_repo_root": str(baseline),
                "action": "clone",
            },
        )

    def _baseline_fetch_origin_main(self) -> None:
        """Run ``git -C <baseline> fetch origin main``. Raise on failure.

        Called by :meth:`ensure_baseline_clone` on reboot and by
        :meth:`_create_worktree` before each ``git worktree add`` so
        the new worktree branches from up-to-date ``origin/main``.
        """
        baseline = self._cfg.baseline_repo_root
        if baseline is None:  # pragma: no cover — guarded by callers
            return

        cmd = ["git", "-C", str(baseline), "fetch", "origin", "main"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=BASELINE_FETCH_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"git CLI not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"git fetch timed out after {BASELINE_FETCH_TIMEOUT_SECONDS}s"
            ) from exc

        if result.returncode != 0:
            stderr_preview = _stderr_tail(result.stderr)
            raise RuntimeError(f"git fetch exit={result.returncode}: {stderr_preview}")

    def _unshallow_baseline_if_needed(self) -> None:
        """Upgrade a pre-existing shallow baseline to a full clone.

        Issue #3039. The pre-#3039 :meth:`ensure_baseline_clone` wrote
        shallow clones (``git clone --depth=1 --no-tags``). After the
        fix lands, any container image whose ephemeral storage still
        contains a shallow clone from a prior boot would continue to
        hit the orphan-commit bug. This method detects that state
        (``git rev-parse --is-shallow-repository == "true"``) and
        runs ``git fetch --unshallow origin main`` to upgrade it in
        place — no full re-clone, no container rebuild required.

        Best-effort. A failure here does NOT raise — the caller's
        :meth:`_baseline_fetch_origin_main` is still going to run and
        will surface any actual connectivity or auth problem. The
        worst case of a silent unshallow failure is that the next
        no-op SHIP continues to produce orphan commits (the pre-#3039
        status quo), which logs are already instrumented to detect.
        """
        baseline = self._cfg.baseline_repo_root
        if baseline is None:  # pragma: no cover — guarded by callers
            return

        is_shallow_cmd = [
            "git",
            "-C",
            str(baseline),
            "rev-parse",
            "--is-shallow-repository",
        ]
        try:
            probe = subprocess.run(
                is_shallow_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self._log.warning(
                "daemon.baseline_unshallow_probe_failed",
                extra={
                    "event": "baseline_unshallow_probe_failed",
                    "run_id": self._run_id,
                    "baseline_repo_root": str(baseline),
                    "detail": str(exc),
                },
            )
            return

        if probe.returncode != 0:
            self._log.warning(
                "daemon.baseline_unshallow_probe_failed",
                extra={
                    "event": "baseline_unshallow_probe_failed",
                    "run_id": self._run_id,
                    "baseline_repo_root": str(baseline),
                    "exit_code": probe.returncode,
                    "stderr_tail": _stderr_tail(probe.stderr),
                },
            )
            return

        if (probe.stdout or "").strip() != "true":
            # Already a full clone — nothing to do. No event; the
            # common case should stay quiet.
            return

        unshallow_cmd = [
            "git",
            "-C",
            str(baseline),
            "fetch",
            "--unshallow",
            "origin",
            "main",
        ]
        try:
            result = subprocess.run(
                unshallow_cmd,
                capture_output=True,
                text=True,
                timeout=BASELINE_CLONE_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self._log.warning(
                "daemon.baseline_unshallow_failed",
                extra={
                    "event": "baseline_unshallow_failed",
                    "run_id": self._run_id,
                    "baseline_repo_root": str(baseline),
                    "detail": str(exc),
                },
            )
            return

        if result.returncode != 0:
            self._log.warning(
                "daemon.baseline_unshallow_failed",
                extra={
                    "event": "baseline_unshallow_failed",
                    "run_id": self._run_id,
                    "baseline_repo_root": str(baseline),
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return

        self._log.info(
            "daemon.baseline_unshallowed",
            extra={
                "event": "baseline_unshallowed",
                "run_id": self._run_id,
                "baseline_repo_root": str(baseline),
            },
        )

    def _create_worktree(self, agent_id: str) -> Path:
        """``git worktree add`` a fresh worktree + branch for this agent.

        Returns the absolute path to the new worktree. Raises
        :class:`RuntimeError` on subprocess failure so the caller can
        mark the agent failed.

        When ``baseline_repo_root`` is configured (production Fargate
        mode — issue #2804), runs ``git -C <baseline> fetch origin main``
        first so the new worktree branches from the freshest possible
        ``origin/main``; then invokes ``git -C <baseline> worktree add``
        with an absolute worktree path under
        :data:`DEFAULT_BASELINE_REPO_ROOT`'s sibling ``worktrees/`` dir.
        In the legacy local-dev / unit-test mode (``baseline_repo_root``
        unset), runs ``git -C <cwd> worktree add`` against
        ``.claude/worktrees/`` as before.
        """
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        git_parent = self._git_parent_root()
        worktree_path = self._compute_worktree_path(short_id)
        branch = f"agent/{short_id}"

        # In baseline-clone mode, refresh ``origin/main`` before
        # branching. Any fetch failure bubbles up as a RuntimeError so
        # the caller marks the agent failed — consistent with the
        # existing worktree-add failure path.
        if self._cfg.baseline_repo_root is not None:
            self._baseline_fetch_origin_main()

        # Defensive branch-delete — the tier-1 retry path (Phase 3C,
        # #2791) re-runs ``_create_worktree`` against the same agent_id
        # after a prior attempt's subprocess_crash. ``agent/<short_id>``
        # is derived from agent_id so the retry would collide with the
        # branch left behind by attempt 1 (`fatal: a branch named
        # 'agent/<short_id>' already exists`). Deleting the branch first
        # makes ``worktree add -b`` idempotent regardless of attempt
        # number (#2821). Exit code is intentionally ignored: ``git
        # branch -D`` returns 1 when the branch doesn't exist, which is
        # the happy case on first attempt. The 10s timeout guards
        # against a wedged git process without blocking normal flow.
        subprocess.run(
            [
                "git",
                "-C",
                str(git_parent),
                "branch",
                "-D",
                branch,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        cmd = [
            "git",
            "-C",
            str(git_parent),
            "worktree",
            "add",
            str(worktree_path),
            "-b",
            branch,
            "origin/main",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"git CLI not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("git worktree add timed out after 60s") from exc

        if result.returncode != 0:
            stderr_preview = _stderr_tail(result.stderr)
            raise RuntimeError(
                f"git worktree add exit={result.returncode}: {stderr_preview}"
            )

        self._log.info(
            "daemon.worktree_created",
            extra={
                "event": "worktree_created",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "worktree_path": str(worktree_path),
                "branch": branch,
            },
        )

        # Swap in the Fargate-narrow preflight hook in the worktree (issue
        # #2982). Only runs in Fargate mode (baseline_repo_root set) — local
        # dev keeps the operator-local hook so developers can still catch
        # prompt-stalling patterns before the code lands. Failures are logged
        # but non-fatal; if the swap can't run (e.g. the staged hook file is
        # missing on a rogue local image), the operator-local hook just stays
        # in place and the narrower rule set simply isn't active.
        if self._cfg.baseline_repo_root is not None:
            self._install_fargate_preflight_hook(worktree_path, agent_id)

        return worktree_path

    def _install_fargate_preflight_hook(
        self, worktree_path: Path, agent_id: str
    ) -> None:
        """Copy the narrowed Fargate preflight hook over the worktree's tracked
        ``.claude/hooks/preflight-bash.sh`` and mark it ``skip-worktree`` so
        git ignores the divergence.

        Must be called AFTER :meth:`_create_worktree` has run ``git worktree
        add`` successfully — the tracked hook file must exist on disk before
        we overwrite it. See issue #2982 for the full motivation.

        The staged source files live at ``$DISPATCHER_FARGATE_HOOKS_DIR/`` (set
        by ``Dockerfile.dispatcher`` to ``/app/fargate-hooks``). If the env
        var is unset, or the source files are missing, this is a no-op +
        warning — the operator-local hook stays in place, matching the local
        dev experience.
        """
        stage_dir_env = os.environ.get("DISPATCHER_FARGATE_HOOKS_DIR")
        if not stage_dir_env:
            self._log.warning(
                "daemon.fargate_hook_skip",
                extra={
                    "event": "fargate_hook_skip",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "reason": "DISPATCHER_FARGATE_HOOKS_DIR unset",
                },
            )
            return

        stage_dir = Path(stage_dir_env)
        source_hook = stage_dir / "preflight-bash.sh"
        source_helper = stage_dir / "preflight_cross_worktree.py"
        if not source_hook.exists() or not source_helper.exists():
            self._log.warning(
                "daemon.fargate_hook_skip",
                extra={
                    "event": "fargate_hook_skip",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "reason": "stage files missing",
                    "stage_dir": str(stage_dir),
                },
            )
            return

        hooks_dir = worktree_path / ".claude" / "hooks"
        target_hook = hooks_dir / "preflight-bash.sh"
        target_helper = hooks_dir / "preflight_cross_worktree.py"

        # ``git worktree add`` always recreates the tracked ``.claude/hooks/``
        # tree, so the target paths will exist unless the branch being checked
        # out diverges from main in that area. Defensive mkdir covers the
        # divergence case without making the common case slower.
        hooks_dir.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copyfile(source_hook, target_hook)
            shutil.copymode(source_hook, target_hook)
            shutil.copyfile(source_helper, target_helper)
            shutil.copymode(source_helper, target_helper)
        except OSError as exc:
            self._log.warning(
                "daemon.fargate_hook_copy_failed",
                extra={
                    "event": "fargate_hook_copy_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "error": str(exc),
                },
            )
            return

        # ``git update-index --skip-worktree`` tells git to pretend the file
        # is unchanged even when it differs from the index. Without this, the
        # diff would show up in every ralph Step 2.5 pre-push run, in
        # ``git status``, and in the PR diff (which would fail the paths-
        # filter gate on .claude/hooks/ and potentially replace the
        # operator-local hook on merge). --skip-worktree is the correct tool
        # here: it's per-worktree (not per-clone like --assume-unchanged),
        # and git documents it as the "intentional local divergence" knob.
        for target in (target_hook, target_helper):
            # Repo-relative path for the update-index call.
            rel = target.relative_to(worktree_path)
            try:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(worktree_path),
                        "update-index",
                        "--skip-worktree",
                        str(rel),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                # Non-fatal — the hook file is still swapped; the ``git
                # status`` / pre-push / PR diff noise is the only regression.
                self._log.warning(
                    "daemon.fargate_hook_skip_worktree_failed",
                    extra={
                        "event": "fargate_hook_skip_worktree_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "path": str(rel),
                        "error": str(exc),
                    },
                )

        self._log.info(
            "daemon.fargate_hook_installed",
            extra={
                "event": "fargate_hook_installed",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "worktree_path": str(worktree_path),
            },
        )

    def _fetch_issue_bundle(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue body, comments, labels for the plan phase input.

        Returns a dict shaped like the ``/task-v2-plan`` SKILL.md input
        contract. Raises :class:`RuntimeError` on subprocess failure.

        The returned dict includes ``issue_updated_at`` — the ISO-8601
        ``updatedAt`` timestamp from GitHub. :meth:`_try_reuse_prior_plan`
        uses this to detect whether the issue body was edited after the
        prior plan ran and therefore whether the cached plan is still
        valid (#2937).
        """
        cmd = [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--json",
            "number,title,body,labels,comments,updatedAt",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"gh CLI not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("gh issue view timed out after 30s") from exc

        if result.returncode != 0:
            stderr_preview = _stderr_tail(result.stderr)
            raise RuntimeError(
                f"gh issue view exit={result.returncode}: {stderr_preview}"
            )

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh issue view returned invalid JSON: {exc}") from exc

        # Filter bot comments — the plan skill expects "non-bot authors".
        raw_comments = payload.get("comments") or []
        filtered_comments: list[dict[str, Any]] = []
        for comment in raw_comments:
            if not isinstance(comment, dict):
                continue
            author = comment.get("author") or {}
            login = author.get("login", "") if isinstance(author, dict) else ""
            if login.endswith("[bot]"):
                continue
            filtered_comments.append(
                {
                    "author": login,
                    "author_association": comment.get("authorAssociation", ""),
                    "date": comment.get("createdAt", ""),
                    "body": comment.get("body", ""),
                }
            )

        labels = [
            entry.get("name", "")
            for entry in (payload.get("labels") or [])
            if isinstance(entry, dict)
        ]

        blocked_by = self._parse_blocked_by(payload.get("body") or "")
        parent_issue = self._parse_parent_issue(payload.get("body") or "")

        return {
            "issue_number": issue_number,
            "issue_title": payload.get("title", ""),
            "issue_body": payload.get("body", ""),
            "issue_comments": filtered_comments,
            "issue_labels": labels,
            "blocked_by": blocked_by,
            "parent_issue": parent_issue,
            # ISO-8601 string — used by _try_reuse_prior_plan to detect
            # post-plan issue edits that invalidate the cached plan (#2937).
            "issue_updated_at": payload.get("updatedAt", ""),
        }

    @staticmethod
    def _parse_blocked_by(body: str) -> list[int]:
        """Extract ``Blocked by #N`` references from an issue body.

        Matches the same convention ``scripts/unblock-dependents.sh``
        uses: one or more ``Blocked by #N`` lines, case-insensitive,
        anywhere in the body.
        """
        import re  # noqa: PLC0415 — lazy import

        matches = re.findall(r"(?im)^\s*blocked by\s+#(\d+)\s*$", body)
        return [int(m) for m in matches]

    @staticmethod
    def _parse_parent_issue(body: str) -> int | None:
        """Extract the first ``Parent: #N`` reference, or None."""
        import re  # noqa: PLC0415

        match = re.search(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$", body)
        return int(match.group(1)) if match else None

    def _write_phase_input(
        self,
        worktree: Path,
        phase: str,
        payload: dict[str, Any],
    ) -> Path:
        """Write ``{worktree}/tmp/dispatcher-input/<phase>.json``.

        Creates parent dirs. Returns the absolute path for logging.
        """
        input_dir = worktree / "tmp" / "dispatcher-input"
        input_dir.mkdir(parents=True, exist_ok=True)
        input_path = input_dir / f"{phase}.json"
        input_path.write_text(json.dumps(payload, indent=2, default=str))
        return input_path

    def _read_phase_output(self, worktree: Path, phase: str) -> dict[str, Any] | None:
        """Read ``{worktree}/tmp/dispatcher-output/<phase>.json``.

        Returns the parsed JSON, or ``None`` if the file is missing /
        malformed (the caller treats that as a phase failure).
        """
        output_path = worktree / "tmp" / "dispatcher-output" / f"{phase}.json"
        if not output_path.exists():
            return None
        try:
            return json.loads(output_path.read_text())
        except json.JSONDecodeError:
            return None

    def _persist_phase_output(
        self,
        agent_id: str,
        phase: str,
        output_json: dict[str, Any],
        log_text: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """INSERT the phase's output into ``dispatcher.phase_outputs``
        and append a row to ``dispatcher.phase_transitions``.

        Both tables share a single transaction so the observed state
        stays consistent under partial failure.

        ``log_text`` (added in #2821, migration 27) persists the full
        ephemeral phase log body so operators can ``SELECT log_text FROM
        dispatcher.phase_outputs WHERE agent_id = '<uuid>' ORDER BY ts
        DESC`` at any time, even after the worktree has been cleaned up
        and the ephemeral ``{worktree}/tmp/claude-p-<phase>.log`` file
        deleted. Nullable for historical rows and for phases that
        completed with no log content to capture. Housekeeping tick
        (#2778/#2779) prunes rows at 30 days along with ``output_json``.

        ``attempt`` (added #2872, migration 30) is derived from the
        agent's current ``retries_used`` counter so second/third plan
        runs after retry reset produce distinct rows rather than
        colliding on the old ``(agent_id, phase)`` unique index. Legacy
        single-attempt callers saw the INSERT rolled back silently; now
        every retry's output is preserved and the admin-page phase log
        shows the full retry trail.

        ``usage`` (added #2869, migration 31) is the parsed
        ``{tokens_input, tokens_output, tokens_cache_read,
        tokens_cache_write, cost_usd, model_used}`` dict produced by
        :meth:`_parse_phase_usage`. All six fields are nullable. When
        ``usage`` is ``None`` (no metering signal — phase crashed before
        claude emitted its JSON envelope, or the envelope was malformed)
        every metering column is set to NULL and the INSERT still runs.
        """
        assert self._conn is not None, "connect() must run before persisting"
        attempt = self._current_attempt_for(agent_id)
        u = usage or {}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.phase_outputs "
                    "    (agent_id, phase, output_json, log_text, attempt, "
                    "     tokens_input, tokens_output, tokens_cache_read, "
                    "     tokens_cache_write, cost_usd, model_used) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (agent_id, phase, attempt) DO UPDATE SET "
                    "    output_json         = EXCLUDED.output_json, "
                    "    log_text            = EXCLUDED.log_text, "
                    "    tokens_input        = EXCLUDED.tokens_input, "
                    "    tokens_output       = EXCLUDED.tokens_output, "
                    "    tokens_cache_read   = EXCLUDED.tokens_cache_read, "
                    "    tokens_cache_write  = EXCLUDED.tokens_cache_write, "
                    "    cost_usd            = EXCLUDED.cost_usd, "
                    "    model_used          = EXCLUDED.model_used, "
                    "    ts                  = now()",
                    (
                        agent_id,
                        phase,
                        json.dumps(output_json, default=str),
                        log_text,
                        attempt,
                        u.get("tokens_input"),
                        u.get("tokens_output"),
                        u.get("tokens_cache_read"),
                        u.get("tokens_cache_write"),
                        u.get("cost_usd"),
                        u.get("model_used"),
                    ),
                )
                cur.execute(
                    "INSERT INTO dispatcher.phase_transitions "
                    "    (agent_id, phase) "
                    "VALUES (%s, %s)",
                    (agent_id, phase),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.persist_phase_output_failed",
                extra={
                    "event": "persist_phase_output_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": phase,
                    "attempt": attempt,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

    def _current_attempt_for(self, agent_id: str) -> int:
        """Return the current retry attempt number for an agent.

        Reads ``dispatcher.agents.retries_used`` — 0 on a fresh claim,
        bumps by 1 on each ``_process_retry_markers`` reset. Matches
        the semantic of ``dispatcher.phase_outputs.attempt`` (migration
        30): "retry number under which this phase ran". On read failure
        falls back to 0 — preferring "assume initial run" over blowing
        up the INSERT, because a wrong ``attempt`` in one row never
        cascades (each phase re-derives), whereas a failed INSERT loses
        the phase record.
        """
        assert self._conn is not None, "connect() must run before attempt read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT retries_used FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return 0
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):  # pragma: no cover — defensive
            return 0

    def _model_for_phase(self, phase: str, agent_id: str) -> str:
        """Return the ``--model`` value to hand to ``claude -p`` for *phase*.

        Most phases use the static :data:`PHASE_MODELS` entry. ``ralph``
        is attempt-aware (#2955): the final budgeted attempt upgrades
        to Opus via :func:`_ralph_model_for_attempt`. The 1-indexed
        attempt number is derived from :meth:`_current_attempt_for`
        (which returns ``retries_used``, 0-indexed) plus 1.

        Scope: ralph only. Plan is already Opus. Summary / fix-ci /
        verify / retro keep their static defaults.
        """
        if phase == "ralph":
            attempt_n = self._current_attempt_for(agent_id) + 1
            return _ralph_model_for_attempt(attempt_n)
        return PHASE_MODELS[phase]

    # ── ralph patch persistence (issue #3012) ──────────────────────────
    #
    # Persist ralph's SHIP'd diff to ``dispatcher.ralph_patches`` so a
    # daemon restart between SHIP and a successful ``gh pr create`` does
    # not lose the work. Three helpers:
    #
    #   - :meth:`_capture_and_persist_ralph_patch` runs after
    #     ``verdict=SHIP`` in :meth:`_run_ralph_phase`. It captures the
    #     patch with ``git format-patch -1 HEAD --stdout``, DELETEs any
    #     prior row for the same ``issue_number`` (supersede semantics),
    #     INSERTs the fresh row, and UPDATEs the matching ralph
    #     ``phase_outputs`` row's ``patch_id`` FK.
    #   - :meth:`_delete_ralph_patches_for_agent` runs after ``gh pr
    #     create`` succeeds in :meth:`_push_and_open_pr`. Once the
    #     branch is on origin, the postgres copy is redundant.
    #   - :meth:`_apply_prior_ralph_patch` runs at the top of
    #     :meth:`_run_ralph_phase` BEFORE ralph spawns. If a prior SHIP
    #     for the same issue exists in ``ralph_patches``, it tries to
    #     ``git am`` the patch onto the fresh worktree. On apply success,
    #     ralph iterates on top of the inherited diff. On apply failure,
    #     it aborts cleanly (``git am --abort``) and records the patch
    #     text so ralph can see it in ``prior_attempts.md``.
    #
    # All three are best-effort by design — a DB error does not fail the
    # agent, it just logs and falls through to the pre-#3012 behavior.
    # The feature is a latency/cost optimization layered on top of the
    # existing pipeline; it must not be able to wedge the pipeline.

    def _capture_and_persist_ralph_patch(
        self,
        agent_id: str,
        issue_number: int,
        worktree: Path,
    ) -> str | None:
        """Capture ralph's SHIP'd diff, persist to postgres, return patch_id.

        Runs at the end of :meth:`_run_ralph_phase` on ``verdict=SHIP``.
        The commit model after #2971 is "ralph commits directly, daemon
        amends" — so at SHIP time HEAD carries ralph's placeholder
        commit (``WIP: ralph output``) and the diff is trapped in that
        one commit. ``git format-patch -1 HEAD --stdout`` dumps the
        full patch (including the ``--- a/path`` / ``+++ b/path``
        hunks ``git am`` needs to replay).

        Supersedes any prior row for the same ``issue_number`` — covers
        the retry-after-failure case where a prior agent SHIPped but
        never got its PR created (c3a69458 @ 2026-04-21).

        On success, also UPDATEs the matching ``phase_outputs`` row's
        ``patch_id`` FK so the admin page and diagnoser can correlate
        the ralph SHIP row to its persisted patch.

        Returns the new ``patch_id`` on success, ``None`` on any
        failure (empty patch, git error, DB error). All failures are
        logged but non-fatal — the happy path continues in
        :meth:`_push_and_open_pr` regardless.
        """
        assert self._conn is not None, "connect() must run before persist"

        # Capture the patch via git format-patch -1 HEAD --stdout.
        # A 60s timeout is generous — format-patch is local-only and
        # typically finishes in <1s even on large worktrees.
        try:
            patch_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "format-patch",
                    "-1",
                    "HEAD",
                    "--stdout",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            self._log.warning(
                "daemon.ralph_patch_capture_failed",
                extra={
                    "event": "ralph_patch_capture_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return None
        if patch_result.returncode != 0 or not (patch_result.stdout or "").strip():
            # Empty or failed patch → nothing to persist. Empty is
            # legitimately possible if ralph's "SHIP" commit has no
            # tracked changes (shouldn't happen per the #2971 contract,
            # but don't blow up on it).
            self._log.info(
                "daemon.ralph_patch_empty_or_failed",
                extra={
                    "event": "ralph_patch_empty_or_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "exit_code": patch_result.returncode,
                    "stderr_tail": _stderr_tail(patch_result.stderr),
                },
            )
            return None
        patch_content = patch_result.stdout

        # Best-effort HEAD SHA — informational only, NULL on failure.
        commit_sha: str | None = None
        try:
            sha_result = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if sha_result.returncode == 0:
                candidate = (sha_result.stdout or "").strip()
                if candidate:
                    commit_sha = candidate
        except Exception:  # pragma: no cover — defensive
            pass

        # Supersede + INSERT + UPDATE phase_outputs.patch_id in a single
        # transaction so observers never see a mid-state (stale row
        # deleted but new row not yet inserted). ``cur.fetchone()``
        # after the INSERT retrieves the generated ``patch_id``; we
        # bind that to both the phase_outputs UPDATE and the return
        # value.
        #
        # Note on verdict column (#3026): this helper runs on the SHIP
        # path, so the inserted row carries ``verdict='SHIP'`` with
        # ``iteration_n=NULL``. Per-iteration intermediate rows (written
        # by :meth:`_persist_ralph_iteration_patch`) carry the actual
        # iteration number and verdict. The DELETE-by-issue_number
        # supersede here cleans up all intermediate rows for the same
        # issue, so the post-SHIP table state has exactly one row for
        # this agent/issue (the authoritative SHIP row).
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM dispatcher.ralph_patches WHERE issue_number = %s",
                    (issue_number,),
                )
                cur.execute(
                    "INSERT INTO dispatcher.ralph_patches "
                    "    (agent_id, issue_number, patch_content, commit_sha, "
                    "     iteration_n, verdict) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "RETURNING patch_id",
                    (agent_id, issue_number, patch_content, commit_sha, None, "SHIP"),
                )
                row = cur.fetchone()
                patch_id = str(row[0]) if row and row[0] else None
                if patch_id is not None:
                    cur.execute(
                        "UPDATE dispatcher.phase_outputs "
                        "SET patch_id = %s "
                        "WHERE agent_id = %s AND phase = 'ralph'",
                        (patch_id, agent_id),
                    )
            self._conn.commit()
        except Exception as exc:
            self._log.exception(
                "daemon.ralph_patch_persist_failed",
                extra={
                    "event": "ralph_patch_persist_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        self._log.info(
            "daemon.ralph_patch_persisted",
            extra={
                "event": "ralph_patch_persisted",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "patch_id": patch_id,
                "commit_sha": commit_sha,
                "patch_bytes": len(patch_content),
            },
        )
        return patch_id

    def _delete_ralph_patches_for_agent(self, agent_id: str) -> int:
        """DELETE this agent's ``ralph_patches`` row after ``gh pr create``.

        Runs at the end of :meth:`_push_and_open_pr` on a successful PR
        open. Once the branch is on origin and the PR exists, the
        postgres copy is redundant — origin/<branch> is the durable
        source. Targeting by ``agent_id`` (rather than ``issue_number``)
        ensures a racing second claim on the same issue won't clobber
        a newer agent's patch: the row we just wrote carries this
        agent's id.

        Returns the row count deleted. A value of 0 is fine — it just
        means we're on the fallback path (patch capture earlier failed,
        no row to clean up). A DB error logs + rolls back; the PR is
        already open so we don't block the happy path on cleanup.
        """
        assert self._conn is not None, "connect() must run before delete"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM dispatcher.ralph_patches WHERE agent_id = %s",
                    (agent_id,),
                )
                deleted = cur.rowcount or 0
            self._conn.commit()
        except Exception as exc:
            self._log.exception(
                "daemon.ralph_patch_delete_failed",
                extra={
                    "event": "ralph_patch_delete_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return 0

        self._log.info(
            "daemon.ralph_patch_deleted",
            extra={
                "event": "ralph_patch_deleted",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "rows_deleted": deleted,
            },
        )
        return deleted

    def _apply_prior_ralph_patch(
        self,
        agent_id: str,
        issue_number: int,
        worktree: Path,
    ) -> dict[str, Any] | None:
        """Apply a prior ralph patch to the fresh worktree, if one exists.

        Called at the top of :meth:`_run_ralph_phase` before ralph is
        spawned. Runs the **unified resume lookup** (#3026): the most
        recent patch for ``issue_number``, any ``agent_id``, any
        ``verdict``, within the 7-day TTL. This is the single code path
        for both same-agent resume (daemon restart mid-ralph) and
        cross-agent resume (fresh claim on an issue whose prior
        attempts were abandoned).

        1. Queries the latest ``ralph_patches`` row for ``issue_number``
           within the 7-day TTL, ordered by ``created_at DESC``.
        2. If none exists → returns ``None`` (no-op first-attempt path).
        3. If one exists, writes the patch to
           ``{worktree}/tmp/dispatcher-input/prior-ralph.patch`` and
           tries ``git am --3way <patchfile>``.
        4. On apply success → returns
           ``{"applied": True, "patch_id": ..., "commit_sha": ...,
             "source_agent_id": ..., "iteration_n": ..., "verdict": ...,
             "bytes": ...}``.
           Ralph starts with the prior diff already on HEAD and iterates
           on top.
        5. On apply **conflict** → **does NOT abort** (issue #3026
           conflict-handoff contract). The worktree is left in the
           conflicted am-in-progress state (``.git/rebase-apply/``
           intact, unmerged index entries) so ralph can inspect and
           decide whether to ``git am --continue`` or ``git am --abort``.
           Returns
           ``{"applied": False, "conflicted": True, "patch_id": ...,
             "patch_content": ..., "source_agent_id": ...,
             "iteration_n": ..., "verdict": ..., "age_seconds": ...,
             "conflict_files": [...], "reason": ...}``
           so the caller can build the "RESUME WITH CONFLICT" prompt
           block that goes into ralph's task.md.

        A DB lookup failure returns ``None`` — same effect as "no prior
        patch" — so a transient postgres hiccup never wedges the agent.

        Same-agent-resume note (#3026 supersedes #3013): the previous
        self-inherit guard is removed because the unified lookup
        semantics explicitly allow same-agent rows. Fresh worktrees are
        always at origin/main HEAD on ralph entry, so applying the
        agent's own last iteration is a valid resume of cumulative work.
        """
        assert self._conn is not None, "connect() must run before query"

        # Query the latest patch for this issue within the 7-day TTL.
        # The TTL predicate is redundant with the housekeeping sweep
        # (which runs on the same 7-day window) but keeps a partial-
        # sweep state from surfacing stale patches. Any agent_id, any
        # verdict — same-agent resume, cross-agent resume, legacy rows
        # from #3013 era all flow through this single lookup.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT patch_id, patch_content, commit_sha, agent_id, "
                    "       iteration_n, verdict, "
                    "       EXTRACT(EPOCH FROM (now() - created_at)) "
                    "FROM dispatcher.ralph_patches "
                    "WHERE issue_number = %s "
                    "  AND created_at > now() - make_interval(days => %s) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (issue_number, DEFAULT_RALPH_PATCH_RETENTION_DAYS),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception as exc:
            self._log.exception(
                "daemon.ralph_patch_query_failed",
                extra={
                    "event": "ralph_patch_query_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        if row is None:
            return None

        prior_patch_id = str(row[0])
        patch_content = row[1] or ""
        prior_commit_sha = row[2] or None
        prior_agent_id = str(row[3]) if row[3] is not None else None
        prior_iteration_n = row[4] if len(row) > 4 else None
        prior_verdict = row[5] if len(row) > 5 else None
        age_seconds_raw = row[6] if len(row) > 6 else None
        try:
            age_seconds = float(age_seconds_raw) if age_seconds_raw is not None else 0.0
        except (TypeError, ValueError):
            age_seconds = 0.0
        if not patch_content.strip():
            # Defensive — NOT NULL constraint guarantees non-null, but
            # the content could be an empty-ish blob from a pathological
            # capture. Treat as "no usable patch".
            return None

        # Write patch to a scratch file under {worktree}/tmp/.
        input_dir = worktree / "tmp" / "dispatcher-input"
        input_dir.mkdir(parents=True, exist_ok=True)
        patch_file = input_dir / "prior-ralph.patch"
        try:
            patch_file.write_text(patch_content, encoding="utf-8")
        except OSError as exc:
            self._log.warning(
                "daemon.ralph_patch_write_failed",
                extra={
                    "event": "ralph_patch_write_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return None

        # Try ``git am --3way <patchfile>``. --3way gives a better
        # chance of success when base has moved; without it, any drift
        # produces an apply failure even for non-overlapping hunks.
        try:
            am_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "am",
                    "--3way",
                    str(patch_file),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            self._log.warning(
                "daemon.ralph_patch_am_invocation_failed",
                extra={
                    "event": "ralph_patch_am_invocation_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "patch_id": prior_patch_id,
                    "detail": str(exc),
                },
            )
            # Best-effort abort in case am started but we lost the
            # process. Unlike the conflict path below, an invocation
            # failure leaves no structured am-in-progress state for
            # ralph to inspect, so aborting is safer than leaving
            # undefined git state.
            self._git_am_abort(worktree)
            return {
                "applied": False,
                "conflicted": False,
                "patch_id": prior_patch_id,
                "patch_content": patch_content,
                "source_agent_id": prior_agent_id,
                "iteration_n": prior_iteration_n,
                "verdict": prior_verdict,
                "age_seconds": age_seconds,
                "conflict_files": [],
                "reason": f"am invocation failed: {exc}",
            }

        if am_result.returncode == 0:
            self._log.info(
                "daemon.ralph_patch_applied",
                extra={
                    "event": "ralph_patch_applied",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "patch_id": prior_patch_id,
                    "prior_commit_sha": prior_commit_sha,
                    "source_agent_id": prior_agent_id,
                    "iteration_n": prior_iteration_n,
                    "verdict": prior_verdict,
                    "patch_bytes": len(patch_content),
                },
            )
            return {
                "applied": True,
                "patch_id": prior_patch_id,
                "commit_sha": prior_commit_sha,
                "source_agent_id": prior_agent_id,
                "iteration_n": prior_iteration_n,
                "verdict": prior_verdict,
                "age_seconds": age_seconds,
                "bytes": len(patch_content),
            }

        # Apply failed with a conflict. Per issue #3026, the daemon
        # does NOT ``git am --abort`` here — the worktree is left in
        # the conflicted am-in-progress state so ralph can inspect
        # the unmerged files, read the conflict markers, and decide
        # whether to ``git am --continue`` (resolve + keep prior work)
        # or ``git am --abort`` (discard + start fresh from main).
        #
        # The conflict-handoff contract is documented in
        # ``.claude/skills/task-v2-ralph/SKILL.md`` §"Resume with
        # conflict". The daemon surfaces the RESUME WITH CONFLICT
        # prompt block (built via ``_format_resume_with_conflict_block``)
        # in ralph's task.md; ralph's worker makes the judgment call.
        reason = _stderr_tail(am_result.stderr) or f"exit={am_result.returncode}"
        conflict_files = self._list_git_am_conflict_files(worktree)
        self._log.warning(
            "daemon.ralph_patch_apply_conflict",
            extra={
                "event": "ralph_patch_apply_conflict",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "patch_id": prior_patch_id,
                "source_agent_id": prior_agent_id,
                "iteration_n": prior_iteration_n,
                "verdict": prior_verdict,
                "exit_code": am_result.returncode,
                "stderr_tail": reason,
                "conflict_file_count": len(conflict_files),
            },
        )
        return {
            "applied": False,
            "conflicted": True,
            "patch_id": prior_patch_id,
            "patch_content": patch_content,
            "source_agent_id": prior_agent_id,
            "iteration_n": prior_iteration_n,
            "verdict": prior_verdict,
            "age_seconds": age_seconds,
            "conflict_files": conflict_files,
            "reason": reason,
        }

    def _git_am_abort(self, worktree: Path) -> None:
        """Best-effort ``git am --abort`` to restore a clean worktree.

        Ignores exit codes — the abort is itself allowed to fail
        (e.g. if there's no in-progress am), and we've already logged
        the underlying apply failure.
        """
        try:
            subprocess.run(
                ["git", "-C", str(worktree), "am", "--abort"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def _list_git_am_conflict_files(self, worktree: Path) -> list[str]:
        """Return the list of files in unmerged (conflict) state.

        Called on the conflict-handoff path of
        :meth:`_apply_prior_ralph_patch`. ``git diff --name-only
        --diff-filter=U`` lists paths with unmerged index entries —
        exactly the files ``git am`` marked as conflicts.

        Returns an empty list on subprocess failure so the caller's
        placeholder formatting uses ``K=0`` rather than raising. The
        conflict count is advisory in the RESUME WITH CONFLICT prompt
        block (it tells ralph how much work it's inheriting); a wrong
        count does not affect correctness.
        """
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "diff",
                    "--name-only",
                    "--diff-filter=U",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:  # pragma: no cover — defensive
            return []
        if result.returncode != 0:
            return []
        return [
            line.strip() for line in (result.stdout or "").splitlines() if line.strip()
        ]

    def _persist_ralph_iteration_patch(
        self,
        agent_id: str,
        issue_number: int,
        iteration_n: int,
        verdict: str,
        worktree: Path,
    ) -> str | None:
        """Persist a per-iteration ralph patch snapshot (#3026).

        Called by the daemon at end-of-iteration (via a ralph-invoked
        helper CLI, see ``scripts/dispatcher/persist_ralph_iteration.py``)
        after ralph has committed the iteration's work with the
        placeholder ``WIP: ralph output`` message (see
        ``.claude/skills/task-v2-ralph/SKILL.md`` §2.5a).

        Unlike :meth:`_capture_and_persist_ralph_patch` — which runs
        only on the SHIP verdict and supersedes prior rows via DELETE-
        by-issue-number — this helper is **additive**: it INSERTs a
        new row per iteration, keyed on (agent_id, iteration_n,
        verdict). Rows accumulate for the agent's lifetime so a mid-
        run daemon crash or cross-agent retry can replay the most
        recent intermediate state.

        The patch is captured via ``git format-patch origin/main..HEAD
        --stdout`` (full cumulative range, not ``-1 HEAD``) so a
        resume on a branch with multiple commits still replays
        everything. Matches the resume path's ``git am --3way``.

        Returns the new ``patch_id`` on success, ``None`` on any
        failure (empty patch, git error, DB error). All failures are
        logged but non-fatal — the happy path continues and a missed
        intermediate write just means the resume inherits the
        previous iteration's state instead of this one.

        The three bundled assurances:

        1. The INSERT does not delete prior rows — intermediate rows
           accumulate so a crash after iteration N+1 still has
           iteration N available.
        2. On SHIP, the existing #3013 supersede-by-issue-number
           DELETE path (see :meth:`_capture_and_persist_ralph_patch`)
           cleans up these intermediate rows; the authoritative SHIP
           row replaces them.
        3. The 7-day TTL housekeeping sweep catches everything
           abandoned (daemon crash, operator stop, ralph BLOCKED).
        """
        assert self._conn is not None, "connect() must run before persist"

        # Capture the cumulative patch via git format-patch
        # origin/main..HEAD --stdout. Full range (not -1 HEAD) so
        # resumes work across multiple commits on the branch — e.g.
        # when a prior iteration's `git am --continue` left an
        # additional commit beyond the placeholder. Ralph amends
        # in-place per #2971 so there's typically one commit, but the
        # range form is safe regardless.
        try:
            patch_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "format-patch",
                    "origin/main..HEAD",
                    "--stdout",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            self._log.warning(
                "daemon.ralph_iteration_patch_capture_failed",
                extra={
                    "event": "ralph_iteration_patch_capture_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "iteration_n": iteration_n,
                    "verdict": verdict,
                    "detail": str(exc),
                },
            )
            return None
        if patch_result.returncode != 0 or not (patch_result.stdout or "").strip():
            self._log.info(
                "daemon.ralph_iteration_patch_empty_or_failed",
                extra={
                    "event": "ralph_iteration_patch_empty_or_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "iteration_n": iteration_n,
                    "verdict": verdict,
                    "exit_code": patch_result.returncode,
                    "stderr_tail": _stderr_tail(patch_result.stderr),
                },
            )
            return None
        patch_content = patch_result.stdout

        # Best-effort HEAD SHA — same pattern as _capture_and_persist_ralph_patch.
        commit_sha: str | None = None
        try:
            sha_result = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if sha_result.returncode == 0:
                candidate = (sha_result.stdout or "").strip()
                if candidate:
                    commit_sha = candidate
        except Exception:  # pragma: no cover — defensive
            pass

        # Additive INSERT (no DELETE). Intermediate rows accumulate.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.ralph_patches "
                    "    (agent_id, issue_number, patch_content, commit_sha, "
                    "     iteration_n, verdict) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "RETURNING patch_id",
                    (
                        agent_id,
                        issue_number,
                        patch_content,
                        commit_sha,
                        iteration_n,
                        verdict,
                    ),
                )
                row = cur.fetchone()
                patch_id = str(row[0]) if row and row[0] else None
            self._conn.commit()
        except Exception as exc:
            self._log.exception(
                "daemon.ralph_iteration_patch_persist_failed",
                extra={
                    "event": "ralph_iteration_patch_persist_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "iteration_n": iteration_n,
                    "verdict": verdict,
                    "detail": str(exc),
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        self._log.info(
            "daemon.ralph_iteration_patch_persisted",
            extra={
                "event": "ralph_iteration_patch_persisted",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "iteration_n": iteration_n,
                "verdict": verdict,
                "patch_id": patch_id,
                "commit_sha": commit_sha,
                "patch_bytes": len(patch_content),
            },
        )
        return patch_id

    @staticmethod
    def _format_resume_with_conflict_block(
        issue_number: int,
        conflict_info: dict[str, Any],
    ) -> str:
        """Render the RESUME WITH CONFLICT prompt block (#3026).

        Called by :meth:`_run_ralph_phase` when
        :meth:`_apply_prior_ralph_patch` returned
        ``{"applied": False, "conflicted": True, ...}``. The returned
        string is appended to the agent's ``prior_attempts.md`` so the
        ralph skill surfaces it to the worker via ``task.md``.

        The block's exact wording matches the #3026 issue body
        contract — ralph's worker is trained to recognise this
        heading and handle the ``git am --continue`` vs. ``git am
        --abort`` decision accordingly. Do not reword without also
        updating ``.claude/skills/task-v2-ralph/SKILL.md`` and the
        ralph worker prompt in ``.claude/skills/ralph/SKILL.md``.
        """
        source_agent_id = conflict_info.get("source_agent_id") or "unknown"
        agent_id_short = source_agent_id[:8] if source_agent_id else "unknown"
        iter_n = conflict_info.get("iteration_n")
        iter_display = str(iter_n) if iter_n is not None else "SHIP (no iteration)"
        verdict = conflict_info.get("verdict") or "(unknown)"
        age_seconds = float(conflict_info.get("age_seconds") or 0.0)
        age_ago = _format_age_ago(age_seconds)
        conflict_files = conflict_info.get("conflict_files") or []
        k = len(conflict_files)

        lines = [
            "RESUME WITH CONFLICT",
            "",
            f"You are resuming work on issue #{issue_number}. A prior attempt saved a",
            f"cumulative patch (agent {agent_id_short}, iteration {iter_display},",
            f"verdict {verdict}, saved {age_ago}). The daemon attempted to",
            f"apply it with `git am --3way` and {k} files are in conflict.",
            "",
            "Your options:",
            "1. Resolve the conflicts and `git am --continue`. Use this path",
            "   if the prior work is a reasonable starting point and the",
            "   conflicts are tractable.",
            "2. `git am --abort` and start fresh from main. Use this path if",
            "   the prior work is stale, wrong for the current issue",
            "   understanding, or the conflict volume exceeds what's worth",
            "   salvaging.",
            "",
            "Use your judgment. Both paths are valid — prior work is",
            "advisory, not binding.",
        ]
        if conflict_files:
            lines.append("")
            lines.append("Conflict files:")
            for path in conflict_files:
                lines.append(f"  - {path}")
        return "\n".join(lines)

    def _update_agent_phase(self, agent_id: str, phase: str) -> None:
        """UPDATE ``dispatcher.agents.phase`` so the scheduler + admin
        page see where the agent currently sits."""
        assert self._conn is not None, "connect() must run before update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents SET phase = %s WHERE agent_id = %s",
                    (phase, agent_id),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.update_agent_phase_failed",
                extra={
                    "event": "update_agent_phase_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": phase,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

    # ── milestone column writers (issue #2953, migration 35) ───────────
    #
    # Each helper stamps one of the four milestone columns on
    # ``dispatcher.agents``. All are best-effort — a DB error logs,
    # rolls back, and returns without re-raising, matching the
    # ``_update_agent_phase`` / ``_write_failure_summary`` pattern so a
    # transient DB hiccup cannot unwind the caller's control flow (the
    # daemon is already past the decision point the milestone records).

    def _write_merged_at(
        self,
        agent_id: str,
        *,
        pr_number: int | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Stamp ``merged_at = now()`` + flip ``status='succeeded'`` on merge.

        Issue #2953. Called by ``_merge_pr_and_advance`` at the moment
        the ``gh pr merge`` call returns successfully — not at end of
        retro. One-way latch: status becomes ``succeeded`` the instant
        the PR ships, so a container kill between merge and retro no
        longer renders as a red ✗.

        Mirrors the terminal-time side-effects of ``_mark_agent_terminal``
        for the ``succeeded`` branch (``ended_at``, ``failure_summary``
        cleanup, diagnosis outcome write-back, terminal-outcome circuit
        breaker feed) in one UPDATE so the admin row transitions atomically.
        """
        assert self._conn is not None, "connect() must run before update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = 'succeeded', "
                    "    merged_at = now(), "
                    "    ended_at = COALESCE(ended_at, now()), "
                    "    exit_code = COALESCE(exit_code, 0), "
                    "    pr_number = COALESCE(%s, pr_number), "
                    "    failure_summary = NULL "
                    "WHERE agent_id = %s",
                    (pr_number, agent_id),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.write_merged_at_failed",
                extra={
                    "event": "write_merged_at_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — defensive
                pass
            return

        # Mirror the best-effort side-effects that ``_mark_agent_terminal``
        # runs for terminal statuses so the merge-time flip lands the
        # same downstream signals (diagnosis outcome write-back,
        # terminal-outcome circuit-breaker feed, issue-label teardown,
        # circuit-breaker evaluation). Each one is independently
        # wrapped — a failure here cannot roll back the status flip
        # above (which was already committed).
        try:
            self._write_diagnosis_outcome_for_agent(agent_id, "succeeded")
        except Exception:
            self._log.exception(
                "daemon.diagnosis_outcome_write_failed",
                extra={
                    "event": "diagnosis_outcome_write_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "status": "succeeded",
                    "origin": "merged_at",
                },
            )
        try:
            self._write_terminal_outcome(agent_id, "succeeded")
        except Exception:
            self._log.exception(
                "daemon.terminal_outcome_write_failed",
                extra={
                    "event": "terminal_outcome_write_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "status": "succeeded",
                    "origin": "merged_at",
                },
            )
        try:
            self._evaluate_circuit_breaker(agent_id)
        except Exception:
            self._log.exception(
                "daemon.circuit_breaker_evaluate_failed",
                extra={
                    "event": "circuit_breaker_evaluate_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "origin": "merged_at",
                },
            )
        if issue_number is not None:
            # Release ``status/in-progress`` label — the agent logically
            # succeeded the moment the PR shipped. Best-effort; matches
            # the ``_mark_agent_terminal`` teardown path (#2866).
            self._gh_issue_remove_labels(issue_number, [STATUS_IN_PROGRESS_LABEL])

    def _write_verified_at(self, agent_id: str) -> None:
        """Stamp ``verified_at = now()`` when the verify phase succeeds.

        Issue #2953. Does NOT touch ``status`` — ``merged_at`` already
        flipped it to ``succeeded`` at merge. The admin cockpit reads
        both columns and renders the pill color from their combined
        presence.
        """
        assert self._conn is not None, "connect() must run before update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents SET verified_at = now() "
                    "WHERE agent_id = %s",
                    (agent_id,),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.write_verified_at_failed",
                extra={
                    "event": "write_verified_at_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — defensive
                pass

    def _write_verify_skip_reason(self, agent_id: str, reason: str) -> None:
        """Stamp ``verify_skip_reason`` pre-push when verify is skipped.

        Issue #2953. Written by ``_push_and_open_pr`` before the push
        when the PR touches ``scripts/dispatcher/`` (self-deploy case).
        The verify phase later reads this column and no-ops if it's
        non-null, so the admin cockpit can distinguish "shipped + verify
        does not apply" from "shipped + verify failed to run".
        """
        assert self._conn is not None, "connect() must run before update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents SET verify_skip_reason = %s "
                    "WHERE agent_id = %s",
                    (reason, agent_id),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.write_verify_skip_reason_failed",
                extra={
                    "event": "write_verify_skip_reason_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "reason": reason,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — defensive
                pass

    def _read_verify_skip_reason(self, agent_id: str) -> str | None:
        """Return the current ``verify_skip_reason`` for an agent, or None."""
        assert self._conn is not None, "connect() must run before read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT verify_skip_reason FROM dispatcher.agents "
                    "WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.read_verify_skip_reason_failed",
                extra={
                    "event": "read_verify_skip_reason_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — defensive
                pass
            return None
        if row is None:
            return None
        value = row[0]
        return str(value) if isinstance(value, str) and value else None

    def _write_retroed_at(self, agent_id: str) -> None:
        """Stamp ``retroed_at = now()`` when retro reaches PHASE_RETRO_DONE.

        Issue #2953. Does NOT touch ``status`` or ``phase`` — those are
        handled by the existing ``_update_agent_phase(PHASE_RETRO_DONE)``
        call. Best-effort; a DB error here cannot unwind the retro work.
        """
        assert self._conn is not None, "connect() must run before update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents SET retroed_at = now() "
                    "WHERE agent_id = %s",
                    (agent_id,),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.write_retroed_at_failed",
                extra={
                    "event": "write_retroed_at_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — defensive
                pass

    @staticmethod
    def _detect_verify_skip_reason(
        touched_paths: list[str],
    ) -> str | None:
        """Return a ``verify_skip_reason`` for a PR's touched file list, or None.

        Issue #2953 dispatcher-self-PR detection. Called in
        ``_push_and_open_pr`` after ``git commit`` and before ``git
        push``. Any file whose path starts with a prefix in
        :data:`_SELF_DEPLOY_PATH_PREFIXES` triggers
        :data:`VERIFY_SKIP_REASON_SELF_DEPLOY`.

        The check is "any file matches" — a PR that touches both
        dispatcher source AND something else still skips verify,
        because the daemon deploy will land mid-verify regardless of
        what else the PR did. Pure-functional for testability.
        """
        for path in touched_paths:
            for prefix in _SELF_DEPLOY_PATH_PREFIXES:
                if path.startswith(prefix):
                    return VERIFY_SKIP_REASON_SELF_DEPLOY
        return None

    def _is_noop_ship(self, worktree: Path) -> bool:
        """Return True when ralph SHIPped without creating a commit.

        Issue #3039. Ralph's §2.5d "no-op guardrail" skips the
        pre-push commit when the working tree is clean at SHIP time
        (data-only / no-code-change task whose deliverable is the
        evidence comment ralph posts on the issue). In that state
        the worktree branch tip is still ``origin/main`` and there
        is nothing to amend, push, or PR.

        Implementation: ``git rev-list --count origin/main..HEAD``.
        Return value ``0`` → no-op SHIP; any positive integer → real
        commits on the branch. Subprocess failures fail-closed to
        ``False`` (treat as "normal commits ahead" and let the rest
        of :meth:`_push_and_open_pr` run normally — worst case is the
        pre-#3039 failure mode, which is already instrumented by the
        ``pr_create_failed`` classifier).

        Pure on a single ``subprocess.run`` so the unit tests can
        stub it with the standard ``patch('subprocess.run',
        side_effect=[...])`` pattern used across the dispatcher
        tests.
        """
        cmd = [
            "git",
            "-C",
            str(worktree),
            "rev-list",
            "--count",
            "origin/main..HEAD",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode != 0:
            return False
        try:
            ahead = int((result.stdout or "").strip())
        except ValueError:
            return False
        return ahead == 0

    def _list_committed_files_at_head(self, worktree: Path) -> list[str]:
        """Return the file list of the most recent commit on ``worktree``.

        Issue #2953. Uses ``git show --name-only --pretty=format:``
        which emits one path per line after the commit's empty-message
        preamble. Returns an empty list on subprocess failure — the
        caller treats that as "no skip reason detectable" and verify
        runs normally. Swallowing the error is safe because the
        admin-cockpit UX gracefully handles a verify that ran and
        wrote ``verified_at`` (the normal path).
        """
        cmd = [
            "git",
            "-C",
            str(worktree),
            "show",
            "--name-only",
            "--pretty=format:",
            "HEAD",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        if result.returncode != 0:
            return []
        return [line for line in (result.stdout or "").splitlines() if line.strip()]

    # ── failure_summary builder (issue #2900) ──────────────────────────

    #: Statuses that get a ``dispatcher.agents.failure_summary``
    #: populated at terminal-time. Genuine failures + the plan-blocked
    #: correct-outcome terminal (operators still want "why did plan
    #: decline") qualify; ``succeeded`` and ``needs_review`` do not —
    #: the draft PR IS the signal for needs_review and success needs
    #: no narrative.
    _FAILURE_SUMMARY_STATUSES = frozenset({"failed", "crashed", "plan_blocked"})

    #: Hard cap matching the admin cockpit tooltip's visual budget and
    #: the Postgres column comment in migration 33. Enforced at
    #: write-time; anything longer is truncated with an ellipsis.
    _FAILURE_SUMMARY_MAX_CHARS = 240

    #: Failure categories where the log tail is meaningless — the
    #: terminal was not caused by a subprocess crash or stderr-emitting
    #: error, so the tail of ``phase_outputs.log_text`` is just the
    #: Anthropic JSON success envelope from the last phase that did run.
    #: For these categories we emit a short human-readable summary with
    #: no tail segment. Issue #2924.
    _NO_TAIL_CATEGORIES = frozenset(
        {"paused_by_killswitch", "daemon_restart_abandoned"}
    )

    #: Human-readable summaries for the no-tail categories. Keyed by
    #: category; the value is the complete ``failure_summary`` string
    #: (no phase/status/category parens — those would repeat the same
    #: word). Issue #2924; strings rephrased for operator-readability in
    #: issue #2935 (``"paused by killswitch"`` → ``"manually stopped"``;
    #: ``"daemon restart abandoned"`` → ``"dispatcher restarted"``). The
    #: stored ``dispatcher.failures.category`` values are unchanged —
    #: only the surfaced display string here.
    _NO_TAIL_CATEGORY_SUMMARIES = {
        "paused_by_killswitch": "manually stopped",
        "daemon_restart_abandoned": "dispatcher restarted",
    }

    #: Display-name map consulted by :meth:`_build_failure_summary` at
    #: the parenthesized-category slot of the templated output. Keyed by
    #: the stored ``dispatcher.failures.category`` value (which is a
    #: machine-readable token used for retry classification, log
    #: filtering, and CloudWatch Insights queries); the value is the
    #: operator-friendly phrasing rendered in the admin cockpit tooltip.
    #: Unknown categories fall through to the stored string verbatim via
    #: ``_CATEGORY_DISPLAY_NAMES.get(category, category)``.
    #:
    #: Issue #2935 — rename is display-only; stored values stay put so
    #: logs, Insights queries, retry classifiers, and existing tests
    #: aren't broken.
    _CATEGORY_DISPLAY_NAMES = {
        "subprocess_turn_limit": "turn limit reached",
        "subprocess_crash": "subprocess crashed",
        "subprocess_auth_fail": "auth failed",
        "ci_red_after_retries": "CI failed after retries",
        "gh_rate_exhausted": "GitHub rate limit",
        "stuck_timeout": "timed out",
        # Push-failure sub-kinds (#2902).
        "push_failed": "git push failed",
        "pre_push_hook_rejected": "pre-push hook rejected",
        "git_push_network": "git push network error",
        # AC-infeasibility (#3010). Tier 3 — routed directly to the
        # diagnoser. Rendered as "AC infeasible (ralph)" /
        # "AC infeasible (summary)" so operators see where in the
        # pipeline the infeasibility was detected.
        "ralph_ac_infeasible": "AC infeasible (ralph)",
        "summary_ac_infeasible": "AC infeasible (summary)",
    }

    @staticmethod
    def _humanize_phase(phase: str) -> str:
        """Turn ``"ralph-reviewer (3)"`` into ``"ralph-reviewer iteration 3"``.

        Other shapes pass through unchanged — ``"plan"`` stays ``"plan"``,
        ``"push_and_pr"`` stays ``"push_and_pr"``, ``"ralph (1)"``
        becomes ``"ralph iteration 1"``. The iteration suffix is the
        ralph loop's attempt counter; surfacing it inline makes the
        tooltip self-describing without the operator having to know the
        phase-naming convention.
        """
        import re  # noqa: PLC0415 — lazy; only fires on terminal path

        m = re.match(r"^(.*)\s*\((\d+)\)\s*$", phase or "")
        if m:
            prefix = m.group(1).strip()
            n = m.group(2)
            return f"{prefix} iteration {n}" if prefix else f"iteration {n}"
        return phase or "unknown"

    @staticmethod
    def _extract_stderr_tail(log_text: str | None) -> str:
        """Pull the last non-empty, non-JSON line out of a ``log_text``.

        ``log_text`` is the composed stdout+stderr blob from
        :meth:`_compose_phase_log` — begins with ``=== stderr ===`` when
        both streams had content. We search from the end for the first
        non-blank, non-separator, non-JSON-envelope line. Empty string
        when the log has no usable tail.

        Issue #2924: the admin cockpit tooltip was rendering the raw
        ``claude -p`` JSON result envelope (``{"type":"result",...}``)
        as the failure summary tail. A completed phase writes its
        success envelope as stdout, which ends up as the last line of
        ``log_text`` — useless as a "what went wrong" tail. We skip
        lines that parse as JSON objects so the tail falls back to the
        nearest real stderr line.
        """
        if not log_text:
            return ""
        lines = [line.strip() for line in log_text.splitlines()]
        for line in reversed(lines):
            if not line:
                continue
            if line.startswith("===") and line.endswith("==="):
                continue
            if DispatcherDaemon._looks_like_json_envelope(line):
                continue
            return line
        return ""

    @staticmethod
    def _looks_like_json_envelope(line: str) -> bool:
        """True if ``line`` parses as a JSON object/array (envelope).

        Used by :meth:`_extract_stderr_tail` to skip past the Anthropic
        ``claude -p`` success envelope that lives in stdout. We only
        treat objects/arrays as envelopes — a bare JSON scalar
        (``"foo"`` or ``42``) could plausibly be a real stderr token
        and we want to preserve it. Non-JSON lines fail the ``json.loads``
        call and return False immediately. Issue #2924.
        """
        if not line:
            return False
        # Quick structural filter — only object/array-shaped lines can be
        # the multi-line JSON envelope we want to skip. Avoids a parse
        # attempt for every log line (cheap but not free).
        first = line[0]
        if first not in "{[":
            return False
        import json  # noqa: PLC0415 — lazy; only on terminal path

        try:
            json.loads(line)
        except (ValueError, TypeError):
            return False
        return True

    @staticmethod
    def _extract_details_message(details: Any) -> str:
        """Pull a short human message out of a ``failures.details`` JSONB.

        The ``details`` column is per-category schema — different keys
        for ``stuck_timeout`` (``stuck_seconds``), ``plan_go_false``
        (``block_reason``), ``ci_red_after_retries`` (``detail``), etc.
        Try the most-often-present keys in priority order; fall back to
        an empty string so the caller can still emit a summary using
        just phase + category.
        """
        if not isinstance(details, dict):
            return ""
        for key in (
            "stderr_tail",
            "block_reason",
            "detail",
            "reason",
            "message",
            "error",
        ):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _build_failure_summary(
        self,
        *,
        agent_id: str,
        status: str,
        phase: str,
        exit_code: int | None,
    ) -> str | None:
        """Build the templated one-liner for ``agents.failure_summary``.

        Issue #2900. Reads the latest ``dispatcher.failures`` row
        (category + details) and the latest ``dispatcher.phase_outputs``
        ``log_text`` (last stderr line) for the agent, then composes:

            "{phase-group} {verb} at {phase-humanized} ({category}): {detail}"

        For ``plan_blocked`` the verb phrase becomes
        ``"plan phase returned go=false"`` to match the issue's example
        list and keep the correct-outcome terminal visually distinct
        from a genuine crash.

        Returns ``None`` for:

        - Statuses outside :attr:`_FAILURE_SUMMARY_STATUSES` (succeeded,
          needs_review, running) — caller skips the column write.
        - Any DB error during the SELECTs — caller writes NULL to the
          column, matching the "best-effort" contract. The column is a
          nice-to-have; never blocking the terminal path.

        Truncates to :attr:`_FAILURE_SUMMARY_MAX_CHARS` with a single
        trailing ellipsis so the admin tooltip stays visually bounded.
        """
        if status not in self._FAILURE_SUMMARY_STATUSES:
            return None
        if self._conn is None:
            return None

        category: str | None = None
        details: Any = None
        log_text: str | None = None
        try:
            with self._conn.cursor() as cur:
                # Latest failure row for this agent. ``ts DESC LIMIT 1``
                # — a multi-retry agent may have several failure rows,
                # the freshest one is the one that caused the current
                # terminal transition.
                cur.execute(
                    "SELECT category, details "
                    "FROM dispatcher.failures "
                    "WHERE agent_id = %s "
                    "ORDER BY ts DESC "
                    "LIMIT 1",
                    (agent_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    category = row[0]
                    details = row[1]

                # Latest phase_outputs log_text — the composed
                # stdout+stderr blob from the most-recent phase (which
                # is typically the failing one).
                cur.execute(
                    "SELECT log_text "
                    "FROM dispatcher.phase_outputs "
                    "WHERE agent_id = %s "
                    "ORDER BY ts DESC "
                    "LIMIT 1",
                    (agent_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    log_text = row[0]
            self._conn.commit()
        except Exception:
            self._log.warning(
                "daemon.failure_summary_read_failed",
                extra={
                    "event": "failure_summary_read_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — defensive
                pass
            return None

        # Issue #2924: for killswitch / restart-abandoned categories
        # there IS no meaningful stderr tail — the agent did not crash,
        # it was administratively terminated. Emit a short neutral
        # summary instead of dumping the raw JSON success envelope from
        # the last phase that did complete.
        if category in self._NO_TAIL_CATEGORIES:
            return self._NO_TAIL_CATEGORY_SUMMARIES[category]

        phase_humanized = self._humanize_phase(phase)
        # Phase-group = first token before a dash or space. ``ralph-reviewer``
        # → ``ralph``; ``push_and_pr`` → ``push_and_pr`` (no dash).
        phase_group = (phase or "unknown").split("-", 1)[0].split(" ", 1)[0]

        # Detail message: prefer the failure-row details dict (has
        # category-specific rich data), fall back to the stderr tail
        # from phase_outputs (raw subprocess output), fall back to the
        # exit_code.
        detail = self._extract_details_message(details)
        if not detail:
            detail = self._extract_stderr_tail(log_text)
        if not detail and exit_code is not None:
            detail = f"exit_code={exit_code}"

        # Verb phrase — ``plan_blocked`` is its own thing per the issue
        # examples; other failure statuses are "crashed" / "failed".
        #
        # Issue #2924: collapse the ``<phase-group> <verb> at
        # <phase-humanized>`` form to just ``<phase> <verb>`` when the
        # two positions resolve to the same string. Sibling #2914
        # already flagged the tautological case — e.g.
        # ``"daemon_restart_abandoned crashed at daemon_restart_abandoned"``
        # just reads as the same word twice. Short-phase rows (``plan``,
        # ``push_and_pr``, ``daemon_restart_abandoned``) hit this every
        # time; only multi-iteration phases like ``ralph-reviewer (3)``
        # preserve the ``at …`` clause because the humanized version
        # adds new information (``ralph`` vs ``ralph-reviewer iteration 3``).
        if status == "plan_blocked":
            verb_phrase = "plan phase returned go=false"
        elif status == "crashed":
            if phase_group == phase_humanized:
                verb_phrase = f"{phase_humanized} crashed"
            else:
                verb_phrase = f"{phase_group} crashed at {phase_humanized}"
        else:  # status == "failed"
            if phase_group == phase_humanized:
                verb_phrase = f"{phase_humanized} failed"
            else:
                verb_phrase = f"{phase_group} failed at {phase_humanized}"

        # Compose. Category is parenthesized; detail trails a colon so
        # a scanner can pattern-match on either end of the string.
        # Issue #2935: map the stored (machine-readable) category through
        # ``_CATEGORY_DISPLAY_NAMES`` so the tooltip reads in English.
        # The stored value in ``dispatcher.failures.category`` stays
        # unchanged — this rewrite is render-time only.
        parts: list[str] = [verb_phrase]
        if category:
            display_category = self._CATEGORY_DISPLAY_NAMES.get(category, category)
            parts[-1] = f"{parts[-1]} ({display_category})"
        if detail:
            summary = f"{parts[-1]}: {detail}"
        else:
            summary = parts[-1]

        # Truncate to the column's visual budget. Leave room for the
        # trailing ellipsis so we don't cut a UTF-8 sequence in half.
        cap = self._FAILURE_SUMMARY_MAX_CHARS
        if len(summary) > cap:
            summary = summary[: cap - 1].rstrip() + "\u2026"
        return summary

    def _write_failure_summary(self, agent_id: str, summary: str) -> None:
        """UPDATE ``dispatcher.agents.failure_summary`` for one agent.

        Issue #2900. Called by :meth:`_mark_agent_terminal` on genuine-
        failure terminals and by the ``/diagnose-failure`` upgrade path
        (written to the same column with richer LLM-authored prose).
        Best-effort — a write failure logs + rolls back but must not
        propagate; the terminal status was already committed.
        """
        assert self._conn is not None, "connect() must run before update"
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE dispatcher.agents SET failure_summary = %s WHERE agent_id = %s",
                (summary, agent_id),
            )
        self._conn.commit()

    def _mark_agent_terminal(
        self,
        agent_id: str,
        status: str,
        phase: str,
        exit_code: int | None = None,
        pr_number: int | None = None,
        issue_number: int | None = None,
    ) -> None:
        """UPDATE ``dispatcher.agents`` with terminal status + metadata.

        Used for ``succeeded``, ``failed``, ``crashed``, ``plan_blocked``,
        ``needs_review``, and the Phase 3A post-PR hand-off state
        (``status='running'``, ``phase='awaiting_ci'``). For terminal
        statuses (``succeeded`` / ``failed`` / ``crashed`` /
        ``plan_blocked`` / ``needs_review``) also sets ``ended_at`` so
        the admin page can compute duration AND writes back the
        resolved outcome to any pending ``dispatcher.diagnoses`` rows
        for this agent (Phase 3E #2798, spec §8 line 305).

        ``plan_blocked`` (issue #2857) is the "plan correctly declined
        to proceed" terminal — distinct from ``failed`` (genuine
        infrastructure/subprocess break) so the admin cockpit and
        reporting can separate correct-outcome triage from real
        failures.

        ``needs_review`` (issue #2856) is the "ralph did real work but
        summary flagged one or more unmet acceptance criteria"
        terminal. The daemon opened a DRAFT PR preserving ralph's
        output so the operator can decide whether to mark it ready +
        merge, close, or iterate. Distinct from ``failed`` (which
        discarded ralph's work) and from ``plan_blocked`` (which never
        reached ralph) — the admin cockpit renders it with an
        amber/yellow "needs your eyes" chip because it IS actionable,
        unlike ``plan_blocked`` which is informational.

        ``issue_number`` is optional (issue #2866). When provided, the
        method clears the ``status/in-progress`` label on the issue so
        the claim interlock releases. Call sites that don't pass it
        (supervisor stuck-timeout sweep, generic retry paths) leave the
        label attached — an operator / next scheduler cycle handles the
        teardown, which is fine because those paths are rare edge cases
        and the DB row is already marked terminal (the authoritative
        interlock signal).
        """
        assert self._conn is not None, "connect() must run before update"
        terminal = status in (
            "succeeded",
            "failed",
            "crashed",
            "plan_blocked",
            "needs_review",
        )
        # Issue #2913: clear ``failure_summary`` on correct-outcome
        # terminals so a row that previously held a crash message from
        # an earlier iteration (crashed → retry_reset → succeeded) does
        # not render its old tooltip on the ✓ glyph in the admin
        # cockpit. Atomic with the status transition — no second
        # round-trip, no window where the admin page can read a
        # ``succeeded`` row with a stale ``failure_summary``. Failure
        # terminals (``failed`` / ``crashed`` / ``plan_blocked``) leave
        # the column alone here; the follow-up ``_write_failure_summary``
        # block below (issue #2900) populates it with the templated
        # one-liner for those statuses.
        clear_failure_summary = status in ("succeeded", "needs_review")
        try:
            with self._conn.cursor() as cur:
                if terminal:
                    if clear_failure_summary:
                        cur.execute(
                            "UPDATE dispatcher.agents "
                            "SET status = %s, phase = %s, ended_at = now(), "
                            "    exit_code = %s, "
                            "    pr_number = COALESCE(%s, pr_number), "
                            "    failure_summary = NULL "
                            "WHERE agent_id = %s",
                            (status, phase, exit_code, pr_number, agent_id),
                        )
                    else:
                        cur.execute(
                            "UPDATE dispatcher.agents "
                            "SET status = %s, phase = %s, ended_at = now(), "
                            "    exit_code = %s, pr_number = COALESCE(%s, pr_number) "
                            "WHERE agent_id = %s",
                            (status, phase, exit_code, pr_number, agent_id),
                        )
                else:
                    cur.execute(
                        "UPDATE dispatcher.agents "
                        "SET status = %s, phase = %s, "
                        "    pr_number = COALESCE(%s, pr_number) "
                        "WHERE agent_id = %s",
                        (status, phase, pr_number, agent_id),
                    )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.mark_agent_failed",
                extra={
                    "event": "mark_agent_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "status": status,
                    "phase": phase,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return

        # Phase 3E (#2798): diagnoser effectiveness tracking. After the
        # agent reaches a terminal status, write the outcome back to
        # any ``dispatcher.diagnoses`` rows for this agent that don't
        # yet have an outcome set. Idempotent — running twice produces
        # the same state because we filter on ``outcome IS NULL``.
        # Wrapped in its own try/except so a write-back failure cannot
        # roll back the terminal-status update above.
        if terminal:
            try:
                self._write_diagnosis_outcome_for_agent(agent_id, status)
            except Exception:
                self._log.exception(
                    "daemon.diagnosis_outcome_write_failed",
                    extra={
                        "event": "diagnosis_outcome_write_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "status": status,
                    },
                )

            # Issue #2900: write a templated one-liner to
            # ``dispatcher.agents.failure_summary`` for genuine-failure
            # + plan_blocked terminals so the admin cockpit can surface
            # "what happened" on hover over the outcome glyph without a
            # cross-table join. Best-effort — a failure here cannot roll
            # back the terminal-status update above. The builder returns
            # None for non-failure statuses (succeeded, needs_review);
            # we skip the UPDATE entirely in that case.
            try:
                summary = self._build_failure_summary(
                    agent_id=agent_id,
                    status=status,
                    phase=phase,
                    exit_code=exit_code,
                )
                if summary:
                    self._write_failure_summary(agent_id, summary)
            except Exception:
                self._log.exception(
                    "daemon.failure_summary_write_failed",
                    extra={
                        "event": "failure_summary_write_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "status": status,
                    },
                )
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover — defensive
                    pass

            # Overnight-safety circuit breaker (#2860): append the outcome
            # to ``dispatcher.terminal_outcomes`` and evaluate the
            # M-of-N rolling-window threshold. A breaker failure here
            # cannot roll back the terminal-status update above — both
            # side effects are independently wrapped.
            try:
                self._write_terminal_outcome(agent_id, status)
            except Exception:
                self._log.exception(
                    "daemon.terminal_outcome_write_failed",
                    extra={
                        "event": "terminal_outcome_write_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "status": status,
                    },
                )
            try:
                self._evaluate_circuit_breaker(agent_id)
            except Exception:
                self._log.exception(
                    "daemon.circuit_breaker_evaluate_failed",
                    extra={
                        "event": "circuit_breaker_evaluate_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                    },
                )

            # Issue #2866 claim-interlock teardown: on any terminal
            # transition where the caller knows the issue number, remove
            # the ``status/in-progress`` label so the issue stops being
            # hidden from the queue scan filter and the operator-visible
            # "in flight" signal clears. Wrapped in its own try/except so
            # a GitHub API hiccup cannot roll back the DB write.
            #
            # The teardown is explicit-opt-in — callers thread
            # ``issue_number`` through when they know it (orchestration
            # hot path, diagnoser auto-fail). Call sites that don't
            # (supervisor stuck-timeout sweep, generic retry paths)
            # simply leave the label attached for the supervisor /
            # operator to clean up. This keeps the hot DB path narrow
            # (no extra SELECT, no test-fixture reshape) while still
            # closing the common-case interlock.
            if issue_number is not None:
                self._gh_issue_remove_labels(issue_number, [STATUS_IN_PROGRESS_LABEL])

    def _spawn_phase_subprocess(
        self, phase: str, worktree: Path, agent_id: str
    ) -> tuple[int, float]:
        """Run ``claude -p '/task-v2-<phase> <agent_id>' --output-format json``.

        Returns ``(exit_code, duration_seconds)``. Raises
        :class:`subprocess.TimeoutExpired` on wall-clock timeout so the
        caller can mark the agent failed. Other subprocess errors bubble
        up as their native exception types.

        **Output capture layout (#2869).** ``--output-format json`` makes
        stdout a single JSON envelope with ``{result, usage,
        total_cost_usd, ...}``; if stderr merged into it the JSON would
        be corrupted and unparseable. We split:

        - stdout → ``{worktree}/tmp/claude-p-<phase>.stdout.json`` (pure
          JSON, parsed by :meth:`_parse_phase_usage` for metering).
        - stderr → ``{worktree}/tmp/claude-p-<phase>.stderr.log`` (normal
          text, preserved for triage).
        - After the subprocess exits we also write a combined view to
          ``{worktree}/tmp/claude-p-<phase>.log`` (stderr first, then a
          separator, then stdout). This preserves the pre-#2869 filename
          invariant so triage helpers (:meth:`_log_tail`,
          :meth:`_read_full_phase_log`, :meth:`_emit_phase_failure_log_event`)
          keep working unchanged, and operators can still ``cat``,
          ``tail``, or ``grep`` a single file during incident triage.

        **Real-time stream forwarding (#3017).** Each line of child
        stdout+stderr is also forwarded to :data:`self._log` (tagged
        ``agent_id``, ``issue_number``, ``phase``, ``stream``,
        ``raw_message``) and mirrored into
        ``{worktree}/.dispatcher/{phase}-{agent_id}.jsonl`` in real time
        by :func:`stream_subprocess_output_async`. This lets operators
        see a ralph hang's last tool call in CloudWatch or via
        ``tail -f`` on the JSONL file — the pre-#3017 code called
        :func:`subprocess.run` which buffered all output until exit, so
        a stuck subprocess produced no breadcrumbs at all. The
        tee-to-file behaviour of the forwarder keeps the pre-#3017
        capture files intact, so metering (:meth:`_parse_phase_usage`)
        and triage helpers continue to work unchanged.

        The worktree is passed as the subprocess's CWD via
        :class:`subprocess.Popen` ``cwd=`` — NOT as a ``--cwd`` flag.
        The ``claude`` CLI does not accept a ``--cwd`` flag; passing one
        makes every phase subprocess exit 1 within 400ms with
        ``error: unknown option '--cwd'`` (#2821). Python's stdlib
        ``cwd=`` is the correct knob for "start the child process in
        this directory".
        """
        max_turns = PHASE_MAX_TURNS[phase]
        model = self._model_for_phase(phase, agent_id)
        log_path = worktree / "tmp" / f"claude-p-{phase}.log"
        stdout_path = worktree / "tmp" / f"claude-p-{phase}.stdout.json"
        stderr_path = worktree / "tmp" / f"claude-p-{phase}.stderr.log"
        jsonl_path = worktree / ".dispatcher" / f"{phase}-{agent_id}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "claude",
            "-p",
            f"/task-v2-{phase} {agent_id}",
            "--max-turns",
            str(max_turns),
            "--model",
            model,
            "--output-format",
            "json",
            # ``--dangerously-skip-permissions`` bypasses the Bash-tool
            # permission policy (the allowlist in
            # ``.claude/settings.json``). Without it, paths not in the
            # default allowlist — e.g. ``.githooks/pre-push`` — get
            # rejected before the preflight hook even runs. The Fargate
            # container image (see Dockerfile.dispatcher) ships a narrowed
            # preflight hook that only enforces the 4 safety-critical
            # rules, so the combination of skip-permissions + narrowed
            # preflight is the minimum configuration that lets a subagent
            # invoke ``.githooks/pre-push`` end-to-end. See issue #2982
            # and the ralph Step 2.5 regression on #2960 (four
            # consecutive permission-denied failures).
            "--dangerously-skip-permissions",
        ]

        start = time.monotonic()
        issue_number = self._agent_issue_number(agent_id)
        self._log.info(
            "daemon.phase_started",
            extra={
                "event": "phase_started",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "phase": phase,
                "model": model,
                "max_turns": max_turns,
                "output_format": "json",
            },
        )

        proc: subprocess.Popen[str] | None = None
        returncode = -1
        try:
            with (
                stdout_path.open("w", encoding="utf-8") as stdout_file,
                stderr_path.open("w", encoding="utf-8") as stderr_file,
            ):
                # ``bufsize=1`` (line-buffered) + ``text=True`` is what
                # makes the forwarder see lines as they arrive rather
                # than in 8KB chunks. Without these, a slow child (long
                # tool call) would appear silent in CloudWatch for
                # seconds-to-minutes at a time — defeating the point of
                # #3017. See the docstring of
                # :func:`stream_subprocess_output_async`.
                proc = subprocess.Popen(  # noqa: S603 — cmd is a literal list of trusted strings
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=str(worktree),
                )
                threads = stream_subprocess_output_async(
                    proc,
                    agent_id=agent_id,
                    issue_number=issue_number,
                    phase=phase,
                    logger=self._log,
                    jsonl_path=jsonl_path,
                    stdout_sink=stdout_file,
                    stderr_sink=stderr_file,
                )
                try:
                    returncode = proc.wait(timeout=CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    # Kill the child so the reader threads receive EOF
                    # and can join. Then re-raise so the caller's
                    # existing ``TimeoutExpired`` handler fires.
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except (
                        subprocess.TimeoutExpired
                    ):  # pragma: no cover — SIGKILL should always succeed
                        pass
                    threads.join(timeout=10)
                    raise
                threads.join(timeout=10)
            duration = time.monotonic() - start
        finally:
            # Always compose the combined ``.log`` view, even on timeout /
            # non-zero exit — triage helpers read it. Whatever partial
            # content the two streams produced before the fault is useful.
            self._compose_phase_log(
                log_path=log_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        return returncode, duration

    def _agent_issue_number(self, agent_id: str) -> int | None:
        """Best-effort lookup of ``dispatcher.agents.issue_number`` by agent id.

        Returns ``None`` if the row is missing, the column is NULL, or
        the DB call fails. The forwarder accepts ``None`` — the
        structured-log ``issue_number`` field is simply serialized as
        JSON null so CloudWatch Log Insights can still filter by
        ``agent_id`` + ``phase``. Issue #3017.
        """
        conn = self._conn
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT issue_number FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:  # pragma: no cover — defensive
            try:
                conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None or row[0] is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):  # pragma: no cover — defensive
            return None

    @staticmethod
    def _compose_phase_log(
        log_path: Path, stdout_path: Path, stderr_path: Path
    ) -> None:
        """Concatenate stderr + stdout files into the combined ``.log`` file.

        Written after every ``_spawn_phase_subprocess`` exit so the
        legacy single-file triage path (``claude-p-<phase>.log``)
        continues to surface both streams to operators. stderr comes
        first because the rare content there (claude CLI crash messages,
        boot errors) is almost always what operators want at the head of
        the triage file; stdout (which is the JSON envelope post-#2869)
        follows behind a thin separator so it's still grep-able but not
        mistaken for stderr.
        """
        parts: list[str] = []
        try:
            if stderr_path.exists():
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                if stderr_text:
                    parts.append("=== stderr ===\n")
                    parts.append(stderr_text)
                    if not stderr_text.endswith("\n"):
                        parts.append("\n")
            if stdout_path.exists():
                stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
                if stdout_text:
                    parts.append("=== stdout (JSON envelope) ===\n")
                    parts.append(stdout_text)
                    if not stdout_text.endswith("\n"):
                        parts.append("\n")
        except Exception:  # pragma: no cover — defensive
            # Never let log composition break the phase. The phase's
            # structured output path is separate; this is triage-only.
            return
        try:
            log_path.write_text("".join(parts), encoding="utf-8")
        except Exception:  # pragma: no cover — defensive
            return

    def _parse_phase_usage(self, worktree: Path, phase: str) -> dict[str, Any] | None:
        """Parse the ``claude -p --output-format json`` usage envelope.

        Reads ``{worktree}/tmp/claude-p-<phase>.stdout.json`` — the
        single JSON object Claude Code writes to stdout when
        ``--output-format json`` is passed. Extracts the fields we
        persist on ``dispatcher.phase_outputs``:

        - ``tokens_input`` ← ``usage.input_tokens``
        - ``tokens_output`` ← ``usage.output_tokens``
        - ``tokens_cache_read`` ← ``usage.cache_read_input_tokens``
        - ``tokens_cache_write`` ← ``usage.cache_creation_input_tokens``
        - ``cost_usd`` ← ``total_cost_usd``
        - ``model_used`` ← ``model`` (falls back to ``PHASE_MODELS[phase]``)

        Returns ``None`` on any error — the stdout file is missing,
        empty, not JSON, or the envelope has no ``usage`` block. The
        caller treats ``None`` the same as "no metering signal": the
        new columns stay NULL, the row still inserts. Issue #2869.
        """
        stdout_path = worktree / "tmp" / f"claude-p-{phase}.stdout.json"
        if not stdout_path.exists():
            return None
        try:
            raw = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:  # pragma: no cover — defensive
            return None
        if not raw:
            return None
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict):
            return None
        usage = envelope.get("usage")
        if not isinstance(usage, dict):
            # Some Claude Code versions emit a usage-less envelope for
            # errors; persist nothing rather than guessing.
            return None
        cost_raw = envelope.get("total_cost_usd")
        try:
            cost_usd: float | None = float(cost_raw) if cost_raw is not None else None
        except (TypeError, ValueError):
            cost_usd = None
        model_used = envelope.get("model")
        if not isinstance(model_used, str) or not model_used:
            model_used = PHASE_MODELS.get(phase)

        def _int_or_none(v: Any) -> int | None:
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "tokens_input": _int_or_none(usage.get("input_tokens")),
            "tokens_output": _int_or_none(usage.get("output_tokens")),
            "tokens_cache_read": _int_or_none(usage.get("cache_read_input_tokens")),
            "tokens_cache_write": _int_or_none(
                usage.get("cache_creation_input_tokens")
            ),
            "cost_usd": cost_usd,
            "model_used": model_used,
        }

    def _claim_and_orchestrate_one(self) -> None:
        """Claim one issue and run the plan → ralph → summary → PR flow.

        The public entry point for Phase 3A. Called by the scheduler
        tick when ``concurrency_cap > 0`` AND no agent is in flight.
        All branching lives here so the tick stays flat.

        **Phase 3C (#2791):** before claiming a new candidate, check for
        any ``status='retrying'`` agent this daemon previously left
        behind. The retry marker processor (§7 step 5) resets agents to
        ``status='retrying' phase='claiming'`` after a tier-1
        mechanical failure's backoff elapses. Picking them up here is
        the "3A's claim path catches it next tick" half of the retry
        loop — without this, the retrying row would sit idle forever.
        """
        # Reset within-tick handoff so a prior run's partial state
        # cannot leak into the next attempt (defense-in-depth; the
        # scheduler only enters this path when no agent is active).
        self._agent_plan_output = None
        self._agent_ralph_output = None
        self._agent_summary_output = None
        self._agent_unmet_criteria = None

        # Phase 3C resume-retry path: pick up any retrying agent first.
        # If one exists, re-orchestrate it on a fresh worktree instead
        # of claiming a new issue. Returning after a resume means this
        # scheduler tick spent its single concurrency slot on the
        # retry, which matches the "concurrency_cap=1" invariant.
        resumed = self._resume_retrying_agent()
        if resumed:
            return

        candidates = self._latest_queue_snapshot_issues()
        if not candidates:
            # No snapshot yet (first tick) or snapshot was empty.
            return

        issue_number = self._pick_candidate_issue(candidates)
        if issue_number is None:
            return

        agent_id = str(uuid.uuid4())
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        worktree_path = self._compute_worktree_path(short_id)

        # Best-effort title lookup from the latest queue snapshot
        # (issue #2820). A None result just means the admin-page
        # recent-completions row renders with issue_title=NULL and the
        # UI falls back to "#N" — not a blocker for the claim.
        issue_title = self._latest_queue_snapshot_title_for(issue_number)
        # Best-effort priority lookup from the same snapshot (#2899).
        # A None result renders an em-dash placeholder — identical to
        # the pre-migration-33 fallback — so a missing snapshot never
        # blocks the claim.
        issue_priority = self._latest_queue_snapshot_priority_for(issue_number)

        self._log.info(
            "daemon.candidate_picked",
            extra={
                "event": "candidate_picked",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "issue_title_captured": issue_title is not None,
                "issue_priority": issue_priority,
            },
        )

        if not self._atomic_claim(
            issue_number,
            agent_id,
            str(worktree_path),
            issue_title=issue_title,
            priority=issue_priority,
        ):
            # Race lost. Don't try the next candidate on this tick —
            # the next tick will re-scan the queue.
            return

        # From here on, any failure must move the agent to status=failed
        # so operators don't see an endlessly-"running" ghost row.
        try:
            worktree = self._create_worktree(agent_id)
        except RuntimeError as exc:
            self._log.warning(
                "daemon.worktree_create_failed",
                extra={
                    "event": "worktree_create_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="claim",
                exit_code=None,
                issue_number=issue_number,
            )
            return

        self._run_orchestration_phases(agent_id, issue_number, worktree)

    def _run_orchestration_phases(
        self, agent_id: str, issue_number: int, worktree: Path
    ) -> None:
        """Run the plan → ralph → summary → push+PR sequence.

        Shared between the fresh-claim path and the Phase 3C resume
        path (:meth:`_resume_retrying_agent`). Per-phase failure handling
        is owned by the individual ``_run_*_phase`` helpers; this
        method just wires them together.

        Between each phase a killswitch check (#2847) inspects
        :attr:`_pause_requested` — an operator cap=0 flip sets this
        event via :meth:`scheduler_tick`, and the worker thread aborts
        cleanly at the next phase boundary. ``plan`` is read-only, so
        aborting before ralph / summary / push_and_pr leaves no
        GitHub-visible artifact.
        """
        if self._check_killswitch_and_abort(agent_id, "claiming", issue_number):
            return
        ok = self._run_plan_phase(agent_id, issue_number, worktree)
        if not ok:
            return
        if self._check_killswitch_and_abort(agent_id, "planning", issue_number):
            return
        ok = self._run_ralph_phase(agent_id, issue_number, worktree)
        if not ok:
            return
        if self._check_killswitch_and_abort(agent_id, "ralph", issue_number):
            return
        ok = self._run_summary_phase(agent_id, issue_number, worktree)
        if not ok:
            return
        if self._check_killswitch_and_abort(agent_id, "summary", issue_number):
            return

        # Daemon-side git commit + push + PR create.
        self._push_and_open_pr(agent_id, issue_number, worktree)

    def _check_killswitch_and_abort(
        self,
        agent_id: str,
        after_phase: str,
        issue_number: int | None = None,
    ) -> bool:
        """Abort the run between phases if a terminal signal was observed.

        Called between each orchestration phase. Returns True iff the
        caller should stop. Two signals are checked in priority order:

        1. **Terminal agent status (#2872 Bug F).** If an external actor
           (supervisor ``_check_stuck_agents``, diagnoser
           ``_consume_action_escalate``, or circuit breaker) has
           written ``dispatcher.agents.status`` to one of
           :data:`TERMINAL_AGENT_STATUSES`, the worker must stop at
           the next phase boundary. Without this check the worker keeps
           running phases against a row already marked ``failed`` —
           producing the 2026-04-19 zombie state where the diagnoser
           wrote ``failed`` at 20:09:44 and the worker thread still
           completed a plan phase at 20:12:16 and tried to start ralph.
           Does NOT re-mark the agent terminal — whoever wrote the
           terminal status already has authority over the row.
        2. **Operator killswitch (#2847).** ``_pause_requested`` is
           set by the scheduler tick when ``concurrency_cap=0`` is
           observed. On hit: mark agent terminal with
           ``phase='paused_by_killswitch'`` and log
           ``orchestration_paused``.

        ``after_phase`` is the phase that just completed — used in the
        log event to show where in the pipeline the abort landed. For
        the pre-plan check (a killswitch / terminal that fired during
        the claim itself) pass ``"claiming"``.

        ``issue_number`` threads through to :meth:`_mark_agent_terminal`
        so the claim-interlock ``status/in-progress`` label clears on
        killswitch teardown (issue #2866). Optional for backward
        compatibility with older call sites that don't know it.
        """
        # Precedence: external-terminal check first. If the supervisor
        # or diagnoser already flipped status, the killswitch check is
        # moot — we only need one abort signal per phase boundary, and
        # the external writer's ``phase`` + ``status`` should not be
        # overwritten by the killswitch terminal.
        external_terminal = self._observe_external_terminal(agent_id)
        if external_terminal is not None:
            self._log.warning(
                "daemon.orchestration_terminated_externally",
                extra={
                    "event": "orchestration_terminated_externally",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "after_phase": after_phase,
                    "observed_status": external_terminal,
                    "detail": (
                        "worker thread observed terminal dispatcher.agents.status "
                        "written by an external actor (supervisor, diagnoser, or "
                        "circuit breaker); aborting before next phase"
                    ),
                },
            )
            return True

        if not self._pause_requested.is_set():
            return False

        # #2884: pick the terminal phase name based on how the
        # killswitch was engaged. An explicit ``force_stop`` command
        # marks the agent ``phase='force_stopped'`` so the admin
        # cockpit / retro can distinguish it from a generic cap=0
        # observation (config edit, circuit breaker, etc.) which
        # marks the agent ``phase='paused_by_killswitch'``.
        terminal_phase = (
            FORCE_STOP_TERMINAL_PHASE
            if self._force_stop_requested
            else KILLSWITCH_TERMINAL_PHASE
        )
        self._log.info(
            "daemon.orchestration_paused",
            extra={
                "event": "orchestration_paused",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "after_phase": after_phase,
                "terminal_phase": terminal_phase,
                "detail": (
                    "concurrency_cap observed as 0 mid-pipeline; aborting "
                    "before next phase to honor operator killswitch"
                ),
            },
        )
        self._mark_agent_terminal(
            agent_id,
            status="failed",
            phase=terminal_phase,
            exit_code=None,
            issue_number=issue_number,
        )
        return True

    def _observe_external_terminal(self, agent_id: str) -> str | None:
        """Return the observed terminal status if one is present.

        SELECTs ``dispatcher.agents.status`` and returns its value iff it
        falls in :data:`TERMINAL_AGENT_STATUSES`. ``None`` means the
        agent is still in a non-terminal state (``running``, ``retrying``)
        or the row cannot be read. On read failure returns ``None`` —
        preferring "assume not terminal, continue phase" over a spurious
        abort that corrupts in-flight work.

        Separate method so tests can stub the observation without
        needing a psycopg mock for the whole killswitch path. Issue
        #2872 Bug F.
        """
        assert self._conn is not None, "connect() must run before terminal check"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.observe_external_terminal_failed",
                extra={
                    "event": "observe_external_terminal_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return None
        if row is None or row[0] is None:
            return None
        status = str(row[0])
        if status in TERMINAL_AGENT_STATUSES:
            return status
        return None

    def _resume_retrying_agent(self) -> bool:
        """Pick up one ``status='retrying'`` agent, rebuild worktree, re-run.

        Phase 3C (#2791). Completes the mechanical-retry loop: the
        supervisor's retry marker processor resets agents to
        ``status='retrying' phase='claiming'`` after backoff; this
        method catches them on the next scheduler tick, flips them to
        ``running``, creates a fresh worktree, and re-runs the
        plan → ralph → summary → PR pipeline.

        Returns True when a retrying agent was picked up (the caller
        must skip the new-claim path on the same tick). Returns False
        when no retrying agent exists.
        """
        assert self._conn is not None, "connect() must run before resume"

        agent_id: str | None = None
        issue_number: int | None = None
        try:
            with self._conn.cursor() as cur:
                # Oldest retrying agent first — FIFO fairness if multiple
                # ever pile up (shouldn't at concurrency_cap=1 but cheap
                # to order the right way anyway).
                cur.execute(
                    "SELECT agent_id, issue_number FROM dispatcher.agents "
                    "WHERE status = 'retrying' "
                    "ORDER BY started_at ASC "
                    "LIMIT 1",
                )
                row = cur.fetchone()
            self._conn.commit()
            if row is None:
                return False
            agent_id = str(row[0])
            issue_number = int(row[1]) if row[1] is not None else None
        except Exception:
            self._log.exception(
                "daemon.resume_scan_failed",
                extra={
                    "event": "resume_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        if agent_id is None or issue_number is None:  # pragma: no cover — SELECT filter
            return False

        # Flip status back to ``running`` + phase ``claiming`` BEFORE
        # starting new work, so a crashed daemon mid-resume leaves a
        # normal stuck-timeout signal for the next supervisor tick to
        # pick up via the existing stuck detection path.
        #
        # #2872 — also write a fresh ``phase_transitions`` row so the
        # stuck-timeout MAX(ts) starts from "now" and the per-phase
        # threshold applies cleanly to the new run rather than the old
        # one's stale timestamp.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = 'running', phase = 'claiming' "
                    "WHERE agent_id = %s",
                    (agent_id,),
                )
                cur.execute(
                    "INSERT INTO dispatcher.phase_transitions "
                    "    (agent_id, phase) "
                    "VALUES (%s, %s)",
                    (agent_id, "resumed"),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.resume_update_failed",
                extra={
                    "event": "resume_update_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        # Fresh worktree for the retry attempt. The prior worktree was
        # dropped by ``_process_retry_markers`` — create a new one here
        # so the retrying phases see a clean tree.
        try:
            worktree = self._create_worktree(agent_id)
        except RuntimeError as exc:
            self._log.warning(
                "daemon.resume_worktree_create_failed",
                extra={
                    "event": "resume_worktree_create_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="claiming", exit_code=None
            )
            return True  # claim-slot consumed; do not also try a new issue

        self._log.info(
            "daemon.resume_retrying_agent",
            extra={
                "event": "resume_retrying_agent",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "worktree_path": str(worktree),
            },
        )

        self._run_orchestration_phases(agent_id, issue_number, worktree)
        return True

    def _plan_blocked_comment_already_posted(self, issue_number: int) -> bool | None:
        """Return True if the plan-blocked sentinel comment is already on the issue.

        Used by :meth:`_handle_plan_blocked` for idempotence — if a
        prior invocation successfully posted the comment but failed
        mid-sequence (label swap crashed, DB update crashed), the next
        run would otherwise double-post. Detects by scanning the body
        of every comment on the issue for
        :data:`PLAN_BLOCKED_COMMENT_SENTINEL`.

        Returns ``None`` on subprocess failure — the caller treats that
        as "unknown" and proceeds with the post attempt (the cost of a
        duplicate comment on a GitHub outage is lower than the cost of
        silently dropping the operator signal).
        """
        cmd = [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--json",
            "comments",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=PLAN_BLOCKED_GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.plan_blocked_sentinel_check_failed",
                extra={
                    "event": "plan_blocked_sentinel_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "error": str(exc),
                },
            )
            return None

        if result.returncode != 0:
            self._log.warning(
                "daemon.plan_blocked_sentinel_check_failed",
                extra={
                    "event": "plan_blocked_sentinel_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_preview": _stderr_tail(result.stderr),
                },
            )
            return None

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            self._log.warning(
                "daemon.plan_blocked_sentinel_check_failed",
                extra={
                    "event": "plan_blocked_sentinel_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "error": f"invalid JSON: {exc}",
                },
            )
            return None

        for comment in payload.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body") or ""
            if PLAN_BLOCKED_COMMENT_SENTINEL in body:
                return True
        return False

    def _render_plan_blocked_comment(self, agent_id: str, block_reason: str) -> str:
        """Render the plan-blocked issue comment body.

        Shape (sentinel MUST be line 1 — see issue #2857 AC2):

            <!-- dispatcher-plan-blocked -->
            ## Plan phase output — `go=false` (autonomous dispatcher run <ISO-8601>)

            The dispatcher picked this up and plan returned `go=false`. Block reason:

            > <block_reason, each line prefixed with `> `>

            Moving this out of `agent/ready` pending operator triage. Agent: `<short>`.

        The block_reason is rendered as a markdown blockquote. Each
        line is prefixed with ``> `` so multi-line reasons stay valid
        blockquote markup. Empty lines inside the reason become ``>`` to
        keep the blockquote from breaking at the first blank line.
        """
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        quoted_lines: list[str] = []
        for line in (block_reason or "").splitlines() or [""]:
            if line == "":
                quoted_lines.append(">")
            else:
                quoted_lines.append(f"> {line}")
        quoted_reason = "\n".join(quoted_lines)
        return (
            f"{PLAN_BLOCKED_COMMENT_SENTINEL}\n"
            f"## Plan phase output — `go=false` "
            f"(autonomous dispatcher run {now_iso})\n"
            f"\n"
            f"The dispatcher picked this up and plan returned "
            f"`go=false`. Block reason:\n"
            f"\n"
            f"{quoted_reason}\n"
            f"\n"
            f"Moving this out of `agent/ready` pending operator triage. "
            f"Agent: `{short_id}`.\n"
        )

    def _post_plan_blocked_comment(
        self,
        agent_id: str,
        issue_number: int,
        block_reason: str,
        worktree: Path,
    ) -> bool:
        """Post the plan-blocked issue comment via ``gh issue comment``.

        Skips the post if the sentinel is already present on the issue
        (idempotence). Returns True when the comment is present on the
        issue at return time (whether we posted it now or found it
        already there). Returns False on a hard subprocess failure so
        the caller can log and continue — label swap and DB update
        still run regardless of this return value.
        """
        already = self._plan_blocked_comment_already_posted(issue_number)
        if already is True:
            self._log.info(
                "daemon.plan_blocked_comment_skipped_idempotent",
                extra={
                    "event": "plan_blocked_comment_skipped_idempotent",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                },
            )
            return True

        body = self._render_plan_blocked_comment(agent_id, block_reason)
        body_path = worktree / "tmp" / "plan-blocked-comment.md"
        try:
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(body, encoding="utf-8")
        except OSError as exc:
            self._log.exception(
                "daemon.plan_blocked_comment_failed",
                extra={
                    "event": "plan_blocked_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "error": f"write body file: {exc}",
                },
            )
            return False

        cmd = [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--body-file",
            str(body_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=PLAN_BLOCKED_GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.exception(
                "daemon.plan_blocked_comment_failed",
                extra={
                    "event": "plan_blocked_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "error": str(exc),
                },
            )
            return False

        if result.returncode != 0:
            self._log.warning(
                "daemon.plan_blocked_comment_failed",
                extra={
                    "event": "plan_blocked_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_preview": _stderr_tail(result.stderr),
                },
            )
            return False

        self._log.info(
            "daemon.plan_blocked_comment_posted",
            extra={
                "event": "plan_blocked_comment_posted",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
            },
        )
        return True

    def _swap_plan_blocked_labels(self, agent_id: str, issue_number: int) -> bool:
        """Remove ``agent/ready`` and add ``status/triage`` via ``gh issue edit``.

        Idempotent by construction — ``gh issue edit --remove-label`` is
        a no-op when the label is already absent and ``--add-label`` is
        a no-op when the label is already present. Returns True on
        subprocess success; False on failure. Failure is logged and
        does NOT block the caller's DB update.
        """
        cmd = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--remove-label",
            "agent/ready",
            "--add-label",
            "status/triage",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=PLAN_BLOCKED_GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.exception(
                "daemon.plan_blocked_labels_failed",
                extra={
                    "event": "plan_blocked_labels_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "error": str(exc),
                },
            )
            return False

        if result.returncode != 0:
            self._log.warning(
                "daemon.plan_blocked_labels_failed",
                extra={
                    "event": "plan_blocked_labels_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_preview": _stderr_tail(result.stderr),
                },
            )
            return False

        self._log.info(
            "daemon.plan_blocked_labels_swapped",
            extra={
                "event": "plan_blocked_labels_swapped",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
            },
        )
        return True

    def _handle_plan_blocked(
        self,
        agent_id: str,
        issue_number: int,
        block_reason: str,
        worktree: Path,
    ) -> None:
        """Automation for ``plan_go_false`` with a populated ``block_reason``.

        Issue #2857. Performs three side effects in sequence; each is
        independently wrapped so a failure of one does not prevent the
        others:

        1. **Comment.** Post the standard plan-blocked comment (with the
           ``<!-- dispatcher-plan-blocked -->`` sentinel as line 1) so
           the operator can see why plan declined without opening the
           admin cockpit or CloudWatch. Idempotent: if the sentinel is
           already present, skip the post.
        2. **Labels.** Remove ``agent/ready``, add ``status/triage`` so
           the cooldown-expiry re-pickup loop stops claiming the issue.
        3. **(caller handles DB update.)** The ``_mark_agent_terminal``
           call with ``status='plan_blocked'`` runs in the caller so
           this method can remain side-effect-only and the DB write
           survives comment/label failures.

        All failures are logged with structured events; none raise. The
        contract is "fire-and-forget for the three external effects —
        the DB update is the authoritative terminal-status write".
        """
        self._post_plan_blocked_comment(agent_id, issue_number, block_reason, worktree)
        self._swap_plan_blocked_labels(agent_id, issue_number)

    # --------------------------------------------------------------------
    # #2856: needs_review handlers — summary flagged unmet AC, daemon
    # opens a DRAFT PR preserving ralph's work + posts an issue comment
    # linking the draft. Parallel structure to the plan_blocked helpers
    # above (#2857): shared sentinel pattern for idempotence, shared
    # timeout constant, independent wrap per side-effect.
    # --------------------------------------------------------------------

    @staticmethod
    def _render_unmet_criteria_pr_section(unmet_criteria: list[str]) -> str:
        """Render the ``⚠️ Unmet acceptance criteria`` PR body section.

        Issue #2856. Appended to the PR body by
        :meth:`_push_and_open_pr` on the needs_review path so the
        operator sees the summary-skill concerns inline on the draft
        PR page without cross-referencing the issue. Each unmet
        criterion is rendered verbatim as a bullet; multi-line entries
        (explanations) stay bulleted so the block renders as a flat
        list rather than a blockquote.

        Format:

            ## \u26a0\ufe0f Unmet acceptance criteria

            The summary phase flagged the following acceptance
            criteria as not satisfied by this change. Ralph produced
            reviewer-approved (SHIP) code, but this subset remains
            for operator review:

            - <criterion 1 text>
            - <criterion 2 text>
        """
        items = [(c or "").strip() for c in unmet_criteria]
        # Drop any empty entries — skill contract should never emit them
        # but defense-in-depth keeps the rendered section clean.
        items = [c for c in items if c]
        if not items:
            # Caller only invokes this when non-empty; defensive return
            # keeps the method total.
            return "## \u26a0\ufe0f Unmet acceptance criteria\n\n(none recorded)\n"
        bullets = "\n".join(f"- {c}" for c in items)
        return (
            "## \u26a0\ufe0f Unmet acceptance criteria\n"
            "\n"
            "The summary phase flagged the following acceptance "
            "criteria as not satisfied by this change. Ralph produced "
            "reviewer-approved (SHIP) code, but this subset remains "
            "for operator review:\n"
            "\n"
            f"{bullets}"
        )

    def _render_needs_review_comment(
        self,
        agent_id: str,
        pr_number: int | None,
        pr_url: str,
        unmet_criteria: list[str],
    ) -> str:
        """Render the needs_review issue comment body.

        Issue #2856. Shape (sentinel MUST be line 1 — mirrors
        :meth:`_render_plan_blocked_comment` for the plan_blocked
        handler):

            <!-- dispatcher-needs-review -->
            ## Summary flagged unmet acceptance criteria — draft PR opened for review

            Ralph completed with verdict=SHIP, but summary found AC
            that the diff did not satisfy. Opened <pr_url> as a
            **draft** so you can review before merge.

            **Unmet acceptance criteria:**

            - <criterion 1>
            - <criterion 2>

            Agent: `<short_id>`.

        The PR URL is rendered raw (GitHub autolinks it); a
        ``None``/missing URL falls back to the phrase "the dispatcher
        draft PR" so the comment still reads if URL parsing fails.
        """
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        items = [(c or "").strip() for c in unmet_criteria]
        items = [c for c in items if c]
        if items:
            bullets = "\n".join(f"- {c}" for c in items)
        else:
            bullets = "- (none recorded)"
        pr_link_phrase = (
            pr_url.strip() if pr_url and pr_url.strip() else "the dispatcher draft PR"
        )
        if (
            pr_number is not None
            and pr_number > 0
            and (not pr_url or not pr_url.strip())
        ):
            # Reconstruct a stable link when gh stdout parsing fell
            # through but we still know the PR number.
            pr_link_phrase = f"#{pr_number}"
        return (
            f"{NEEDS_REVIEW_COMMENT_SENTINEL}\n"
            f"## Summary flagged unmet acceptance criteria — draft PR "
            f"opened for review\n"
            f"\n"
            f"Ralph completed with `verdict=SHIP` but the summary "
            f"phase found acceptance criteria the diff did not "
            f"satisfy. Opened {pr_link_phrase} as a **draft** so you "
            f"can review before merge, close without merging, or "
            f"request changes.\n"
            f"\n"
            f"**Unmet acceptance criteria:**\n"
            f"\n"
            f"{bullets}\n"
            f"\n"
            f"Agent: `{short_id}`.\n"
        )

    def _needs_review_comment_already_posted(self, issue_number: int) -> bool | None:
        """Return True if the needs_review sentinel is already on the issue.

        Issue #2856. Same fail-open / "unknown → proceed" contract as
        :meth:`_plan_blocked_comment_already_posted` (#2857): a
        GitHub outage that hides the sentinel causes at worst one
        duplicate comment, which is preferable to silently dropping
        the operator signal. Detection scans every comment body for
        :data:`NEEDS_REVIEW_COMMENT_SENTINEL`.
        """
        cmd = [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--json",
            "comments",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=NEEDS_REVIEW_GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.needs_review_sentinel_check_failed",
                extra={
                    "event": "needs_review_sentinel_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "error": str(exc),
                },
            )
            return None

        if result.returncode != 0:
            self._log.warning(
                "daemon.needs_review_sentinel_check_failed",
                extra={
                    "event": "needs_review_sentinel_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_preview": _stderr_tail(result.stderr),
                },
            )
            return None

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            self._log.warning(
                "daemon.needs_review_sentinel_check_failed",
                extra={
                    "event": "needs_review_sentinel_check_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "error": f"invalid JSON: {exc}",
                },
            )
            return None

        for comment in payload.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body") or ""
            if NEEDS_REVIEW_COMMENT_SENTINEL in body:
                return True
        return False

    def _post_needs_review_comment(
        self,
        agent_id: str,
        issue_number: int,
        pr_number: int | None,
        pr_url: str,
        unmet_criteria: list[str],
        worktree: Path,
    ) -> bool:
        """Post the needs_review issue comment via ``gh issue comment``.

        Issue #2856. Idempotence: skip the post if the sentinel is
        already present on the issue. Returns True when the comment is
        present on the issue at return time (whether posted now or
        found already). Returns False on hard subprocess failure so
        the caller can log and continue — the DB terminal-status
        update still runs regardless of this return value (authoritative
        write rule, same as #2857).
        """
        already = self._needs_review_comment_already_posted(issue_number)
        if already is True:
            self._log.info(
                "daemon.needs_review_comment_skipped_idempotent",
                extra={
                    "event": "needs_review_comment_skipped_idempotent",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                },
            )
            return True

        body = self._render_needs_review_comment(
            agent_id, pr_number, pr_url, unmet_criteria
        )
        body_path = worktree / "tmp" / "needs-review-comment.md"
        try:
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(body, encoding="utf-8")
        except OSError as exc:
            self._log.exception(
                "daemon.needs_review_comment_failed",
                extra={
                    "event": "needs_review_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "error": f"write body file: {exc}",
                },
            )
            return False

        cmd = [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--body-file",
            str(body_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=NEEDS_REVIEW_GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.exception(
                "daemon.needs_review_comment_failed",
                extra={
                    "event": "needs_review_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "error": str(exc),
                },
            )
            return False

        if result.returncode != 0:
            self._log.warning(
                "daemon.needs_review_comment_failed",
                extra={
                    "event": "needs_review_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_preview": _stderr_tail(result.stderr),
                },
            )
            return False

        self._log.info(
            "daemon.needs_review_comment_posted",
            extra={
                "event": "needs_review_comment_posted",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "pr_number": pr_number,
            },
        )
        return True

    # ------------------------------------------------------------------ #
    # Plan-reuse helpers (issue #2937)                                    #
    # ------------------------------------------------------------------ #

    def _try_reuse_prior_plan(
        self,
        issue_number: int,
        issue_updated_at: str,
    ) -> tuple[dict[str, Any], str] | None:
        """Return ``(plan_output, prior_ts_str)`` if a reusable prior plan exists.

        Queries ``dispatcher.phase_outputs`` for the most recent
        successful plan output from an agent that was terminated by an
        infra-preemption event (``daemon_restart_abandoned`` or
        ``paused_by_killswitch``).  Returns ``None`` when:

        * No qualifying row exists.
        * The prior plan's ``output_json.go`` is not ``True`` (plan
          blocked or no-op — the new agent must re-evaluate).
        * The issue was updated after the prior plan ran (body edit
          may have changed scope; re-plan is required).

        The infra-preemption filter (``a.status IN ('failed', 'crashed')``
        AND ``a.phase = ANY(%s)`` matching
        :data:`_INFRA_PREEMPTION_CATEGORIES`) scopes reuse to dead agents
        that were killed by infrastructure churn, not to same-agent
        budgeted-retry successors (those never write a terminal row before
        retrying) and not to ``plan_blocked`` predecessors (their ``go``
        guard catches them even if they somehow slipped through the phase
        filter).

        Returns a ``(output_json, prior_ts_str)`` tuple on hit so the
        caller can include ``prior_plan_output_ts`` in the
        ``daemon.plan_reused`` observability event (AC #4, issue #2937).
        """
        assert self._conn is not None, "connect() must run before plan-reuse query"

        infra_phases = list(_INFRA_PREEMPTION_CATEGORIES)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT po.output_json, po.ts "
                    "FROM dispatcher.phase_outputs po "
                    "JOIN dispatcher.agents a ON a.agent_id = po.agent_id "
                    "WHERE a.issue_number = %s "
                    "  AND po.phase = 'plan' "
                    "  AND a.status IN ('failed', 'crashed') "
                    "  AND a.phase = ANY(%s) "
                    "ORDER BY po.ts DESC LIMIT 1",
                    (issue_number, infra_phases),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.plan_reuse_query_failed",
                extra={
                    "event": "plan_reuse_query_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        if row is None:
            return None

        raw_output_json, prior_ts = row[0], row[1]

        # Parse output_json — may arrive as a string (psycopg2 behaviour
        # on JSONB columns without the extras codec registered) or as a
        # dict (psycopg3 default).
        if isinstance(raw_output_json, str):
            try:
                prior_output: dict[str, Any] = json.loads(raw_output_json)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw_output_json, dict):
            prior_output = raw_output_json
        else:
            return None

        # Only reuse plans that said "go ahead".  A plan that returned
        # go=false must be re-evaluated — the issue may have since been
        # unblocked or the scope decision may have changed.
        if not prior_output.get("go"):
            return None

        # Invalidate if the issue body was updated after the plan ran.
        # ``issue_updated_at`` and ``prior_ts`` are both ISO-8601 strings;
        # simple lexicographic comparison is correct for the UTC subset
        # that GitHub and PostgreSQL both emit (YYYY-MM-DDTHH:MM:SSZ form).
        prior_ts_str = (
            prior_ts.isoformat()
            if hasattr(prior_ts, "isoformat")
            else str(prior_ts or "")
        )
        if issue_updated_at and prior_ts is not None:
            if issue_updated_at > prior_ts_str:
                return None

        return prior_output, prior_ts_str

    def _materialize_plan_output(
        self,
        worktree: Path,
        plan_output: dict[str, Any],
    ) -> None:
        """Write a reused plan to ``{worktree}/tmp/dispatcher-output/plan.json``.

        Mirrors the artifact that the real plan subprocess produces so
        any downstream code that reads the file directly (e.g. scripts,
        admin tooling) sees an identical structure regardless of whether
        the plan was freshly generated or reused from a prior agent's
        run (#2937).
        """
        output_dir = worktree / "tmp" / "dispatcher-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "plan.json"
        output_path.write_text(json.dumps(plan_output, indent=2, default=str))

    def _fetch_prior_attempts(self, issue_number: int) -> list[dict[str, Any]]:
        """Return up to 3 prior non-infra-preempted failed agents for *issue_number*.

        Queries ``dispatcher.agents`` LEFT JOIN ``dispatcher.phase_outputs``
        (twice — once for the ``ralph`` phase log, once for the
        ``push_and_pr`` phase log) and returns rows for agents whose terminal
        phase is NOT in :data:`_INFRA_PREEMPTION_CATEGORIES` (i.e. real
        budgeted retries: ``subprocess_crash``, ``stuck_timeout``,
        ``gh_rate_exhausted``, ``operator_retry``).

        Returns an empty list on any DB error (fail-open — the spawn
        continues without prior context).

        Each returned dict has keys:

        * ``failure_summary`` — ``a.failure_summary`` (may be ``None``).
        * ``ralph_log_text``  — ``po.log_text`` for the ralph phase (may be ``None``).
        * ``push_log_text``   — ``pp.log_text`` for the push_and_pr phase (may be ``None``).
        * ``started_at``      — ``a.started_at`` as a string (ISO-8601).
        """
        assert self._conn is not None, "connect() must run before prior-attempts query"

        non_infra_phases = list(_INFRA_PREEMPTION_CATEGORIES)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT "
                    "  a.failure_summary, "
                    "  po.log_text, "
                    "  pp.log_text, "
                    "  a.started_at "
                    "FROM dispatcher.agents a "
                    "LEFT JOIN dispatcher.phase_outputs po "
                    "  ON po.agent_id = a.agent_id AND po.phase = 'ralph' "
                    "LEFT JOIN dispatcher.phase_outputs pp "
                    "  ON pp.agent_id = a.agent_id AND pp.phase = 'push_and_pr' "
                    "WHERE a.issue_number = %s "
                    "  AND a.status IN ('failed', 'crashed') "
                    "  AND a.phase != ALL(%s) "
                    "ORDER BY a.started_at DESC "
                    "LIMIT 3",
                    (issue_number, non_infra_phases),
                )
                rows = cur.fetchall()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.prior_attempts_query_failed",
                extra={
                    "event": "prior_attempts_query_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return []

        results: list[dict[str, Any]] = []
        for row in rows:
            failure_summary, ralph_log, push_log, started_at = row
            started_at_str = (
                started_at.isoformat()
                if hasattr(started_at, "isoformat")
                else str(started_at or "")
            )
            results.append(
                {
                    "failure_summary": failure_summary,
                    "ralph_log_text": ralph_log,
                    "push_log_text": push_log,
                    "started_at": started_at_str,
                }
            )
        return results

    def _materialize_prior_attempts(self, worktree: Path, issue_number: int) -> int:
        """Write ``prior_attempts.md`` to the worktree and return the attempt count.

        Fetches up to 3 prior non-infra-preempted failures via
        :meth:`_fetch_prior_attempts` and formats them into a markdown file at
        ``{worktree}/tmp/dispatcher-output/prior_attempts.md`` so the ralph
        skill can surface them to fresh workers.

        **When the count is 0 the file is NOT written** — the first-attempt
        path must not receive a stale or empty ``prior_attempts.md`` that could
        confuse the worker.

        The markdown shape for each attempt::

            ## Prior attempt N (started <ISO-ts>)

            **Category:** <failure_summary or "unknown">

            **Pre-push failure tail:**

            <last ~2000 chars of push_and_pr log, or "(none)">

            **Ralph iteration narrative:**

            <## Iteration feedback section extracted from ralph log, or "(none)">

        Returns the number of attempts written (0 if none).
        """
        attempts = self._fetch_prior_attempts(issue_number)
        if not attempts:
            return 0

        sections: list[str] = []
        for i, attempt in enumerate(attempts, start=1):
            failure_summary = attempt.get("failure_summary") or "unknown"
            started_at = attempt.get("started_at") or "unknown"

            # Push-and-pr log tail (~2000 chars).
            push_log = attempt.get("push_log_text") or ""
            if push_log:
                push_tail = push_log[-2000:] if len(push_log) > 2000 else push_log
            else:
                push_tail = "(none)"

            # Extract the ## Iteration feedback section from the ralph log.
            ralph_log = attempt.get("ralph_log_text") or ""
            ralph_narrative = "(none)"
            if ralph_log:
                marker = "## Iteration feedback"
                idx = ralph_log.find(marker)
                if idx != -1:
                    ralph_narrative = ralph_log[idx:]

            section = (
                f"## Prior attempt {i} (started {started_at})\n\n"
                f"**Category:** {failure_summary}\n\n"
                f"**Pre-push failure tail:**\n\n"
                f"{push_tail}\n\n"
                f"**Ralph iteration narrative:**\n\n"
                f"{ralph_narrative}"
            )
            sections.append(section)

        content = "\n\n---\n\n".join(sections)
        output_dir = worktree / "tmp" / "dispatcher-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "prior_attempts.md").write_text(content, encoding="utf-8")
        return len(attempts)

    def _append_unapplied_patch_to_prior_attempts(
        self,
        worktree: Path,
        patch_info: dict[str, Any],
        *,
        issue_number: int | None = None,
    ) -> None:
        """Append an unapplied prior ralph patch to ``prior_attempts.md``.

        Called from :meth:`_run_ralph_phase` when
        :meth:`_apply_prior_ralph_patch` returned ``{"applied": False}``.

        Two branches (#3026):

        * **Conflict branch** (``patch_info["conflicted"] == True``):
          the daemon LEFT the worktree in the ``git am --3way``
          conflict state (per the #3026 conflict-handoff contract).
          The section written is the structured "RESUME WITH
          CONFLICT" block from
          :meth:`_format_resume_with_conflict_block` — ralph reads
          the heading and handles the ``git am --continue`` vs.
          ``git am --abort`` decision on its own.

        * **Invocation-failure branch** (``patch_info["conflicted"] ==
          False``): ``git am`` failed before conflict resolution
          started (invocation error, corrupt patch). The worktree is
          aborted clean; the patch text is surfaced verbatim so ralph
          can cherry-pick manually.

        Appends (creates if missing) to
        ``{worktree}/tmp/dispatcher-output/prior_attempts.md`` so the
        ralph skill's existing prior-attempts surfacing picks it up
        verbatim. Best-effort — a write failure just means ralph
        misses the patch context, which is the pre-#3012 status quo.
        """
        output_dir = worktree / "tmp" / "dispatcher-output"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        target = output_dir / "prior_attempts.md"
        patch_content = patch_info.get("patch_content") or ""
        patch_id = patch_info.get("patch_id") or "unknown"
        reason = patch_info.get("reason") or "unknown"
        conflicted = bool(patch_info.get("conflicted"))

        if conflicted:
            # Conflict-handoff path — surface the structured
            # RESUME WITH CONFLICT prompt block so ralph's worker
            # handles the continue-vs-abort judgment. See
            # ``.claude/skills/task-v2-ralph/SKILL.md`` §"Resume with
            # conflict" for the contract.
            effective_issue = issue_number if issue_number is not None else 0
            resume_block = self._format_resume_with_conflict_block(
                effective_issue, patch_info
            )
            section = (
                f"\n\n---\n\n## {resume_block.splitlines()[0]}\n\n"
                f"**Patch id:** {patch_id}\n\n"
                f"**Apply failure reason:** {reason}\n\n"
                f"{resume_block}\n\n"
                f"Reference — the patch bytes follow (the worktree already has"
                f" these partially applied in the am-in-progress state; this"
                f" block is for manual inspection if you choose `git am"
                f" --abort`):\n\n"
                f"```diff\n{patch_content}\n```\n"
            )
        else:
            # Legacy non-conflict failure path (invocation error,
            # corrupt patch). Worktree is clean; patch is advisory.
            section = (
                f"\n\n---\n\n## Prior ralph patch (did NOT apply cleanly)\n\n"
                f"**Patch id:** {patch_id}\n\n"
                f"**Apply failure reason:** {reason}\n\n"
                f"A previous agent's ralph iteration was saved but the daemon"
                f" could not ``git am`` it onto the fresh worktree (invocation"
                f" error, corrupt patch). The worktree was aborted clean — you"
                f" are starting from origin/main. The patch text below is"
                f" advisory; cherry-pick or re-derive the relevant hunks"
                f" manually if they look useful.\n\n"
                f"```diff\n{patch_content}\n```\n"
            )

        try:
            if target.exists():
                existing = target.read_text(encoding="utf-8")
                target.write_text(existing + section, encoding="utf-8")
            else:
                # No prior_attempts.md yet — write a standalone file
                # with just the patch section. Strip the leading
                # newlines + separator so the first-section case looks
                # clean.
                target.write_text(section.lstrip("\n-").lstrip(), encoding="utf-8")
        except OSError:  # pragma: no cover — defensive
            return

    def _run_plan_phase(self, agent_id: str, issue_number: int, worktree: Path) -> bool:
        """Run ``/task-v2-plan``. Returns True to continue, False to stop."""
        self._update_agent_phase(agent_id, "planning")

        try:
            bundle = self._fetch_issue_bundle(issue_number)
        except RuntimeError as exc:
            self._log.warning(
                "daemon.issue_fetch_failed",
                extra={
                    "event": "issue_fetch_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="planning",
                exit_code=None,
                issue_number=issue_number,
            )
            return False

        # ── Plan-reuse short-circuit (#2937) ──────────────────────────────
        # Before spawning the expensive Opus plan subprocess, check whether
        # a prior agent ran plan successfully on this same issue and was
        # terminated by an infra-preemption event (daemon restart,
        # killswitch cap flip).  If the issue hasn't been edited since that
        # prior plan and the prior plan said go=True, reuse it directly —
        # no subprocess spawn, no Opus cost.
        issue_updated_at: str = bundle.get("issue_updated_at", "")
        reuse_result = self._try_reuse_prior_plan(issue_number, issue_updated_at)
        if reuse_result is not None:
            reused, prior_ts_str = reuse_result
            self._persist_phase_output(
                agent_id,
                "plan",
                reused,
                log_text="<reused from prior plan>",
                usage=None,
            )
            self._materialize_plan_output(worktree, reused)
            self._log.info(
                "daemon.plan_reused",
                extra={
                    "event": "plan_reused",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "prior_plan_output_ts": prior_ts_str,
                },
            )
            self._agent_plan_output = reused
            return True
        # ── end plan-reuse ─────────────────────────────────────────────────

        plan_input = {
            "agent_id": agent_id,
            "worktree_path": str(worktree),
            "repo_root": str(worktree),
            **bundle,
        }
        self._write_phase_input(worktree, "plan", plan_input)

        exit_code = self._run_subprocess_or_fail(
            agent_id, "plan", worktree, issue_number=issue_number
        )
        if exit_code is None:
            return False

        plan_output = self._read_phase_output(worktree, "plan")
        if plan_output is None:
            extra: dict[str, Any] = {
                "event": "phase_output_missing",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "plan",
            }
            preview = self._extract_log_preview(worktree, "plan")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.phase_output_missing", extra=extra)
            # Issue #3032: route through the unified failure handler
            # so the diagnoser can pick up the missing-output failure
            # on the next supervisor tick.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="planning",
                category=FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
                stderr_tail=preview or "",
                exit_code=exit_code,
                details={"missing_phase_output": "plan"},
                issue_number=issue_number,
            )
            return False

        self._persist_phase_output(
            agent_id,
            "plan",
            plan_output,
            log_text=self._read_full_phase_log(worktree, "plan") or None,
            usage=self._parse_phase_usage(worktree, "plan"),
        )
        self._log.info(
            "daemon.phase_succeeded",
            extra={
                "event": "phase_succeeded",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "plan",
                "exit_code": exit_code,
                "go": plan_output.get("go"),
            },
        )

        # Branch on plan.go — false ends orchestration.
        if not plan_output.get("go"):
            reason = plan_output.get("block_reason") or ""
            # A missing block_reason means "no work needed" —
            # succeed. A populated block_reason indicates plan
            # correctly declined to proceed ("plan_blocked" — #2857).
            # Distinct from ``failed`` which is reserved for real
            # infrastructure / subprocess failures so the admin
            # cockpit and reporting can separate correct-outcome
            # triage from genuine breakage.
            status = "plan_blocked" if reason else "succeeded"
            self._log.info(
                "daemon.plan_go_false",
                extra={
                    "event": "plan_go_false",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "block_reason": reason,
                    "terminal_status": status,
                },
            )
            if status == "plan_blocked":
                # Side-effects: comment + label swap. Run BEFORE the DB
                # update so a DB crash can't lose the operator-visible
                # work. Each side-effect is individually wrapped so a
                # failure of one does not prevent the others.
                self._handle_plan_blocked(agent_id, issue_number, reason, worktree)
            self._mark_agent_terminal(
                agent_id,
                status=status,
                phase="planning",
                exit_code=exit_code,
                issue_number=issue_number,
            )
            return False

        # Stash plan output on the agent for ralph + summary to reuse.
        self._agent_plan_output = plan_output
        return True

    def _run_ralph_phase(
        self, agent_id: str, issue_number: int, worktree: Path
    ) -> bool:
        """Run ``/task-v2-ralph``. Returns True to continue, False to stop."""
        self._update_agent_phase(agent_id, "ralph")

        # ── Prior-SHIP'd patch inheritance (#3012) ─────────────────────────
        # If a prior agent on this issue SHIPped but never got a PR
        # created (daemon restart between ralph exit and ``gh pr
        # create``), the patch is sitting in ``dispatcher.ralph_patches``.
        # Try to apply it to the fresh worktree so ralph iterates on top
        # of the inherited diff rather than re-ralph'ing from scratch.
        #
        # ``prior_patch_info`` shape:
        #   None                              — no prior patch (first attempt)
        #   {"applied": True, ...}            — patch applied; HEAD has the diff
        #   {"applied": False, "patch_content": ..., "reason": ...}
        #                                     — apply failed; patch text is
        #                                       included so _materialize_prior_attempts
        #                                       can surface it to ralph
        prior_patch_info = self._apply_prior_ralph_patch(
            agent_id, issue_number, worktree
        )
        # ── end prior-SHIP'd patch inheritance ─────────────────────────────

        # ── Prior-attempt context (#2984) ──────────────────────────────────
        # Before seeding the ralph input bundle, materialize prior failed
        # attempts (non-infra-preempted) into prior_attempts.md so the
        # /task-v2-ralph skill can surface them to fresh workers.  The call
        # is a no-op (returns 0, writes no file) on first-attempt spawns.
        prior_attempts_count = self._materialize_prior_attempts(worktree, issue_number)
        # If a prior SHIP'd patch failed to apply cleanly, append its
        # text to prior_attempts.md so ralph can see the intended diff
        # and cherry-pick / re-derive manually.
        if prior_patch_info is not None and not prior_patch_info.get("applied"):
            self._append_unapplied_patch_to_prior_attempts(
                worktree, prior_patch_info, issue_number=issue_number
            )
        self._log.info(
            "daemon.ralph_spawn",
            extra={
                "event": "ralph_spawn",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "prior_attempts_count": prior_attempts_count,
                "prior_patch_applied": (
                    bool(prior_patch_info and prior_patch_info.get("applied"))
                ),
                "prior_patch_conflicted": (
                    bool(prior_patch_info and prior_patch_info.get("conflicted"))
                ),
                "prior_patch_id": (
                    prior_patch_info.get("patch_id") if prior_patch_info else None
                ),
            },
        )
        # ── end prior-attempt context ──────────────────────────────────────

        plan_output = self._agent_plan_output or {}
        ralph_input = {
            "agent_id": agent_id,
            "issue_number": issue_number,
            "plan": plan_output,
            "worktree_path": str(worktree),
            "repo_root": str(worktree),
            "max_iterations": 5,
            "dependencies_installed": plan_output.get("dependencies_to_install", []),
        }
        self._write_phase_input(worktree, "ralph", ralph_input)

        exit_code = self._run_subprocess_or_fail(
            agent_id, "ralph", worktree, issue_number=issue_number
        )
        if exit_code is None:
            return False

        ralph_output = self._read_phase_output(worktree, "ralph")
        if ralph_output is None:
            extra: dict[str, Any] = {
                "event": "phase_output_missing",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "ralph",
            }
            preview = self._extract_log_preview(worktree, "ralph")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.phase_output_missing", extra=extra)
            # Issue #3032: route through the unified failure handler.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="ralph",
                category=FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
                stderr_tail=preview or "",
                exit_code=exit_code,
                details={"missing_phase_output": "ralph"},
                issue_number=issue_number,
            )
            return False

        self._persist_phase_output(
            agent_id,
            "ralph",
            ralph_output,
            log_text=self._read_full_phase_log(worktree, "ralph") or None,
            usage=self._parse_phase_usage(worktree, "ralph"),
        )
        verdict = ralph_output.get("verdict", "")
        self._log.info(
            "daemon.phase_succeeded",
            extra={
                "event": "phase_succeeded",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "ralph",
                "exit_code": exit_code,
                "verdict": verdict,
                "iterations_used": ralph_output.get("iterations_used"),
            },
        )

        if verdict == "AC_INFEASIBLE":
            # Issue #3010 — ralph surfaced a structurally-impossible AC.
            # Route to the diagnoser (Tier 3) immediately; no summary,
            # no push_and_pr, no mechanical retry. Ralph's worktree
            # diff (if any) is discarded on diagnoser handoff.
            infeasible_acs = ralph_output.get("infeasible_acs") or []
            # Normalize to a list of dicts with int-coerced indices so
            # the diagnoser's context bundle has a clean shape even if
            # the skill emitted strings or a single dict.
            normalized_infeasible: list[dict[str, Any]] = []
            if isinstance(infeasible_acs, list):
                for entry in infeasible_acs:
                    if not isinstance(entry, dict):
                        continue
                    idx = entry.get("index")
                    try:
                        idx_int = int(idx) if idx is not None else None
                    except (TypeError, ValueError):
                        idx_int = None
                    evidence = entry.get("evidence")
                    normalized_infeasible.append(
                        {
                            "index": idx_int,
                            "evidence": str(evidence) if evidence is not None else "",
                        }
                    )
            self._log.info(
                "daemon.ralph_ac_infeasible",
                extra={
                    "event": "ralph_ac_infeasible",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "infeasible_acs_count": len(normalized_infeasible),
                },
            )
            self._write_failure(
                agent_id=agent_id,
                category=FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
                detected_by="ralph_output_parse",
                details={
                    "infeasible_acs": normalized_infeasible,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="ralph",
                exit_code=exit_code,
                issue_number=issue_number,
            )
            return False

        if verdict != "SHIP":
            self._log.info(
                "daemon.ralph_not_ship",
                extra={
                    "event": "ralph_not_ship",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "verdict": verdict,
                    "block_reason": ralph_output.get("block_reason"),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="ralph",
                exit_code=exit_code,
                issue_number=issue_number,
            )
            return False

        # ── SHIP'd — persist the patch (#3012) ─────────────────────────────
        # Capture ralph's SHIP'd diff to ``dispatcher.ralph_patches`` so a
        # daemon restart between here and a successful ``gh pr create``
        # does not lose the work. Supersedes any prior row for the same
        # issue (covers retry-after-failure). Returns None on capture /
        # DB failure; that's non-fatal — the happy path continues and
        # pre-#3012 behavior resumes (loss on daemon restart).
        self._capture_and_persist_ralph_patch(agent_id, issue_number, worktree)
        # ── end SHIP patch persist ─────────────────────────────────────────

        self._agent_ralph_output = ralph_output
        return True

    def _run_summary_phase(
        self, agent_id: str, issue_number: int, worktree: Path
    ) -> bool:
        """Run ``/task-v2-summary``. Returns True to continue, False to stop."""
        self._update_agent_phase(agent_id, "summary")

        # Capture the git diff from the ralph-produced worktree for the
        # summary phase to map back to AC. The diff is against
        # ``origin/main`` since the agent branch was just created from
        # it in ``_create_worktree``.
        try:
            diff_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "diff",
                    "origin/main...HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            git_diff = diff_result.stdout if diff_result.returncode == 0 else ""
        except Exception:  # pragma: no cover — defensive; not asserted in unit tests
            git_diff = ""

        ralph_output = self._agent_ralph_output or {}
        changed_files = ralph_output.get("changed_files", []) or []
        # Fall back to a git-state read if ralph didn't populate
        # changed_files (non-testable short-circuit path).
        if not changed_files:
            try:
                status_result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(worktree),
                        "diff",
                        "--name-only",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if status_result.returncode == 0:
                    changed_files = [
                        ln.strip()
                        for ln in (status_result.stdout or "").splitlines()
                        if ln.strip()
                    ]
            except Exception:  # pragma: no cover
                pass

        # Refetch issue body so a mid-flight edit is reflected in the
        # summary's AC mapping. Best-effort — fall back to empty on
        # failure, the summary skill tolerates it.
        try:
            bundle = self._fetch_issue_bundle(issue_number)
        except RuntimeError:
            bundle = {
                "issue_number": issue_number,
                "issue_title": "",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }

        plan_output = self._agent_plan_output or {}
        # Branch name matches ``_create_worktree``'s naming convention.
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        branch = f"agent/{short_id}"

        summary_input = {
            "agent_id": agent_id,
            "issue_number": issue_number,
            "issue_title": bundle.get("issue_title", ""),
            "issue_body": bundle.get("issue_body", ""),
            "issue_comments": bundle.get("issue_comments", []),
            "ralph_summary": ralph_output.get("summary", ""),
            "changed_files": changed_files,
            "git_diff": git_diff,
            "worktree_path": str(worktree),
            "repo_root": str(worktree),
            "branch": branch,
            "plan_acceptance_criteria": plan_output.get("acceptance_criteria", []),
            "scope_check": plan_output.get("scope_check", []),
        }
        self._write_phase_input(worktree, "summary", summary_input)

        exit_code = self._run_subprocess_or_fail(
            agent_id, "summary", worktree, issue_number=issue_number
        )
        if exit_code is None:
            return False

        summary_output = self._read_phase_output(worktree, "summary")
        if summary_output is None:
            extra: dict[str, Any] = {
                "event": "phase_output_missing",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "summary",
            }
            preview = self._extract_log_preview(worktree, "summary")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.phase_output_missing", extra=extra)
            # Issue #3032: route through the unified failure handler.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="summary",
                category=FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
                stderr_tail=preview or "",
                exit_code=exit_code,
                details={"missing_phase_output": "summary"},
                issue_number=issue_number,
            )
            return False

        self._persist_phase_output(
            agent_id,
            "summary",
            summary_output,
            log_text=self._read_full_phase_log(worktree, "summary") or None,
            usage=self._parse_phase_usage(worktree, "summary"),
        )
        self._log.info(
            "daemon.phase_succeeded",
            extra={
                "event": "phase_succeeded",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "summary",
                "exit_code": exit_code,
            },
        )

        summary_verdict = str(summary_output.get("verdict") or "").upper()
        if summary_verdict == "AC_INFEASIBLE":
            # Issue #3010 — summary found a structurally-impossible AC
            # after ralph already shipped. Ralph's diff is discarded;
            # daemon writes a failure row and routes to the diagnoser
            # (Tier 3). The diagnoser bundle carries ralph's diff +
            # summary's AC mapping so the reissue rewrite can align
            # with what ralph already built.
            infeasible_acs_raw = summary_output.get("infeasible_acs") or []
            normalized_infeasible: list[dict[str, Any]] = []
            if isinstance(infeasible_acs_raw, list):
                for entry in infeasible_acs_raw:
                    if not isinstance(entry, dict):
                        continue
                    idx = entry.get("index")
                    try:
                        idx_int = int(idx) if idx is not None else None
                    except (TypeError, ValueError):
                        idx_int = None
                    evidence = entry.get("evidence")
                    normalized_infeasible.append(
                        {
                            "index": idx_int,
                            "evidence": str(evidence) if evidence is not None else "",
                        }
                    )
            self._log.info(
                "daemon.summary_ac_infeasible",
                extra={
                    "event": "summary_ac_infeasible",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "infeasible_acs_count": len(normalized_infeasible),
                },
            )
            self._write_failure(
                agent_id=agent_id,
                category=FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
                detected_by="summary_output_parse",
                details={
                    "infeasible_acs": normalized_infeasible,
                    "deferred_acs": summary_output.get("deferred_acs") or [],
                    "ralph_diff": git_diff,
                    "summary_ac_mapping": summary_output.get("ac_mapping") or [],
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="summary",
                exit_code=exit_code,
                issue_number=issue_number,
            )
            return False

        unmet = summary_output.get("unmet_criteria") or []
        if unmet:
            # #2856: previously this path hard-failed and discarded all
            # of ralph's work, even when ralph produced reviewer-approved
            # (verdict=SHIP) code. Now we stash the unmet list and
            # proceed to _push_and_open_pr which opens the PR as a
            # DRAFT, appends an "Unmet acceptance criteria" section to
            # the body, posts an issue comment linking the draft, and
            # terminates the agent as ``status='needs_review'``
            # (distinct from ``failed``). The draft PR sits for
            # operator triage — auto-merge does NOT touch drafts, and
            # ``needs_review`` is NOT in ``_list_advanceable_agents``'s
            # SELECT so the supervisor tick leaves it alone.
            self._log.info(
                "daemon.summary_unmet_criteria",
                extra={
                    "event": "summary_unmet_criteria",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "unmet_criteria": unmet,
                    "terminal_status": "needs_review",
                },
            )
            # Coerce to list[str] so downstream rendering is total on
            # malformed skill output (non-string entries get stringified
            # rather than crashing the orchestration).
            self._agent_unmet_criteria = [str(u) for u in unmet]

        self._agent_summary_output = summary_output
        return True

    def _run_subprocess_or_fail(
        self,
        agent_id: str,
        phase: str,
        worktree: Path,
        issue_number: int | None = None,
    ) -> int | None:
        """Run ``claude -p <phase>`` and log the outcome.

        Returns the exit code on clean subprocess exit (even a non-zero
        one — per-phase skills always exit 0 on structured-output errors
        so any non-zero code is an infrastructure failure). Returns
        ``None`` on subprocess timeout or other non-exit-code failure
        modes, AND marks the agent failed.

        **Phase 3C (#2791):** non-zero exits are classified into the §8
        tier-1 category table (``subprocess_crash``,
        ``subprocess_turn_limit``, ``subprocess_auth_fail``) via
        :meth:`_classify_subprocess_failure`. Tier-1 auto-retry
        categories (currently ``subprocess_crash``) get a retry marker
        enqueued so the next supervisor tick re-arms the agent with a
        fresh worktree. Tier-2/3 categories (``turn_limit`` → 3D;
        ``auth_fail`` → halt) leave the agent in ``status='failed'``
        for 3D's diagnoser to pick up.

        ``issue_number`` threads through to :meth:`_mark_agent_terminal`
        so the ``status/in-progress`` label clears on teardown
        (issue #2866). Optional for backward compatibility with call
        sites that don't know it yet.
        """
        try:
            exit_code, duration = self._spawn_phase_subprocess(
                phase, worktree, agent_id
            )
        except subprocess.TimeoutExpired:
            # Timeout = subprocess runaway (spec §17 Risk 4). Treat as
            # a generic ``subprocess_crash`` — the subprocess didn't
            # actually crash but the next retry needs a fresh worktree
            # just the same. Capture the log tail for triage — a runaway
            # ralph spinning on a failing test still produces useful log
            # output before the timeout fires.
            tail = self._log_tail(
                worktree, phase, max_chars=PHASE_STDERR_TAIL_MAX_CHARS
            )
            self._handle_subprocess_failure(
                agent_id=agent_id,
                phase=phase,
                reason="timeout",
                exit_code=None,
                stderr_tail=tail,
                duration_s=None,
                extra={"timeout_seconds": CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS},
                worktree=worktree,
                issue_number=issue_number,
            )
            return None
        except FileNotFoundError:
            # ``claude`` not on PATH is a dispatcher-image problem, not
            # an agent problem. Still write the failure row so operators
            # see it on the admin page; do NOT enqueue a retry — the
            # next attempt will hit the same missing binary.
            self._log.error(
                "daemon.subprocess_failed",
                extra={
                    "event": "subprocess_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": phase,
                    "reason": "claude_not_on_path",
                },
            )
            self._write_failure(
                agent_id=agent_id,
                category=FAILURE_CATEGORY_SUBPROCESS_CRASH,
                detected_by="scheduler",
                details={
                    "phase": phase,
                    "reason": "claude_not_on_path",
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase=phase,
                exit_code=None,
                issue_number=issue_number,
            )
            return None
        except Exception as exc:  # pragma: no cover — defensive catch
            self._log.exception(
                "daemon.subprocess_failed",
                extra={
                    "event": "subprocess_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": phase,
                    "reason": "unhandled",
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase=phase,
                exit_code=None,
                issue_number=issue_number,
            )
            return None

        if exit_code != 0:
            # Per-phase skills always exit 0 — a non-zero code is an
            # infra failure (claude-p crash, OOM, harness error, auth
            # error, turn-limit trip). Tail the log for forensic
            # context but don't include verbatim in the structured
            # log envelope (may contain secrets). The classifier only
            # sees a short tail so a bad regex cannot echo a secret.
            # Tail size matches :data:`PHASE_STDERR_TAIL_MAX_CHARS` —
            # 500 was too tight for noisy failures where the useful
            # "failed: X" line appears early (#2821).
            tail = self._log_tail(
                worktree, phase, max_chars=PHASE_STDERR_TAIL_MAX_CHARS
            )
            self._handle_subprocess_failure(
                agent_id=agent_id,
                phase=phase,
                reason="nonzero_exit",
                exit_code=exit_code,
                stderr_tail=tail,
                duration_s=duration,
                extra={},
                worktree=worktree,
                issue_number=issue_number,
            )
            return None

        self._log.info(
            "daemon.phase_exited",
            extra={
                "event": "phase_exited",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": phase,
                "exit_code": exit_code,
                "duration_s": round(duration, 2),
            },
        )
        return exit_code

    def _handle_subprocess_failure(
        self,
        *,
        agent_id: str,
        phase: str,
        reason: str,
        exit_code: int | None,
        stderr_tail: str,
        duration_s: float | None,
        extra: dict[str, Any],
        worktree: Path | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Classify a subprocess failure, write failure row, enqueue retry.

        Phase 3C (#2791). Centralized so ``_run_subprocess_or_fail``'s
        timeout / non-zero-exit branches share the same structured-log +
        failure-insert + retry-marker logic.

        Auto-retries ``subprocess_crash`` (tier 1). Tier 2/3 categories
        (``subprocess_turn_limit``, ``subprocess_auth_fail``) land a
        failure row but leave the agent in ``status='failed'`` so 3D's
        diagnoser can pick up. Future: 3D may flip turn-limit to a
        single retry-with-hint — the classifier already returns the
        distinct category so that wiring is a one-line change.

        When ``worktree`` is provided, a secondary
        ``daemon.phase_failure_log`` event is emitted carrying up to
        :data:`PHASE_FAILURE_LOG_MAX_CHARS` chars of the full phase log
        body (#2821). Both events share ``agent_id`` so a single
        CloudWatch filter-log-events query returns the pair. The full
        log is the "click-in" payload for triage — the primary event's
        ``stderr_tail`` stays narrow so filter queries against the
        primary event don't double-scan the full body.
        """
        # ``claude`` is the Phase-3 default runner. When multi-runner
        # support lands (per dispatcher.config.runner_by_phase), this
        # will read the agent's runner override — until then the
        # classifier's claude-first path is always correct.
        category = self._classify_subprocess_failure(
            runner="claude",
            exit_code=int(exit_code) if exit_code is not None else 0,
            stderr_tail=stderr_tail,
        )

        # Build the failure-log envelope. Keep the stderr tail in the
        # row payload (JSONB) rather than the top-level log message so
        # CloudWatch Insights queries can filter it out when scanning
        # for recurring categories.
        log_extra: dict[str, Any] = {
            "event": "subprocess_failed",
            "run_id": self._run_id,
            "agent_id": agent_id,
            "phase": phase,
            "reason": reason,
            "category": category,
        }
        if exit_code is not None:
            log_extra["exit_code"] = int(exit_code)
        if duration_s is not None:
            log_extra["duration_s"] = round(duration_s, 2)
        log_extra.update(extra)
        log_extra["stderr_tail"] = stderr_tail
        self._log.warning("daemon.subprocess_failed", extra=log_extra)

        # Secondary event: full-log dump for triage (#2821). Emitted
        # when we have a worktree reference (all real failure paths) and
        # the log file actually has content. Cap at 10k chars — the tail
        # is preserved because failures surface at the tail, not the
        # head, and merged stdout+stderr logs commonly start with env
        # dumps or prompt echoes that are noise.
        if worktree is not None:
            self._emit_phase_failure_log_event(
                agent_id=agent_id, phase=phase, worktree=worktree
            )

        self._write_failure(
            agent_id=agent_id,
            category=category,
            detected_by="scheduler",
            details={
                "phase": phase,
                "reason": reason,
                "exit_code": int(exit_code) if exit_code is not None else None,
                "duration_s": round(duration_s, 2) if duration_s is not None else None,
                "stderr_tail": stderr_tail,
                **extra,
            },
        )

        # Status transition — tier-1 retry categories still move to
        # ``failed`` temporarily; the retry marker processor flips the
        # agent back to ``retrying`` when the backoff elapses. Tier-2/3
        # categories stay in ``failed`` for 3D.
        self._mark_agent_terminal(
            agent_id,
            status="failed",
            phase=phase,
            exit_code=int(exit_code) if exit_code is not None else None,
            issue_number=issue_number,
        )

        if category in AUTO_RETRY_CATEGORIES:
            self._create_retry_marker(agent_id=agent_id, reason=category)

    def _handle_agent_failure(
        self,
        *,
        agent_id: str,
        phase: str,
        category: str,
        stderr_tail: str,
        exit_code: int | None,
        details: dict[str, Any] | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Unified agent-terminal failure exit path (issue #3032).

        Writes a ``dispatcher.failures`` row AND marks the agent
        terminal, so the next ``_run_diagnoser_pass`` (supervisor tick)
        picks it up via :meth:`_find_diagnoser_candidates` and routes
        it through Opus.

        Categories in :data:`TIER_2_FIRST_OCCURRENCE_CATEGORIES`,
        :data:`TIER_2_RECURRENCE_CATEGORIES`, or
        :data:`TIER_3_CATEGORIES` will be diagnosed; others fall back
        to the existing mechanical-retry policy (e.g.
        ``daemon_restart_abandoned`` stays on the infra-preemption
        path).

        Before #3032, ``git_push_failed``, ``pr_create_failed``, and
        ``phase_output_missing`` either wrote no failure row at all
        (pr_create_failed, phase_output_missing) or landed in the
        tier-1 auto-retry set (push categories). Neither route gave
        the Opus diagnoser a chance to differentiate a self-healing
        transient from a deterministic operator-action blocker like
        the PAT-scope cascade on 2026-04-22/23. This method
        standardizes the exit: write the row, mark terminal, let the
        supervisor tick pick it up on the next pass.

        Not used for the per-phase subprocess failures —
        :meth:`_handle_subprocess_failure` already handles the
        classifier, secondary-log-dump event, and tier-1 auto-retry
        semantics for those.
        """
        failure_details: dict[str, Any] = {
            "phase": phase,
            "stderr_tail": stderr_tail,
            "exit_code": int(exit_code) if exit_code is not None else None,
        }
        if details:
            # Caller-supplied fields take precedence but still layer
            # over the standard envelope above so downstream readers
            # always have the canonical three keys.
            failure_details.update(details)

        self._log.warning(
            "daemon.agent_failure_routed",
            extra={
                "event": "agent_failure_routed",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": phase,
                "category": category,
                "exit_code": failure_details.get("exit_code"),
                "issue_number": issue_number,
            },
        )

        self._write_failure(
            agent_id=agent_id,
            category=category,
            detected_by="scheduler",
            details=failure_details,
        )
        self._mark_agent_terminal(
            agent_id,
            status="failed",
            phase=phase,
            exit_code=exit_code,
            issue_number=issue_number,
        )

    def _log_tail(self, worktree: Path, phase: str, max_chars: int = 500) -> str:
        """Return the last ``max_chars`` of the phase log, or an empty string."""
        log_path = worktree / "tmp" / f"claude-p-{phase}.log"
        if not log_path.exists():
            return ""
        try:
            data = log_path.read_text(errors="replace")
        except Exception:  # pragma: no cover
            return ""
        if len(data) <= max_chars:
            return data
        return data[-max_chars:]

    def _extract_log_preview(
        self,
        worktree: Path,
        phase: str,
        max_chars: int = PHASE_STDERR_PREVIEW_MAX_CHARS,
    ) -> str:
        """Return a short preview of the phase log for triage-in-CloudWatch.

        For phase subprocesses the stream is merged stdout+stderr (see
        :meth:`_spawn_phase_subprocess` which redirects stderr to stdout into
        ``{worktree}/tmp/claude-p-<phase>.log``) — we return the last
        ``max_chars`` of the stripped log because failures surface at the
        tail, not the head.

        The full text is preserved on disk at ``claude-p-<phase>.log``,
        at ``dispatcher.phase_outputs.log_text`` (durable across worktree
        cleanup — #2821), and in the secondary
        ``daemon.phase_failure_log`` CloudWatch event — this helper is
        for triage, not archival. Callers should omit the
        ``stderr_preview`` log field when this returns an empty string
        (avoids `stderr_preview: ""` noise on CloudWatch for phases that
        never produced a log, e.g. a preflight exception before spawn).

        Default cap bumped from 200 → 2000 in #2821 — the old tail was
        too tight for noisy failures where the useful line appeared
        early. See :data:`PHASE_STDERR_PREVIEW_MAX_CHARS`. Introduced in
        issue #2809.
        """
        tail = self._log_tail(worktree, phase, max_chars=max_chars)
        return tail.strip()[:max_chars]

    def _read_full_phase_log(self, worktree: Path, phase: str) -> str:
        """Return the full phase log text, or empty string on any error.

        Unlike :meth:`_log_tail` / :meth:`_extract_log_preview`, this
        returns the complete file with no truncation. Used to populate
        ``dispatcher.phase_outputs.log_text`` (durable archival per
        #2821) and as the input to :meth:`_emit_phase_failure_log_event`
        which then applies the :data:`PHASE_FAILURE_LOG_MAX_CHARS` cap.
        """
        log_path = worktree / "tmp" / f"claude-p-{phase}.log"
        if not log_path.exists():
            return ""
        try:
            return log_path.read_text(errors="replace")
        except Exception:  # pragma: no cover — defensive
            return ""

    def _emit_phase_failure_log_event(
        self, agent_id: str, phase: str, worktree: Path
    ) -> None:
        """Emit ``daemon.phase_failure_log`` with up to 10k chars of full log.

        Paired with each ``daemon.subprocess_failed`` event emitted by
        :meth:`_handle_subprocess_failure` (#2821). Skipped when the
        phase log is empty (avoids a blank event for phases that failed
        before any output was produced). Tail-truncated at
        :data:`PHASE_FAILURE_LOG_MAX_CHARS` — failures surface at the
        tail, and merged stdout+stderr logs commonly start with env
        dumps or prompt echoes that are noise.

        The envelope carries both ``log_chars_total`` (full size) and
        ``log_chars_emitted`` (what made it into this event) so
        operators know whether the truncation lost context.
        """
        body = self._read_full_phase_log(worktree, phase)
        if not body:
            return
        total = len(body)
        if total <= PHASE_FAILURE_LOG_MAX_CHARS:
            emitted_body = body
        else:
            emitted_body = body[-PHASE_FAILURE_LOG_MAX_CHARS:]
        self._log.warning(
            "daemon.phase_failure_log",
            extra={
                "event": "phase_failure_log",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": phase,
                "log_chars_total": total,
                "log_chars_emitted": len(emitted_body),
                "log_body": emitted_body,
            },
        )

    def _push_and_open_pr(
        self, agent_id: str, issue_number: int, worktree: Path
    ) -> None:
        """Run the final mechanical steps: amend commit, push, open PR.

        **Commit model (issue #2971).** Ralph's Step 2.5 commits its
        work to a placeholder commit ("WIP: ralph output") and leaves
        it in place on SHIP. This method amends that commit with
        summary's conventional-commits message via ``git commit
        --amend -F <file>``, then pushes. The pre-#2971 model (Ralph
        resets to uncommitted, daemon runs ``git add -A && git commit
        -m <msg>``) was removed because an incomplete reset silently
        swallowed ralph's diff and produced ``git_commit_failed
        exit_code=1 stderr_tail=""`` ("nothing to commit" goes to
        stdout, not stderr).

        On failure at any step, the agent is marked ``failed``. On
        success (criteria all met), ``phase='awaiting_ci'`` so Phase 3B
        knows where to pick up, and ``status`` stays ``running``.

        **needs_review branch (#2856):** when
        :attr:`_agent_unmet_criteria` is non-empty (set by
        :meth:`_run_summary_phase`), the PR is opened as a DRAFT with
        an ``⚠️ Unmet acceptance criteria`` section appended to the
        body, an issue comment is posted linking the draft + listing
        the unmet criteria, and the agent terminates as
        ``status='needs_review', phase='needs_review'`` with
        ``ended_at`` set. The draft PR is NOT picked up by Phase 3B
        (``needs_review`` is outside the ``_list_advanceable_agents``
        SELECT), so auto-merge cannot touch it — the operator reviews,
        marks ready + merges, or closes.
        """
        self._update_agent_phase(agent_id, "push_and_pr")

        # Issue #3039: ralph §2.5d "no-op guardrail" SHIP detection.
        # When ralph's working tree was clean at SHIP time (no code
        # changes needed — e.g. data-only SQL backfill task whose
        # deliverable is ralph's evidence comment on the issue), ralph
        # intentionally did not create a commit. The branch tip is
        # still ``origin/main`` and ``git rev-list --count
        # origin/main..HEAD`` returns 0.
        #
        # Pre-#3039 the daemon unconditionally ran ``git commit
        # --amend`` below, which against the shallow-boundary ``HEAD``
        # produced an orphan root commit ("no history in common with
        # main" from ``gh pr create``). Even with the shallow-clone
        # fix (Fix a), amending ``origin/main`` on a non-shallow clone
        # would rewrite the main-branch commit on the local branch
        # tip — the resulting PR's diff would be empty and
        # confusing, and a push would fail because the branch is
        # not ahead of origin/main.
        #
        # The ralph skill already logs "pre-push gate skipped —
        # working tree clean; no commit created" for this path. The
        # daemon mirrors that with a clean terminal: mark the agent
        # ``status='succeeded' phase=PHASE_NO_OP`` and emit
        # :event:`daemon.push_and_pr_skipped_no_op`. No amend, no
        # push, no PR. Ralph's issue comment is the deliverable.
        if self._is_noop_ship(worktree):
            self._log.info(
                "daemon.push_and_pr_skipped_no_op",
                extra={
                    "event": "push_and_pr_skipped_no_op",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "detail": (
                        "origin/main..HEAD is empty — ralph's §2.5d "
                        "no-op guardrail fired; no commit to push."
                    ),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="succeeded",
                phase=PHASE_NO_OP,
                exit_code=0,
                issue_number=issue_number,
            )
            return

        unmet_criteria = self._agent_unmet_criteria or []
        is_needs_review = bool(unmet_criteria)

        summary_output = self._agent_summary_output or {}
        commit_message = summary_output.get("commit_message") or ""
        pr_title = summary_output.get("pr_title") or ""
        pr_body_md = summary_output.get("pr_body_md") or ""
        if is_needs_review:
            # Append the unmet-criteria section so the operator sees the
            # concerns inline on the draft PR page without opening the
            # issue. Render once and reuse the rendered block for the
            # issue comment (same content, different wrapper) to keep
            # the two views in sync.
            pr_body_md = (
                pr_body_md.rstrip()
                + "\n\n"
                + self._render_unmet_criteria_pr_section(unmet_criteria)
                + "\n"
            )

        if not commit_message or not pr_title or not pr_body_md:
            self._log.warning(
                "daemon.summary_output_incomplete",
                extra={
                    "event": "summary_output_incomplete",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "has_commit": bool(commit_message),
                    "has_pr_title": bool(pr_title),
                    "has_pr_body": bool(pr_body_md),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="push_and_pr",
                exit_code=None,
                issue_number=issue_number,
            )
            return

        # git commit --amend -F <file>. Issue #2971: Ralph's Step 2.5
        # commits its work directly (placeholder message "WIP: ralph
        # output") and leaves the commit in place. We amend that commit
        # with summary's conventional-commits message here, instead of
        # the pre-#2971 ``git add -A && git commit -m <msg>`` pair that
        # assumed ralph produced an uncommitted diff.
        #
        # Why amend instead of stage+commit: the old flow required Step
        # 2.5 to undo its throwaway commit before returning, so the
        # daemon could re-stage and commit fresh. An incomplete undo
        # (observed 2026-04-21 02:59 UTC on agent cc6c5a07) produced a
        # "nothing to commit" failure (``exit_code=1, stderr_tail=""``)
        # because the working tree was already clean — the diff was
        # trapped in the throwaway commit. Amending eliminates the
        # juggling: ralph's commit is always in place, we just rewrite
        # the message.
        #
        # Squash-merge compatibility: GitHub's squash-merge uses the PR
        # title for the merged-main commit subject, so the number of
        # commits on the PR branch is irrelevant to the merged history.
        # Whether this call amends a single ralph commit or ralph
        # landed multiple iteration commits (`--amend --no-edit -a` in
        # Step 2.5's retry loop collapses iterations to one), the
        # result on main is identical.
        commit_msg_path = worktree / "tmp" / "commit_msg.txt"
        commit_msg_path.parent.mkdir(parents=True, exist_ok=True)
        commit_msg_path.write_text(commit_message)

        try:
            commit_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "commit",
                    "--amend",
                    "-F",
                    str(commit_msg_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            self._log.exception(
                "daemon.git_commit_failed",
                extra={
                    "event": "git_commit_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="push_and_pr",
                exit_code=None,
                issue_number=issue_number,
            )
            return
        if commit_result.returncode != 0:
            self._log.warning(
                "daemon.git_commit_failed",
                extra={
                    "event": "git_commit_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": commit_result.returncode,
                    "stderr_tail": _stderr_tail(commit_result.stderr),
                },
            )
            self._mark_agent_terminal(
                agent_id,
                status="failed",
                phase="push_and_pr",
                exit_code=None,
                issue_number=issue_number,
            )
            return

        # Issue #2953: dispatcher-self-PR detection. If the just-made
        # commit touches any file under ``scripts/dispatcher/``, stamp
        # ``verify_skip_reason='self_deploy'`` NOW — before the push —
        # so the downstream verify phase reads the signal and no-ops.
        # Rationale: the daemon that would run verify post-merge is
        # about to be replaced by the deploy of this very PR, and
        # verifying against a soon-dead container produces false-failure
        # noise during the drain window. Inspecting HEAD's file list
        # is a pure read against the local worktree — no network, no
        # DB beyond the single UPDATE.
        try:
            touched_paths = self._list_committed_files_at_head(worktree)
            skip_reason = self._detect_verify_skip_reason(touched_paths)
            if skip_reason:
                self._write_verify_skip_reason(agent_id, skip_reason)
                self._log.info(
                    "daemon.verify_skip_reason_written",
                    extra={
                        "event": "verify_skip_reason_written",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "issue_number": issue_number,
                        "skip_reason": skip_reason,
                        "touched_paths": touched_paths[:20],
                    },
                )
        except Exception:
            # Non-critical path — a failure to detect skip reason
            # just means verify runs normally. The worst case is
            # false-failure noise on the admin page during a self-
            # deploy window, which was the pre-#2953 status quo.
            self._log.exception(
                "daemon.verify_skip_detection_failed",
                extra={
                    "event": "verify_skip_detection_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )

        # git push -u origin <branch>
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        branch = f"agent/{short_id}"
        try:
            push_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "push",
                    "-u",
                    "origin",
                    branch,
                ],
                capture_output=True,
                text=True,
                timeout=GIT_PUSH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            tail = _stderr_tail(getattr(exc, "stderr", None))
            self._log.exception(
                "daemon.git_push_timeout",
                extra={
                    "event": "git_push_timeout",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "timeout_seconds": GIT_PUSH_TIMEOUT_SECONDS,
                    "stderr_tail": tail,
                    "detail": str(exc),
                },
            )
            self._persist_phase_output(
                agent_id,
                phase="push_and_pr",
                output_json={"event": "git_push_timeout", "branch": branch},
                log_text=tail,
            )
            # Issue #3032: route through the unified failure handler so
            # the diagnoser picks this up on the next supervisor tick.
            # ``git_push_network`` is now a TIER_2_FIRST_OCCURRENCE
            # category.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="push_and_pr",
                category=FAILURE_CATEGORY_GIT_PUSH_NETWORK,
                stderr_tail=tail,
                exit_code=None,
                details={"branch": branch, "reason": "timeout"},
                issue_number=issue_number,
            )
            return
        except Exception as exc:
            self._log.exception(
                "daemon.git_push_failed",
                extra={
                    "event": "git_push_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            self._persist_phase_output(
                agent_id,
                phase="push_and_pr",
                output_json={"event": "git_push_failed", "branch": branch},
                log_text=str(exc),
            )
            # Issue #3032: route through the unified failure handler.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="push_and_pr",
                category=FAILURE_CATEGORY_PUSH_FAILED,
                stderr_tail=str(exc),
                exit_code=None,
                details={"branch": branch},
                issue_number=issue_number,
            )
            return
        if push_result.returncode != 0:
            tail = _stderr_tail(push_result.stderr)
            category = _classify_push_failure(tail)
            self._log.warning(
                "daemon.git_push_failed",
                extra={
                    "event": "git_push_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": push_result.returncode,
                    "stderr_tail": tail,
                    "category": category,
                    "branch": branch,
                },
            )
            self._persist_phase_output(
                agent_id,
                phase="push_and_pr",
                output_json={
                    "event": "git_push_failed",
                    "exit_code": push_result.returncode,
                    "branch": branch,
                },
                log_text=(push_result.stderr or ""),
            )
            # Issue #3032: route through the unified failure handler.
            # The classifier's pre_push_hook_rejected / git_push_network
            # / push_failed categories are all tier-2 first-occurrence.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="push_and_pr",
                category=category,
                stderr_tail=tail,
                exit_code=push_result.returncode,
                details={"branch": branch},
                issue_number=issue_number,
            )
            return

        # gh pr create with --body-file pointing to a scratch file in
        # the worktree's tmp/. ``--draft`` is added on the needs_review
        # path (#2856) so auto-merge cannot touch the PR until the
        # operator marks it ready.
        pr_body_path = worktree / "tmp" / "pr_body.md"
        pr_body_path.write_text(pr_body_md)
        pr_create_cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            self._cfg.github_repo,
            "--title",
            pr_title,
            "--body-file",
            str(pr_body_path),
            "--base",
            "main",
            "--head",
            branch,
        ]
        if is_needs_review:
            pr_create_cmd.append("--draft")
        try:
            pr_result = subprocess.run(
                pr_create_cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            self._log.exception(
                "daemon.pr_create_failed",
                extra={
                    "event": "pr_create_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            # Issue #3032: route through the unified failure handler so
            # the Opus diagnoser can tell a duplicate-PR hit from a
            # genuine gh-CLI break from a transient GitHub API wobble.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="push_and_pr",
                category=FAILURE_CATEGORY_PR_CREATE_FAILED,
                stderr_tail=str(exc),
                exit_code=None,
                details={"branch": branch, "reason": "exception"},
                issue_number=issue_number,
            )
            return
        if pr_result.returncode != 0:
            pr_stderr_tail = _stderr_tail(pr_result.stderr)
            self._log.warning(
                "daemon.pr_create_failed",
                extra={
                    "event": "pr_create_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": pr_result.returncode,
                    "stderr_tail": pr_stderr_tail,
                },
            )
            # Issue #3032: route through the unified failure handler.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="push_and_pr",
                category=FAILURE_CATEGORY_PR_CREATE_FAILED,
                stderr_tail=pr_stderr_tail,
                exit_code=pr_result.returncode,
                details={"branch": branch},
                issue_number=issue_number,
            )
            return

        pr_number = self._parse_pr_number(pr_result.stdout or "")
        pr_url = (
            (pr_result.stdout or "").strip().splitlines()[-1]
            if pr_result.stdout
            else ""
        )
        self._log.info(
            "daemon.pr_opened",
            extra={
                "event": "pr_opened",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "is_draft": is_needs_review,
            },
        )

        # ── Cleanup persisted ralph patch (#3012) ──────────────────────────
        # The branch is now on origin and the PR exists — origin/<branch>
        # is the durable source and the postgres copy is redundant.
        # DELETE by agent_id so a racing second claim on the same issue
        # doesn't clobber a newer agent's patch (rows keyed by the
        # specific agent that wrote them). Best-effort — the PR is open,
        # so a delete failure just means the housekeeping sweep will
        # catch it within 7 days.
        self._delete_ralph_patches_for_agent(agent_id)
        # ── end cleanup ────────────────────────────────────────────────────

        if is_needs_review:
            # #2856: side-effect — post an issue comment linking the
            # draft PR + listing the unmet criteria. Runs BEFORE the DB
            # terminal-status update so a DB crash can't lose the
            # operator-visible signal. Failure of the comment does NOT
            # block the terminal transition; the DB update is the
            # authoritative write.
            self._post_needs_review_comment(
                agent_id=agent_id,
                issue_number=issue_number,
                pr_number=pr_number,
                pr_url=pr_url,
                unmet_criteria=unmet_criteria,
                worktree=worktree,
            )
            self._mark_agent_terminal(
                agent_id,
                status="needs_review",
                phase="needs_review",
                exit_code=None,
                pr_number=pr_number,
                issue_number=issue_number,
            )
            return

        # Final state: keep status=running so Phase 3B picks it up.
        self._mark_agent_terminal(
            agent_id,
            status="running",
            phase="awaiting_ci",
            exit_code=None,
            pr_number=pr_number,
        )

    @staticmethod
    def _parse_pr_number(gh_output: str) -> int | None:
        """Extract the PR number from ``gh pr create`` stdout.

        ``gh pr create`` prints the full PR URL on the last
        non-empty line, e.g.
        ``https://github.com/judgemind/judgemind/pull/1234``. Returns
        the integer PR number, or ``None`` if the URL isn't parseable.
        """
        import re  # noqa: PLC0415

        for line in reversed((gh_output or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            match = re.search(r"/pull/(\d+)", line)
            if match:
                return int(match.group(1))
        return None

    # ── Phase 3B post-PR orchestration (supervisor-tick step 3) ─────────
    #
    # 3B advances agents that 3A handed off in ``phase='awaiting_ci'``.
    # Each supervisor tick calls ``_advance_running_agents``, which runs
    # one-shot polls (no blocking ``gh run watch``) and promotes each
    # agent by at most one state-machine step per tick. The 120s tick
    # cadence is the effective poll interval.
    #
    #   awaiting_ci pending      → no-op (re-check next tick)
    #   awaiting_ci all-green    → gh pr merge --squash → awaiting_deploy
    #   awaiting_ci any-failed   → /task-v2-fix-ci subprocess
    #       verdict=PATCHED      → git commit + push → awaiting_ci (retry)
    #                              retries_used++; if > FIX_CI_MAX_RETRIES
    #                              mark status=failed (3C/3D diagnoser)
    #       verdict=BLOCKED      → mark status=failed with block_reason
    #       verdict=FLAKY        → no-op; next tick re-polls (GitHub
    #                              re-runs the flaky job on its own
    #                              cadence or the operator nudges it)
    #   awaiting_deploy pending  → no-op
    #   awaiting_deploy success  → /task-v2-verify subprocess, post
    #                              evidence comment, status=succeeded
    #   awaiting_deploy no-run   → treat as "no deploy applicable",
    #                              proceed to verify with skip-reason
    #   awaiting_deploy failure  → mark status=failed (3C/3D escalates)
    #
    # All subprocess + ``gh`` operations happen in try/except wrappers;
    # unhandled exceptions flip the agent to ``status='crashed'`` and
    # the supervisor tick continues with the next agent.

    def _list_advanceable_agents(self) -> list[dict[str, Any]]:
        """Return agents waiting for the next state-machine step.

        SELECT covers two distinct branches:

        * ``status='running' AND phase IN ('awaiting_ci', 'awaiting_deploy')``
          — Phase 3B's CI watch + deploy watch + verify chain.
        * ``status='succeeded' AND phase IN ('done', 'retro_failed')``
          — Phase 3E's post-success retro + cleanup chain. ``phase='done'``
          rows trigger the retro phase; ``phase='retro_done'`` and
          ``phase='retro_failed'`` rows trigger the worktree cleanup.

        Returns a list of small dicts with the fields the advance methods
        need — ``agent_id``, ``issue_number``, ``phase``, ``status``,
        ``pr_number``, ``worktree_path``, ``retries_used``. An empty
        list is returned on DB error (with a rollback), so the
        supervisor tick can continue without this work.

        Cleanup_done / cleanup_blocked are deliberately excluded — they
        are terminal phases with no further advance to perform.
        """
        assert self._conn is not None, "connect() must run before reading"

        agents: list[dict[str, Any]] = []
        try:
            with self._conn.cursor() as cur:
                # #2927: /task subagents no longer write to
                # ``dispatcher.agents``, so every row here is
                # daemon-owned by construction — no ``kind`` filter
                # needed.
                # Issue #2953: post-merge rows now carry
                # ``status='succeeded'`` from merge-detection onward
                # (instead of only after retro). The filter picks up
                # both pre-merge ``status='running'`` agents (awaiting_ci,
                # awaiting_deploy) AND post-merge ``status='succeeded'``
                # agents that still need verify / retro / cleanup
                # (awaiting_deploy, done, retro_done, retro_failed).
                # The awaiting_deploy phase appears in both branches
                # because the transition merge→awaiting_deploy flips
                # status without changing phase — the next tick sees
                # the succeeded branch.
                cur.execute(
                    "SELECT agent_id, issue_number, phase, pr_number, "
                    "       worktree_path, retries_used, status "
                    "FROM dispatcher.agents "
                    "WHERE (status = 'running' "
                    "       AND phase IN ('awaiting_ci', 'awaiting_deploy')) "
                    "   OR (status = 'succeeded' "
                    "       AND phase IN ('awaiting_deploy', 'done', "
                    "                     'retro_done', 'retro_failed')) "
                    "ORDER BY started_at ASC",
                )
                for row in cur.fetchall():
                    agents.append(
                        {
                            "agent_id": str(row[0]),
                            "issue_number": int(row[1]),
                            "phase": str(row[2]),
                            "pr_number": int(row[3]) if row[3] is not None else None,
                            "worktree_path": str(row[4]),
                            "retries_used": int(row[5]) if row[5] is not None else 0,
                            "status": str(row[6]) if len(row) > 6 else "running",
                        }
                    )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.list_advanceable_failed",
                extra={
                    "event": "list_advanceable_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return []
        return agents

    def _advance_running_agents(self) -> int:
        """Advance each running agent by at most one state-machine step.

        Called from ``supervisor_tick``. One-shot polls only — no
        blocking ``gh run watch``. Per-agent failures are caught and
        logged as ``daemon.advance_failed`` with the agent flipped to
        ``status='crashed'`` so 3C/3D can retry with a fresh worktree.

        Returns the number of agents this tick touched (for logging).
        The actual phase transition that each advance performed is
        logged inline via ``daemon.pr_merged``, ``daemon.fix_ci_*``,
        ``daemon.deploy_*``, and ``daemon.agent_completed`` events.
        """
        assert self._conn is not None, "connect() must run before advancing"

        agents = self._list_advanceable_agents()
        if not agents:
            return 0

        advanced = 0
        for agent in agents:
            agent_id = agent["agent_id"]
            phase = agent["phase"]
            status = agent.get("status", "running")
            try:
                if phase == "awaiting_ci":
                    self._advance_awaiting_ci(agent)
                elif phase == "awaiting_deploy":
                    # Issue #2953: post-merge rows have flipped to
                    # ``status='succeeded'`` at merge time; pre-merge
                    # rows (merge path not yet reached) are still
                    # ``status='running'``. Both need the deploy
                    # handler — status differentiates only the "is the
                    # crash recoverable" policy in the exception
                    # handler below.
                    self._advance_awaiting_deploy(agent)
                elif phase == "done" and status == "succeeded":
                    # Phase 3E (#2798): post-success retro phase.
                    # ``status='succeeded'`` is preserved across this
                    # advance — the retro is bookkeeping for a
                    # successful run, not part of the success itself.
                    self._run_retro_phase(agent)
                elif (
                    phase in (PHASE_RETRO_DONE, PHASE_RETRO_FAILED)
                    and status == "succeeded"
                ):
                    # Phase 3E (#2798): post-retro worktree cleanup.
                    # Fires regardless of whether the retro itself
                    # succeeded or failed — both phases mean "the agent
                    # is done with all the LLM work".
                    self._cleanup_agent_worktree(agent)
                else:  # pragma: no cover — SELECT filter guarantees this
                    continue
                advanced += 1
            except Exception as exc:
                # Unhandled exception in one agent's advance must not
                # stall the daemon. For ``status='running'`` agents,
                # flip to ``status='crashed'`` so 3C picks it up with a
                # fresh worktree. For ``status='succeeded'`` agents
                # (Phase 3E retro/cleanup branches), keep the success
                # status — only log the failure. The agent itself
                # succeeded; an unexpected exception in the post-success
                # bookkeeping should not retroactively flip it to
                # crashed.
                self._log.exception(
                    "daemon.advance_failed",
                    extra={
                        "event": "advance_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "phase": phase,
                        "status": status,
                        "detail": str(exc),
                    },
                )
                if status == "succeeded":
                    # Mark the post-success phase failed without
                    # touching the success status. The agent logically
                    # succeeded at merge time (#2953) — subsequent
                    # deploy-watch / verify / retro / cleanup are
                    # bookkeeping that can leave milestone columns
                    # NULL without retroactively changing the success
                    # status:
                    #   awaiting_deploy  → deploy-watch/verify exception
                    #                      advances straight to
                    #                      PHASE_RETRO_FAILED so retro
                    #                      runs bookkeeping anyway.
                    #                      ``verified_at`` stays NULL
                    #                      — admin shows amber ✓.
                    #   done             → retro exception → retro_failed
                    #                      so cleanup still runs.
                    #   retro_done /
                    #   retro_failed     → cleanup exception →
                    #                      cleanup_blocked.
                    if phase == "awaiting_deploy":
                        self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
                    elif phase == "done":
                        self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
                    elif phase in (PHASE_RETRO_DONE, PHASE_RETRO_FAILED):
                        self._update_agent_phase(agent_id, PHASE_CLEANUP_BLOCKED)
                else:
                    self._mark_agent_terminal(
                        agent_id,
                        status="crashed",
                        phase=phase,
                        exit_code=None,
                    )
        return advanced

    # ── awaiting_ci branch ──────────────────────────────────────────────

    def _advance_awaiting_ci(self, agent: dict[str, Any]) -> None:
        """One supervisor step for an ``awaiting_ci`` agent.

        Polls the PR's combined check rollup via ``gh pr view``. Branches
        on the aggregate state:

        * **Pending** (any check in_progress/queued) — no-op; re-check
          next supervisor tick.
        * **Green** (all SUCCESS/SKIPPED + ``mergeable=MERGEABLE`` +
          ``mergeStateStatus=CLEAN``) — merge with squash, transition
          ``phase='awaiting_deploy'``.
        * **Red** (any FAILURE/CANCELLED/TIMED_OUT/ACTION_REQUIRED) —
          spawn ``/task-v2-fix-ci``. Verdict PATCHED applies the patch
          + pushes + stays in awaiting_ci with ``retries_used++``.
          Verdicts BLOCKED / max-retries-exceeded mark failed.
        """
        agent_id = agent["agent_id"]
        pr_number = agent["pr_number"]

        if pr_number is None:
            # 3A should never produce an ``awaiting_ci`` row without a
            # PR number. If it does, that's a 3A bug — mark failed and
            # log so operators see it.
            self._log.warning(
                "daemon.awaiting_ci_missing_pr",
                extra={
                    "event": "awaiting_ci_missing_pr",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return

        pr_status = self._fetch_pr_status(pr_number)
        if pr_status is None:
            # Transient GitHub error — log and re-check next tick. No
            # terminal transition; the agent keeps its awaiting_ci phase.
            return

        rollup_state = self._classify_check_rollup(pr_status)
        self._log.info(
            "daemon.ci_poll",
            extra={
                "event": "ci_poll",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "pr_number": pr_number,
                "rollup_state": rollup_state,
                "mergeable": pr_status.get("mergeable"),
                "merge_state_status": pr_status.get("mergeStateStatus"),
            },
        )

        if rollup_state == "pending":
            return

        if rollup_state == "green":
            self._merge_pr_and_advance(agent, pr_status)
            return

        # rollup_state == "red"
        self._run_fix_ci(agent, pr_status)

    def _fetch_pr_status(self, pr_number: int) -> dict[str, Any] | None:
        """Fetch the combined check rollup + merge state for a PR.

        Runs ``gh pr view --json statusCheckRollup,mergeable,mergeStateStatus,headRefOid,mergeCommit``
        as a one-shot call (no blocking watch). Returns the parsed JSON
        dict, or ``None`` on subprocess failure (logged as a warning so
        the supervisor can retry next tick).
        """
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self._cfg.github_repo,
            "--json",
            "statusCheckRollup,mergeable,mergeStateStatus,headRefOid,mergeCommit",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            self._log.warning(
                "daemon.gh_missing",
                extra={
                    "event": "gh_missing",
                    "run_id": self._run_id,
                    "pr_number": pr_number,
                },
            )
            return None
        except subprocess.TimeoutExpired:
            self._log.warning(
                "daemon.pr_view_timeout",
                extra={
                    "event": "pr_view_timeout",
                    "run_id": self._run_id,
                    "pr_number": pr_number,
                },
            )
            return None

        if result.returncode != 0:
            self._log.warning(
                "daemon.pr_view_failed",
                extra={
                    "event": "pr_view_failed",
                    "run_id": self._run_id,
                    "pr_number": pr_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return None

        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            self._log.warning(
                "daemon.pr_view_invalid_json",
                extra={
                    "event": "pr_view_invalid_json",
                    "run_id": self._run_id,
                    "pr_number": pr_number,
                },
            )
            return None

    @staticmethod
    def _classify_check_rollup(pr_status: dict[str, Any]) -> str:
        """Return ``'green'``, ``'red'``, or ``'pending'`` for a PR status.

        Logic:

        * Any check in ``IN_PROGRESS`` / ``QUEUED`` / ``PENDING`` /
          ``WAITING`` → ``'pending'`` (even if others failed — we must
          wait for the full signal before deciding).
        * If no pending and any check in ``FAILURE`` / ``CANCELLED`` /
          ``TIMED_OUT`` / ``ACTION_REQUIRED`` → ``'red'``.
        * All ``SUCCESS`` / ``SKIPPED`` / ``NEUTRAL`` / ``STALE`` AND
          ``mergeable='MERGEABLE'`` AND ``mergeStateStatus='CLEAN'`` →
          ``'green'``.
        * Otherwise (mergeable=false, conflicting, etc.) → ``'red'``
          so the fix-ci path runs.

        Accepts both Actions-style (``status``/``conclusion``) and
        legacy-commit-status-style (``state``) rollup entries so it
        works with whatever mix ``gh pr view --json statusCheckRollup``
        returns.
        """
        rollup = pr_status.get("statusCheckRollup") or []

        pending_statuses = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"}
        # Values GitHub uses for a failed Actions check.
        failure_conclusions = {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }
        # Legacy commit-status states.
        legacy_pending = {"PENDING", "EXPECTED"}
        legacy_failure = {"FAILURE", "ERROR"}

        has_pending = False
        has_failure = False

        for check in rollup:
            if not isinstance(check, dict):
                continue
            # Actions-style.
            raw_status = check.get("status")
            raw_conclusion = check.get("conclusion")
            if raw_status is not None:
                status_up = str(raw_status).upper()
                conclusion_up = (
                    str(raw_conclusion).upper() if raw_conclusion is not None else ""
                )
                if status_up == "COMPLETED":
                    if conclusion_up in failure_conclusions:
                        has_failure = True
                elif status_up in pending_statuses:
                    has_pending = True
                continue
            # Legacy commit-status-style.
            raw_state = check.get("state")
            if raw_state is not None:
                state_up = str(raw_state).upper()
                if state_up in legacy_pending:
                    has_pending = True
                elif state_up in legacy_failure:
                    has_failure = True

        if has_pending:
            return "pending"
        if has_failure:
            return "red"

        mergeable = str(pr_status.get("mergeable") or "").upper()
        merge_state = str(pr_status.get("mergeStateStatus") or "").upper()
        if mergeable == "MERGEABLE" and merge_state == "CLEAN":
            return "green"

        # Checks all green but PR not mergeable (conflicts, branch
        # protection unmet, etc.) — treat as red so fix-ci can try.
        return "red"

    def _merge_pr_and_advance(
        self, agent: dict[str, Any], pr_status: dict[str, Any]
    ) -> None:
        """Squash-merge the PR, record the merge SHA, advance to deploy."""
        agent_id = agent["agent_id"]
        pr_number = agent["pr_number"]

        cmd = [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            self._cfg.github_repo,
            "--squash",
            "--delete-branch",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS * 2,
                check=False,
            )
        except FileNotFoundError:
            self._log.warning(
                "daemon.gh_missing",
                extra={
                    "event": "gh_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            return
        except subprocess.TimeoutExpired:
            self._log.warning(
                "daemon.pr_merge_timeout",
                extra={
                    "event": "pr_merge_timeout",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                },
            )
            return

        if result.returncode != 0:
            self._log.warning(
                "daemon.pr_merge_failed",
                extra={
                    "event": "pr_merge_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return

        # Extract the merge commit SHA from the PR status if available;
        # fall back to re-fetching once after the merge.
        merge_sha = self._extract_merge_sha(pr_status)
        if not merge_sha:
            refreshed = self._fetch_pr_status(pr_number)
            if refreshed is not None:
                merge_sha = self._extract_merge_sha(refreshed)

        # Issue #2953: flip ``status='succeeded'`` + stamp ``merged_at``
        # the moment the PR squash-merge lands — not at end of retro.
        # The row now reads "shipped" durably even if the container
        # dies mid-verify / mid-retro. ``phase`` advances to
        # ``awaiting_deploy`` so the scheduler's next tick picks it up
        # and drives the remaining post-merge bookkeeping. Order
        # matters: stamp milestone + flip status FIRST, then advance
        # phase — a crash between the two writes leaves a
        # ``status='succeeded' AND phase='awaiting_ci'`` row (still
        # recoverable by the next tick's advanceable-agents scan
        # because the expanded filter picks up
        # ``status='succeeded' AND phase IN (...)``).
        issue_number = agent.get("issue_number")
        self._write_merged_at(
            agent_id,
            pr_number=pr_number,
            issue_number=int(issue_number) if issue_number is not None else None,
        )
        self._update_agent_phase(agent_id, "awaiting_deploy")
        self._log.info(
            "daemon.pr_merged",
            extra={
                "event": "pr_merged",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "pr_number": pr_number,
                "merge_sha": merge_sha,
            },
        )

    @staticmethod
    def _extract_merge_sha(pr_status: dict[str, Any]) -> str | None:
        """Return the merge commit SHA from a ``gh pr view`` payload.

        ``--json mergeCommit`` returns ``{"mergeCommit": {"oid": "..."}}``
        for merged PRs. Before the merge lands it can be null. We fall
        back to ``headRefOid`` as a last resort since squash-merges
        produce a fresh commit on main, but the head SHA is at least
        a useful correlation key for the deploy-run finder.
        """
        merge_commit = pr_status.get("mergeCommit")
        if isinstance(merge_commit, dict):
            oid = merge_commit.get("oid")
            if isinstance(oid, str) and oid:
                return oid
        head = pr_status.get("headRefOid")
        if isinstance(head, str) and head:
            return head
        return None

    def _run_fix_ci(self, agent: dict[str, Any], pr_status: dict[str, Any]) -> None:
        """Gather failing-job logs, spawn ``/task-v2-fix-ci``, handle verdict.

        Escalates to ``status='failed'`` when ``retries_used >=
        FIX_CI_MAX_RETRIES`` (spec §8 ``ci_red_after_retries``). On
        ``PATCHED`` verdict, applies the patch via ``git add -A`` +
        ``git commit`` + ``git push``, increments ``retries_used``, and
        leaves the agent in ``awaiting_ci`` so the next supervisor
        tick re-polls. On ``BLOCKED`` — mark failed. On ``FLAKY`` —
        no-op; let the next tick re-poll (GitHub may re-run flaky jobs
        automatically, or the operator can nudge).
        """
        agent_id = agent["agent_id"]
        pr_number = agent["pr_number"]
        issue_number = agent["issue_number"]
        worktree = Path(agent["worktree_path"])
        retries_used = agent["retries_used"]

        if retries_used >= FIX_CI_MAX_RETRIES:
            self._log.warning(
                "daemon.fix_ci_max_retries_exceeded",
                extra={
                    "event": "fix_ci_max_retries_exceeded",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "retries_used": retries_used,
                },
            )
            # Phase 3D (#2795): write a tier-3 `ci_red_after_retries`
            # failure row BEFORE flipping the agent to 'failed' so the
            # supervisor tick's tier-3 detector can pick it up and
            # spawn the diagnoser. The failure row carries just enough
            # context for the diagnoser's context-bundle assembly to
            # find the PR + CI log URL.
            self._write_failure(
                agent_id=agent_id,
                category=FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
                detected_by="scheduler",
                details={
                    "phase": "awaiting_ci",
                    "pr_number": pr_number,
                    "issue_number": issue_number,
                    "retries_used": retries_used,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return

        # Build fix-ci input bundle.
        failing_jobs = self._extract_failing_jobs(pr_status)
        git_diff = self._fetch_pr_diff(worktree, pr_number)
        branch = self._branch_for_agent(agent_id)

        fix_ci_input = {
            "agent_id": agent_id,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "branch": branch,
            "failing_jobs": failing_jobs,
            "git_diff_base_to_head": git_diff,
            "worktree_path": str(worktree),
            "repo_root": str(worktree),
            "previous_fix_attempts": retries_used,
        }
        self._write_phase_input(worktree, "fix-ci", fix_ci_input)

        self._log.info(
            "daemon.fix_ci_started",
            extra={
                "event": "fix_ci_started",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "pr_number": pr_number,
                "failing_job_count": len(failing_jobs),
                "retries_used": retries_used,
            },
        )

        exit_code = self._run_subprocess_or_fail(agent_id, "fix-ci", worktree)
        if exit_code is None:
            # Subprocess infra failure — agent already marked failed
            # inside _run_subprocess_or_fail. Stop.
            return

        fix_ci_output = self._read_phase_output(worktree, "fix-ci")
        if fix_ci_output is None:
            extra: dict[str, Any] = {
                "event": "phase_output_missing",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "fix-ci",
            }
            preview = self._extract_log_preview(worktree, "fix-ci")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.phase_output_missing", extra=extra)
            # Issue #3032: route through the unified failure handler.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="awaiting_ci",
                category=FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
                stderr_tail=preview or "",
                exit_code=exit_code,
                details={"missing_phase_output": "fix-ci"},
            )
            return

        self._persist_phase_output(
            agent_id,
            "fix-ci",
            fix_ci_output,
            log_text=self._read_full_phase_log(worktree, "fix-ci") or None,
            usage=self._parse_phase_usage(worktree, "fix-ci"),
        )
        verdict = str(fix_ci_output.get("verdict") or "").upper()

        if verdict == "PATCHED":
            self._apply_fix_ci_patch(agent, fix_ci_output)
            return
        if verdict == "FLAKY":
            # No code change. Next tick will re-poll; GitHub's flaky
            # re-run path or a manual nudge resolves eventually.
            self._log.info(
                "daemon.fix_ci_flaky",
                extra={
                    "event": "fix_ci_flaky",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "flaky_evidence": fix_ci_output.get("flaky_evidence"),
                },
            )
            return
        # verdict == "BLOCKED" or unrecognized
        self._log.warning(
            "daemon.fix_ci_blocked",
            extra={
                "event": "fix_ci_blocked",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "pr_number": pr_number,
                "verdict": verdict,
                "block_reason": fix_ci_output.get("block_reason"),
            },
        )
        self._mark_agent_terminal(
            agent_id, status="failed", phase="awaiting_ci", exit_code=exit_code
        )

    @staticmethod
    def _extract_failing_jobs(pr_status: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the failing check-run entries out of the PR rollup.

        Returns a list of ``{name, conclusion, databaseId, detailsUrl}``
        dicts — the daemon fills ``log_tail`` later via
        ``_fetch_job_log_tail``. Capped at ``FIX_CI_MAX_FAILING_JOBS``
        to bound the payload size handed to fix-ci.
        """
        failure_conclusions = {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }
        rollup = pr_status.get("statusCheckRollup") or []
        failing: list[dict[str, Any]] = []
        for check in rollup:
            if not isinstance(check, dict):
                continue
            conclusion = str(check.get("conclusion") or "").upper()
            status = str(check.get("status") or "").upper()
            if status == "COMPLETED" and conclusion in failure_conclusions:
                failing.append(
                    {
                        "name": check.get("name") or check.get("context") or "",
                        "conclusion": conclusion,
                        "databaseId": check.get("databaseId"),
                        "detailsUrl": check.get("detailsUrl"),
                    }
                )
            if len(failing) >= FIX_CI_MAX_FAILING_JOBS:
                break
        return failing

    def _fetch_pr_diff(self, worktree: Path, pr_number: int) -> str:
        """Return the PR's full base-to-head diff, empty string on error."""
        cmd = [
            "gh",
            "pr",
            "diff",
            str(pr_number),
            "--repo",
            self._cfg.github_repo,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout or ""

    def _branch_for_agent(self, agent_id: str) -> str:
        """Return the agent's branch name derived from agent_id."""
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        return f"agent/{short_id}"

    def _apply_fix_ci_patch(
        self, agent: dict[str, Any], fix_ci_output: dict[str, Any]
    ) -> None:
        """Stage + commit + push the fix-ci patch; stay in awaiting_ci.

        The fix-ci skill left the patch in the worktree's working tree;
        ``changed_files`` is the list of files it touched. We run
        ``git add -A`` (the skill may have created new files too),
        ``git commit -F <msg-file>``, and ``git push``. On success,
        increment ``retries_used`` and leave the agent in
        ``awaiting_ci`` so the next supervisor tick re-polls.
        """
        agent_id = agent["agent_id"]
        worktree = Path(agent["worktree_path"])
        branch = self._branch_for_agent(agent_id)
        retries_used = agent["retries_used"]

        commit_message = str(fix_ci_output.get("commit_message") or "").strip()
        if not commit_message:
            self._log.warning(
                "daemon.fix_ci_missing_commit_message",
                extra={
                    "event": "fix_ci_missing_commit_message",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return

        # git add -A
        try:
            add_result = subprocess.run(
                ["git", "-C", str(worktree), "add", "-A"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception:
            self._log.exception(
                "daemon.fix_ci_git_add_failed",
                extra={
                    "event": "fix_ci_git_add_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return
        if add_result.returncode != 0:
            self._log.warning(
                "daemon.fix_ci_git_add_failed",
                extra={
                    "event": "fix_ci_git_add_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": add_result.returncode,
                    "stderr_tail": _stderr_tail(add_result.stderr),
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return

        # git commit -F <file>
        commit_msg_path = worktree / "tmp" / "commit_msg_fix_ci.txt"
        commit_msg_path.parent.mkdir(parents=True, exist_ok=True)
        commit_msg_path.write_text(commit_message)

        try:
            commit_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "commit",
                    "-F",
                    str(commit_msg_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception:
            self._log.exception(
                "daemon.fix_ci_git_commit_failed",
                extra={
                    "event": "fix_ci_git_commit_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return
        if commit_result.returncode != 0:
            # ``git commit`` exits non-zero if nothing is staged —
            # that means the skill reported PATCHED but wrote no diff.
            # Treat as block.
            self._log.warning(
                "daemon.fix_ci_git_commit_failed",
                extra={
                    "event": "fix_ci_git_commit_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": commit_result.returncode,
                    "stderr_tail": _stderr_tail(commit_result.stderr),
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return

        # git push
        try:
            push_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "push",
                    "origin",
                    branch,
                ],
                capture_output=True,
                text=True,
                timeout=GIT_PUSH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._log.exception(
                "daemon.fix_ci_git_push_timeout",
                extra={
                    "event": "fix_ci_git_push_timeout",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "timeout_seconds": GIT_PUSH_TIMEOUT_SECONDS,
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return
        except Exception:
            self._log.exception(
                "daemon.fix_ci_git_push_failed",
                extra={
                    "event": "fix_ci_git_push_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return
        if push_result.returncode != 0:
            self._log.warning(
                "daemon.fix_ci_git_push_failed",
                extra={
                    "event": "fix_ci_git_push_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": push_result.returncode,
                    "stderr_tail": _stderr_tail(push_result.stderr),
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=None
            )
            return

        # Success — bump retries_used, leave awaiting_ci for next tick.
        self._increment_retries_used(agent_id)
        self._log.info(
            "daemon.fix_ci_patched",
            extra={
                "event": "fix_ci_patched",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "new_retries_used": retries_used + 1,
                "commit_message": commit_message,
            },
        )

    def _increment_retries_used(self, agent_id: str) -> None:
        """UPDATE ``dispatcher.agents.retries_used = retries_used + 1``."""
        assert self._conn is not None, "connect() must run before update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET retries_used = retries_used + 1 "
                    "WHERE agent_id = %s",
                    (agent_id,),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.increment_retries_failed",
                extra={
                    "event": "increment_retries_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

    # ── awaiting_deploy branch ──────────────────────────────────────────

    def _advance_awaiting_deploy(self, agent: dict[str, Any]) -> None:
        """One supervisor step for an ``awaiting_deploy`` agent.

        Finds the deploy runs triggered by the merge commit, polls
        their conclusions, and advances to verify on success / treats
        doc-only PRs as "no deploy applicable".
        """
        agent_id = agent["agent_id"]
        pr_number = agent["pr_number"]

        if pr_number is None:  # pragma: no cover — 3A always sets pr_number
            self._log.warning(
                "daemon.awaiting_deploy_missing_pr",
                extra={
                    "event": "awaiting_deploy_missing_pr",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_deploy", exit_code=None
            )
            return

        pr_status = self._fetch_pr_status(pr_number)
        if pr_status is None:
            return  # transient — retry next tick
        merge_sha = self._extract_merge_sha(pr_status)
        if not merge_sha:
            self._log.warning(
                "daemon.awaiting_deploy_no_merge_sha",
                extra={
                    "event": "awaiting_deploy_no_merge_sha",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                },
            )
            return  # next tick might see it

        deploy_runs = self._find_deploy_runs(merge_sha)
        # ``deploy_runs=[]`` means no matching deploy workflow fired on
        # that SHA. Treat as "no deploy applicable" and proceed to verify.
        deploy_state = self._classify_deploy_runs(deploy_runs)
        self._log.info(
            "daemon.deploy_poll",
            extra={
                "event": "deploy_poll",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "pr_number": pr_number,
                "merge_sha": merge_sha,
                "deploy_state": deploy_state,
                "deploy_run_count": len(deploy_runs),
            },
        )
        if deploy_state == "pending":
            return
        if deploy_state == "failure":
            self._log.warning(
                "daemon.deploy_failed",
                extra={
                    "event": "deploy_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "merge_sha": merge_sha,
                    "deploy_runs": deploy_runs,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_deploy", exit_code=None
            )
            return

        # deploy_state in ("success", "none") — run verify.
        self._run_verify_and_complete(agent, pr_status, merge_sha, deploy_runs)

    def _find_deploy_runs(self, merge_sha: str) -> list[dict[str, Any]]:
        """Return deploy-workflow runs whose ``headSha`` equals ``merge_sha``.

        Runs ``gh run list --commit <sha> --json
        databaseId,workflowName,status,conclusion,createdAt`` and filters
        by ``workflowName in DEPLOY_WORKFLOW_NAMES``. Returns an empty
        list if no deploy workflow fired on that SHA (doc-only PRs)
        or on subprocess failure.
        """
        cmd = [
            "gh",
            "run",
            "list",
            "--repo",
            self._cfg.github_repo,
            "--commit",
            merge_sha,
            "--json",
            "databaseId,workflowName,status,conclusion,createdAt",
            "--limit",
            "20",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            self._log.warning(
                "daemon.run_list_failed",
                extra={
                    "event": "run_list_failed",
                    "run_id": self._run_id,
                    "merge_sha": merge_sha,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return []
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        runs: list[dict[str, Any]] = []
        for run in payload:
            if not isinstance(run, dict):
                continue
            if run.get("workflowName") in DEPLOY_WORKFLOW_NAMES:
                runs.append(run)
        return runs

    @staticmethod
    def _classify_deploy_runs(runs: list[dict[str, Any]]) -> str:
        """Return ``'pending'``, ``'success'``, ``'failure'``, or ``'none'``.

        * ``none`` — no deploy run matched (doc-only PR etc.).
        * ``pending`` — any run in a non-terminal state.
        * ``failure`` — at least one run terminal with failure-type
          conclusion.
        * ``success`` — all runs terminal with success/skipped/neutral.
        """
        if not runs:
            return "none"
        success_conclusions = {"SUCCESS", "SKIPPED", "NEUTRAL"}
        has_pending = False
        has_failure = False
        for run in runs:
            status = str(run.get("status") or "").upper()
            if status != "COMPLETED":
                # Any non-terminal status means the deploy is still
                # running / queued / waiting. Classify as pending and
                # re-poll next tick.
                has_pending = True
                continue
            conclusion = str(run.get("conclusion") or "").upper()
            if conclusion in success_conclusions:
                continue
            has_failure = True
        if has_pending:
            return "pending"
        if has_failure:
            return "failure"
        return "success"

    # ── verify (final) ──────────────────────────────────────────────────

    def _run_verify_and_complete(
        self,
        agent: dict[str, Any],
        pr_status: dict[str, Any],
        merge_sha: str,
        deploy_runs: list[dict[str, Any]],
    ) -> None:
        """Spawn ``/task-v2-verify``, post evidence comment, advance to done.

        Issue #2953: ``status='succeeded'`` was already written at merge
        time by ``_write_merged_at`` — this method no longer flips the
        status. Its remaining responsibilities are (a) short-circuit
        when ``verify_skip_reason`` is set (dispatcher-self-PR case),
        (b) run the verify subprocess, (c) stamp ``verified_at`` on
        VERIFIED/SKIPPED verdict, and (d) advance ``phase`` to ``done``
        so the retro phase picks it up on the next tick.

        A FAILED verdict flips status back to ``failed`` via
        ``_mark_agent_terminal`` — verify failing post-merge is a real
        problem (the deployed code didn't behave as expected) and the
        admin cockpit should surface it in red even though the PR
        technically shipped. The row still has ``merged_at`` populated
        so the milestone breakdown tooltip can show "merged X · verify
        failed" on operator hover.
        """
        agent_id = agent["agent_id"]
        issue_number = agent["issue_number"]
        pr_number = agent["pr_number"]
        worktree = Path(agent["worktree_path"])

        # Issue #2953: short-circuit when this PR was detected as a
        # dispatcher-self-PR during push_and_pr. The daemon process
        # running verify here is about to be replaced by its own
        # deploy, so verifying against a soon-dead container adds
        # noise (false-failure race during the drain window) without
        # validating anything. Advance straight to ``done`` — the
        # retro phase still runs (bookkeeping fits in the drain
        # window) and the admin row renders as shipped-without-verify
        # (green ✓) per the skip-reason semantic.
        skip_reason = self._read_verify_skip_reason(agent_id)
        if skip_reason:
            self._log.info(
                "daemon.verify_skipped",
                extra={
                    "event": "verify_skipped",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "skip_reason": skip_reason,
                },
            )
            # Advance phase so retro picks it up. Do NOT stamp
            # ``verified_at`` — the skip reason is the canonical
            # signal; a stamped timestamp would conflate "verify ran
            # and passed" with "verify intentionally did not run".
            self._update_agent_phase(agent_id, "done")
            return

        # Fetch the issue bundle for acceptance criteria. Best-effort —
        # the verify skill tolerates an empty AC list with a failure row.
        try:
            bundle = self._fetch_issue_bundle(issue_number)
        except RuntimeError:
            bundle = {
                "issue_number": issue_number,
                "issue_title": "",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }

        acceptance_criteria = self._extract_acceptance_criteria(
            bundle.get("issue_body") or ""
        )

        # Pick a representative deploy run for the input bundle. Prefer
        # the first non-SKIPPED success; fall back to the first entry.
        deploy_status = self._select_deploy_status(deploy_runs)
        change_type = self._infer_change_type(deploy_runs)

        # Issue #3010 — fetch summary's persisted output so we can thread
        # ``deferred_acs`` through to the verify skill. The verify skill
        # runs the deferred ACs first (summary skipped them pre-merge)
        # and labels each as "deferred (marker|heuristic) → pass|fail"
        # in the evidence comment. Absent/empty on pre-#3010 agents and
        # on no-deploy types — verify treats the universe as "every AC,
        # no labeling" in that case (see task-v2-verify SKILL.md).
        summary_persisted = self._fetch_phase_output(agent_id, "summary") or {}
        deferred_acs_raw = summary_persisted.get("deferred_acs") or []
        deferred_acs: list[dict[str, Any]] = []
        if isinstance(deferred_acs_raw, list):
            for entry in deferred_acs_raw:
                if not isinstance(entry, dict):
                    continue
                idx = entry.get("index")
                try:
                    idx_int = int(idx) if idx is not None else None
                except (TypeError, ValueError):
                    idx_int = None
                reason = str(entry.get("reason") or "")
                verify_instruction = str(entry.get("verify_instruction") or "")
                deferred_acs.append(
                    {
                        "index": idx_int,
                        "reason": reason,
                        "verify_instruction": verify_instruction,
                    }
                )

        verify_input = {
            "agent_id": agent_id,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "acceptance_criteria": acceptance_criteria,
            "change_type": change_type,
            "touched_services": self._touched_services_from_runs(deploy_runs),
            "deploy_status": deploy_status,
            "merged_commit_sha": merge_sha,
            "worktree_path": str(worktree),
            "repo_root": str(worktree),
            "deferred_acs": deferred_acs,
        }
        self._write_phase_input(worktree, "verify", verify_input)

        self._log.info(
            "daemon.verify_started",
            extra={
                "event": "verify_started",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "pr_number": pr_number,
                "change_type": change_type,
                "deploy_applicable": deploy_status is not None,
            },
        )

        exit_code = self._run_subprocess_or_fail(agent_id, "verify", worktree)
        if exit_code is None:
            return  # subprocess failure already marked failed

        verify_output = self._read_phase_output(worktree, "verify")
        if verify_output is None:
            extra: dict[str, Any] = {
                "event": "phase_output_missing",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": "verify",
            }
            preview = self._extract_log_preview(worktree, "verify")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.phase_output_missing", extra=extra)
            # Issue #3032: route through the unified failure handler.
            self._handle_agent_failure(
                agent_id=agent_id,
                phase="awaiting_deploy",
                category=FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
                stderr_tail=preview or "",
                exit_code=exit_code,
                details={"missing_phase_output": "verify"},
            )
            return

        self._persist_phase_output(
            agent_id,
            "verify",
            verify_output,
            log_text=self._read_full_phase_log(worktree, "verify") or None,
            usage=self._parse_phase_usage(worktree, "verify"),
        )

        evidence_md = str(verify_output.get("evidence_md") or "").strip()
        if evidence_md:
            self._post_evidence_comment(agent_id, issue_number, worktree, evidence_md)
        else:
            self._log.warning(
                "daemon.verify_missing_evidence_md",
                extra={
                    "event": "verify_missing_evidence_md",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                },
            )

        verdict = str(verify_output.get("verdict") or "").upper()
        if verdict == "FAILED":
            self._log.warning(
                "daemon.verify_failed",
                extra={
                    "event": "verify_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "failure_reason": verify_output.get("failure_reason"),
                },
            )
            # Issue #2953: verify failed post-merge is a genuine
            # regression signal — the deployed code didn't behave as
            # expected. Flip status back to ``failed`` so the admin
            # cockpit renders red. ``merged_at`` stays populated so the
            # tooltip can read "merged X · verify failed" instead of
            # hiding the shipment entirely.
            self._mark_agent_terminal(
                agent_id, status="failed", phase="done", exit_code=exit_code
            )
            return

        # VERIFIED or SKIPPED — stamp ``verified_at`` and advance phase
        # so the retro phase picks up the row next tick. ``status`` is
        # already ``succeeded`` from merge-time; no re-flip needed
        # (issue #2953).
        self._write_verified_at(agent_id)
        self._update_agent_phase(agent_id, "done")
        self._log.info(
            "daemon.agent_completed",
            extra={
                "event": "agent_completed",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "merge_sha": merge_sha,
                "verdict": verdict,
            },
        )

    @staticmethod
    def _extract_acceptance_criteria(body: str) -> list[str]:
        """Pull ``- [ ] …`` checkboxes out of the issue body.

        Matches any task-list style checkbox (checked or unchecked), so
        a partially-checked AC still ends up in the list. Returns the
        text after the checkbox, stripped. Skips the verification
        checkboxes that live under ``### Post-deploy verification`` or
        under an ``### Automated checks`` heading, since those are
        workflow chrome, not issue-level ACs.
        """
        import re  # noqa: PLC0415

        lines = body.splitlines()
        skip_sections = {"post-deploy verification", "automated checks", "test plan"}
        in_skip_section = False
        criteria: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                # Headings reset the skip-section state.
                heading_text = stripped.lstrip("#").strip().lower()
                in_skip_section = any(tag in heading_text for tag in skip_sections)
                continue
            if in_skip_section:
                continue
            match = re.match(r"^\s*-\s*\[[ xX]\]\s*(.+)$", line)
            if match:
                criteria.append(match.group(1).strip())
        return criteria

    @staticmethod
    def _select_deploy_status(
        deploy_runs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Pick one deploy run to describe in the verify input bundle.

        Returns ``None`` when no deploy fired (doc-only PR). Otherwise
        returns ``{workflow_name, run_id, conclusion, duration_s}`` from
        the first entry (deploy workflows are usually single-run per
        commit; multiple runs get picked up pairwise for logging, but
        verify only needs one for its ``deploy_status`` field).
        """
        if not deploy_runs:
            return None
        first = deploy_runs[0]
        return {
            "workflow_name": first.get("workflowName"),
            "run_id": first.get("databaseId"),
            "conclusion": first.get("conclusion"),
            "duration_s": None,
        }

    @staticmethod
    def _infer_change_type(deploy_runs: list[dict[str, Any]]) -> str:
        """Map the first matching deploy workflow to a ``change_type`` tag.

        The verify skill's ``change_type`` field drives its
        verification path. If no deploy fired, return
        ``no_deployed_component`` so the skill emits a SKIPPED verdict.
        """
        if not deploy_runs:
            return "no_deployed_component"
        # Most-specific match wins.
        name_to_type = {
            "Deploy API": "api",
            "Deploy Dispatcher": "dx_tooling",
            "Deploy Scraper": "scraper",
            "Deploy Production": "web",
            "Deploy Production (Web)": "web",
            "Terraform": "dx_tooling",
        }
        for run in deploy_runs:
            mapped = name_to_type.get(str(run.get("workflowName") or ""))
            if mapped:
                return mapped
        return "dx_tooling"

    @staticmethod
    def _touched_services_from_runs(
        deploy_runs: list[dict[str, Any]],
    ) -> list[str]:
        """Return the ECS service names the deploy workflows affected."""
        name_to_service = {
            "Deploy API": "judgemind-api-dev",
            "Deploy Dispatcher": "judgemind-dispatcher-dev",
            "Deploy Scraper": "judgemind-scraper-dev",
        }
        services: list[str] = []
        for run in deploy_runs:
            svc = name_to_service.get(str(run.get("workflowName") or ""))
            if svc and svc not in services:
                services.append(svc)
        return services

    def _post_evidence_comment(
        self,
        agent_id: str,
        issue_number: int,
        worktree: Path,
        evidence_md: str,
    ) -> None:
        """Post the verify skill's ``evidence_md`` as an issue comment.

        Uses ``gh issue comment --body-file`` to avoid shell-quoting the
        markdown body. Writes the file into the worktree's ``tmp/`` so
        every subprocess sees the same filesystem layout.
        """
        evidence_path = worktree / "tmp" / "verification_evidence.md"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(evidence_md)

        cmd = [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--body-file",
            str(evidence_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._log.warning(
                "daemon.evidence_comment_failed",
                extra={
                    "event": "evidence_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "reason": "subprocess",
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.evidence_comment_failed",
                extra={
                    "event": "evidence_comment_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return
        self._log.info(
            "daemon.evidence_comment_posted",
            extra={
                "event": "evidence_comment_posted",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "evidence_chars": len(evidence_md),
            },
        )

    # ── Phase 3E retro orchestration + worktree cleanup (issue #2798) ──
    #
    # After an agent reaches ``status='succeeded' AND phase='done'``
    # (the verify-success path in Phase 3B), the supervisor tick drives
    # two more state-machine steps:
    #
    #   1. Retro phase. ``_run_retro_phase`` builds the input bundle
    #      for ``/task-v2-retro``, spawns the subprocess, reads the
    #      output, and files each retro issue via ``gh issue create``.
    #      Transitions to ``phase='retro_done'`` on success or
    #      ``phase='retro_failed'`` on subprocess failure.
    #   2. Worktree cleanup. ``_cleanup_agent_worktree`` runs
    #      ``scripts/cleanup_worktree.sh``. Transitions to
    #      ``phase='cleanup_done'`` on success or
    #      ``phase='cleanup_blocked'`` on safety-check refusal.
    #      Never bypasses with ``--force`` (CLAUDE.md §Critical Rules).
    #
    # Both branches preserve ``status='succeeded'`` — the agent itself
    # succeeded; only the post-success bookkeeping varies.

    def _run_retro_phase(self, agent: dict[str, Any]) -> None:
        """Spawn ``/task-v2-retro`` and file any retro issues it produces.

        Reads ``dispatcher.phase_transitions`` + ``dispatcher.failures``
        for this agent, writes the retro input bundle, runs the
        subprocess, parses the output, and calls ``gh issue create``
        once per entry in ``retro_issues``. Transitions the agent's
        phase to :data:`PHASE_RETRO_DONE` on success or
        :data:`PHASE_RETRO_FAILED` on subprocess / parse failure.

        ``status='succeeded'`` is preserved across this advance — the
        retro is bookkeeping for an already-successful run.
        """
        agent_id = agent["agent_id"]
        issue_number = agent["issue_number"]
        pr_number = agent.get("pr_number")
        worktree = Path(agent["worktree_path"])

        # Best-effort defense: if the worktree directory is gone (e.g.
        # an operator manually removed it before retro ran), skip
        # straight to cleanup_done — there is no LLM to spawn against.
        if not worktree.exists():
            self._log.warning(
                "daemon.retro_worktree_missing",
                extra={
                    "event": "retro_worktree_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "worktree_path": str(worktree),
                },
            )
            self._update_agent_phase(agent_id, PHASE_CLEANUP_DONE)
            return

        # Build the retro input bundle. The retro skill's SKILL.md
        # documents the contract — we satisfy the required fields and
        # the optional ones we can derive cheaply from DB state.
        retro_input = self._build_retro_input(agent)
        self._write_phase_input(worktree, "retro", retro_input)

        self._log.info(
            "daemon.retro_started",
            extra={
                "event": "retro_started",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "ralph_iterations": retro_input.get("ralph_iterations"),
                "ci_attempts": retro_input.get("ci_attempts"),
            },
        )

        # Spawn the retro subprocess. Failure here flips to retro_failed
        # but does NOT touch the agent's succeeded status.
        try:
            exit_code, duration_s = self._spawn_phase_subprocess(
                "retro", worktree, agent_id
            )
        except subprocess.TimeoutExpired:
            extra: dict[str, Any] = {
                "event": "retro_timeout",
                "run_id": self._run_id,
                "agent_id": agent_id,
            }
            preview = self._extract_log_preview(worktree, "retro")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.retro_timeout", extra=extra)
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return
        except (FileNotFoundError, OSError) as exc:
            extra = {
                "event": "retro_subprocess_error",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "detail": str(exc),
            }
            preview = self._extract_log_preview(worktree, "retro")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.retro_subprocess_error", extra=extra)
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return

        if exit_code != 0:
            extra = {
                "event": "retro_nonzero_exit",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "exit_code": exit_code,
                "duration_s": duration_s,
            }
            preview = self._extract_log_preview(worktree, "retro")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.retro_nonzero_exit", extra=extra)
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return

        retro_output = self._read_phase_output(worktree, "retro")
        if retro_output is None:
            extra = {
                "event": "retro_output_missing",
                "run_id": self._run_id,
                "agent_id": agent_id,
            }
            preview = self._extract_log_preview(worktree, "retro")
            if preview:
                extra["stderr_preview"] = preview
            self._log.warning("daemon.retro_output_missing", extra=extra)
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return

        # Persist the retro output to dispatcher.phase_outputs +
        # phase_transitions so the admin page sees the run.
        self._persist_phase_output(
            agent_id,
            "retro",
            retro_output,
            log_text=self._read_full_phase_log(worktree, "retro") or None,
            usage=self._parse_phase_usage(worktree, "retro"),
        )

        # File the retro issues. Each entry becomes a separate
        # ``gh issue create``. Honour the per-agent cap defensively —
        # the skill is supposed to produce only high-signal findings.
        retro_issues = retro_output.get("retro_issues") or []
        if not isinstance(retro_issues, list):
            retro_issues = []
        if len(retro_issues) > MAX_RETRO_ISSUES_PER_AGENT:
            self._log.warning(
                "daemon.retro_issue_cap_truncate",
                extra={
                    "event": "retro_issue_cap_truncate",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_count": len(retro_issues),
                    "cap": MAX_RETRO_ISSUES_PER_AGENT,
                },
            )
            retro_issues = retro_issues[:MAX_RETRO_ISSUES_PER_AGENT]

        filed = 0
        for entry in retro_issues:
            if not isinstance(entry, dict):
                continue
            new_issue = self._file_retro_issue(agent_id, worktree, entry)
            if new_issue is not None:
                filed += 1

        # Issue #2953: stamp ``retroed_at`` BEFORE advancing phase so a
        # crash between the two writes leaves the milestone column set
        # — the admin cockpit can render the "retro completed" signal
        # even if the phase advance failed. Paired reads of
        # ``phase=retro_done`` and ``retroed_at IS NOT NULL`` are both
        # authoritative (post this fix).
        self._write_retroed_at(agent_id)
        self._update_agent_phase(agent_id, PHASE_RETRO_DONE)
        self._log.info(
            "daemon.retro_done",
            extra={
                "event": "retro_done",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "no_findings": bool(retro_output.get("no_findings")),
                "issues_filed": filed,
                "issues_attempted": len(retro_issues),
                "duration_s": duration_s,
            },
        )

    def _build_retro_input(self, agent: dict[str, Any]) -> dict[str, Any]:
        """Assemble the input bundle the retro skill reads.

        Pulls ``phase_transitions`` + ``failures`` for this agent from
        the DB, plus a few summary counters derived from those rows.
        Optional fields like ``scope_check_followups`` /
        ``plan_follow_ups`` are populated when the corresponding
        ``dispatcher.phase_outputs`` row exists; missing data is fine
        (the retro skill tolerates missing optional fields).
        """
        agent_id = agent["agent_id"]
        worktree_path = str(agent["worktree_path"])

        phase_transitions = self._fetch_phase_transitions(agent_id)
        failures = self._fetch_failures_grouped(agent_id)

        # Derive counters from the persisted phase_transitions log.
        ralph_iterations = sum(
            1 for p in phase_transitions if p.get("phase") == "ralph"
        )
        ci_attempts = sum(
            1 for p in phase_transitions if p.get("phase") == "awaiting_ci"
        )
        fix_ci_attempts = sum(
            1 for p in phase_transitions if p.get("phase") == "fix_ci"
        )
        # Floor of 1 for ralph_iterations + ci_attempts so a clean
        # single-pass run still satisfies the retro skill's
        # short-circuit (== 1) check. A successful agent ran ralph
        # at least once and CI at least once even if the daemon's
        # phase log only captured a single transition row.
        if ralph_iterations < 1:
            ralph_iterations = 1
        if ci_attempts < 1:
            ci_attempts = 1

        # Wall-clock duration from started_at to now (the retro phase
        # itself counts as the verify-completed marker — close enough
        # for the weekly report).
        total_duration_s = self._fetch_agent_total_duration_s(agent_id)

        # Best-effort scope-check + plan-follow-ups extraction from the
        # persisted plan output, if present.
        plan_output = self._fetch_phase_output(agent_id, "plan")
        scope_check_followups = []
        plan_follow_ups = []
        if isinstance(plan_output, dict):
            scope_raw = plan_output.get("scope_check_followups") or []
            if isinstance(scope_raw, list):
                scope_check_followups = [str(x) for x in scope_raw if x]
            follow_raw = (
                plan_output.get("follow_ups")
                or plan_output.get("plan_follow_ups")
                or []
            )
            if isinstance(follow_raw, list):
                plan_follow_ups = [str(x) for x in follow_raw if x]

        # Verify evidence — derived from the verify phase output if
        # persisted. The retro skill uses this to summarize the run
        # for the weekly report.
        verify_output = self._fetch_phase_output(agent_id, "verify")
        verify_evidence_md = ""
        if isinstance(verify_output, dict):
            verify_evidence_md = str(verify_output.get("evidence_md") or "")

        # diff_stats is left empty — it's optional in the retro
        # contract and would require shelling out to git just for the
        # input bundle. The retro skill tolerates missing fields.
        diff_stats: dict[str, int] = {
            "files_changed": 0,
            "insertions": 0,
            "deletions": 0,
        }

        return {
            "agent_id": agent_id,
            "issue_number": agent["issue_number"],
            "pr_number": agent.get("pr_number"),
            "phase_transitions": phase_transitions,
            "failures": failures,
            "ralph_iterations": ralph_iterations,
            "ci_attempts": ci_attempts,
            "fix_ci_attempts": fix_ci_attempts,
            "total_duration_s": total_duration_s,
            "diff_stats": diff_stats,
            "worktree_path": worktree_path,
            "repo_root": worktree_path,
            "scope_check_followups": scope_check_followups,
            "plan_follow_ups": plan_follow_ups,
            "verify_evidence_md": verify_evidence_md,
        }

    def _fetch_phase_transitions(self, agent_id: str) -> list[dict[str, Any]]:
        """SELECT ``dispatcher.phase_transitions`` rows for this agent.

        Returns oldest-first list of ``{phase, ts}`` dicts. ``ts`` is
        ISO-8601 string. Empty list on DB error (logged + rolled back).
        """
        assert self._conn is not None, "connect() must run before reading"
        rows: list[dict[str, Any]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT phase, ts FROM dispatcher.phase_transitions "
                    "WHERE agent_id = %s ORDER BY ts ASC",
                    (agent_id,),
                )
                for row in cur.fetchall():
                    rows.append(
                        {
                            "phase": str(row[0]) if row[0] is not None else "",
                            "ts": (
                                row[1].isoformat()
                                if hasattr(row[1], "isoformat")
                                else str(row[1])
                            ),
                        }
                    )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.fetch_phase_transitions_failed",
                extra={
                    "event": "fetch_phase_transitions_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
        return rows

    def _fetch_failures_grouped(self, agent_id: str) -> list[dict[str, Any]]:
        """SELECT ``dispatcher.failures`` rows grouped by category for this agent.

        Returns list of ``{category, count, first_seen, last_seen}``
        dicts. Empty list on DB error.
        """
        assert self._conn is not None, "connect() must run before reading"
        rows: list[dict[str, Any]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT category, count(*), min(ts), max(ts) "
                    "FROM dispatcher.failures "
                    "WHERE agent_id = %s "
                    "GROUP BY category "
                    "ORDER BY count(*) DESC",
                    (agent_id,),
                )
                for row in cur.fetchall():
                    rows.append(
                        {
                            "category": str(row[0]) if row[0] is not None else "",
                            "count": int(row[1] or 0),
                            "first_seen": (
                                row[2].isoformat()
                                if hasattr(row[2], "isoformat")
                                else str(row[2])
                            ),
                            "last_seen": (
                                row[3].isoformat()
                                if hasattr(row[3], "isoformat")
                                else str(row[3])
                            ),
                        }
                    )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.fetch_failures_failed",
                extra={
                    "event": "fetch_failures_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
        return rows

    def _fetch_agent_total_duration_s(self, agent_id: str) -> int:
        """Compute wall-clock seconds since the agent claimed."""
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM (now() - started_at))::int "
                    "FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.fetch_duration_failed",
                extra={
                    "event": "fetch_duration_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return 0
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    def _fetch_phase_output(self, agent_id: str, phase: str) -> dict[str, Any] | None:
        """SELECT a single ``dispatcher.phase_outputs`` row JSON, or None."""
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT output_json FROM dispatcher.phase_outputs "
                    "WHERE agent_id = %s AND phase = %s",
                    (agent_id, phase),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.fetch_phase_output_failed",
                extra={
                    "event": "fetch_phase_output_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": phase,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None:
            return None
        raw = row[0]
        # psycopg returns JSONB as already-parsed Python objects when
        # the connection is configured normally. Guard against the
        # JSON-as-string case for tests that pass strings through.
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    def _file_retro_issue(
        self,
        agent_id: str,
        worktree: Path,
        entry: dict[str, Any],
    ) -> int | None:
        """Run ``gh issue create`` for one retro issue body.

        Returns the new issue number on success, ``None`` on failure.
        Failures log a warning but do not raise — one bad retro entry
        should not block the others or stop the cleanup phase.
        """
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not title or not body:
            self._log.warning(
                "daemon.retro_issue_missing_fields",
                extra={
                    "event": "retro_issue_missing_fields",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "has_title": bool(title),
                    "has_body": bool(body),
                },
            )
            return None

        # Defensive truncation on body length.
        if len(body) > MAX_RETRO_ISSUE_BODY_CHARS:
            body = body[:MAX_RETRO_ISSUE_BODY_CHARS] + "\n\n[truncated]"

        labels = entry.get("labels")
        if not isinstance(labels, list) or not labels:
            labels = list(DEFAULT_RETRO_LABELS)
        labels = [str(label) for label in labels if label]

        # Write body to a tmp file (no heredocs / inline body per
        # CLAUDE.md). One file per retro issue keeps reads idempotent.
        body_dir = worktree / "tmp" / "dispatcher-retro"
        try:
            body_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log.warning(
                "daemon.retro_body_dir_failed",
                extra={
                    "event": "retro_body_dir_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            return None

        # Filename uses a uuid suffix so multiple retro entries don't
        # collide on disk if their titles happen to slugify the same.
        body_path = body_dir / f"retro-{uuid.uuid4().hex[:8]}.md"
        try:
            body_path.write_text(body, encoding="utf-8")
        except OSError as exc:
            self._log.warning(
                "daemon.retro_body_write_failed",
                extra={
                    "event": "retro_body_write_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            return None

        cmd = [
            "gh",
            "issue",
            "create",
            "--repo",
            self._cfg.github_repo,
            "--title",
            title,
            "--body-file",
            str(body_path),
        ]
        for label in labels:
            cmd.extend(["--label", label])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=RETRO_GH_ISSUE_CREATE_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.retro_issue_create_subprocess_error",
                extra={
                    "event": "retro_issue_create_subprocess_error",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "title": title,
                    "detail": str(exc),
                },
            )
            return None

        if result.returncode != 0:
            self._log.warning(
                "daemon.retro_issue_create_failed",
                extra={
                    "event": "retro_issue_create_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "title": title,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return None

        # ``gh issue create`` prints the issue URL on stdout. Parse the
        # trailing ``/issues/<N>`` segment for logging.
        new_issue_number = self._parse_issue_url(result.stdout or "")
        self._log.info(
            "daemon.retro_issue_created",
            extra={
                "event": "retro_issue_created",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "title": title,
                "labels": labels,
                "new_issue_number": new_issue_number,
            },
        )
        return new_issue_number

    @staticmethod
    def _parse_issue_url(stdout: str) -> int | None:
        """Extract the trailing ``/issues/<N>`` integer from a gh URL."""
        import re  # noqa: PLC0415 — lazy import

        match = re.search(r"/issues/(\d+)", stdout)
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):  # pragma: no cover — regex guarantees digits
            return None

    def _cleanup_agent_worktree(self, agent: dict[str, Any]) -> None:
        """Run ``scripts/cleanup_worktree.sh`` against the agent's worktree.

        Transitions to :data:`PHASE_CLEANUP_DONE` on success or
        :data:`PHASE_CLEANUP_BLOCKED` on safety-check refusal. Per
        CLAUDE.md §Critical Rules, never bypass with ``--force`` —
        an operator can sweep blocked worktrees manually.
        """
        agent_id = agent["agent_id"]
        worktree_path = str(agent.get("worktree_path") or "")

        if not worktree_path:
            self._log.warning(
                "daemon.cleanup_missing_worktree_path",
                extra={
                    "event": "cleanup_missing_worktree_path",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._update_agent_phase(agent_id, PHASE_CLEANUP_BLOCKED)
            return

        # Worktree may already be gone (operator manually cleaned, or
        # cleanup ran in a prior tick that crashed before phase update).
        # Treat missing-directory as cleanup_done — there's nothing
        # left to remove.
        if not Path(worktree_path).exists():
            self._log.info(
                "daemon.cleanup_already_gone",
                extra={
                    "event": "cleanup_already_gone",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "worktree_path": worktree_path,
                },
            )
            self._update_agent_phase(agent_id, PHASE_CLEANUP_DONE)
            return

        repo_root = self._repo_root()
        cleanup_script = repo_root / "scripts" / "cleanup_worktree.sh"

        if not cleanup_script.exists():
            self._log.warning(
                "daemon.cleanup_script_missing",
                extra={
                    "event": "cleanup_script_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "expected_path": str(cleanup_script),
                },
            )
            self._update_agent_phase(agent_id, PHASE_CLEANUP_BLOCKED)
            return

        try:
            result = subprocess.run(
                [str(cleanup_script), worktree_path],
                capture_output=True,
                text=True,
                timeout=CLEANUP_WORKTREE_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.cleanup_subprocess_error",
                extra={
                    "event": "cleanup_subprocess_error",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "worktree_path": worktree_path,
                    "detail": str(exc),
                },
            )
            self._update_agent_phase(agent_id, PHASE_CLEANUP_BLOCKED)
            return

        if result.returncode == 0:
            self._update_agent_phase(agent_id, PHASE_CLEANUP_DONE)
            self._log.info(
                "daemon.cleanup_done",
                extra={
                    "event": "cleanup_done",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "worktree_path": worktree_path,
                },
            )
            return

        # Non-zero exit means the safety check refused (locked / no
        # session log / etc.). Per CLAUDE.md §Critical Rules, do NOT
        # bypass with ``--force``. An operator can sweep manually.
        self._update_agent_phase(agent_id, PHASE_CLEANUP_BLOCKED)
        self._log.warning(
            "daemon.cleanup_blocked",
            extra={
                "event": "cleanup_blocked",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "worktree_path": worktree_path,
                "exit_code": result.returncode,
                "stderr_tail": _stderr_tail(result.stderr),
                "stdout_tail": _stderr_tail(result.stdout),
            },
        )

    def _write_diagnosis_outcome_for_agent(
        self, agent_id: str, final_status: str
    ) -> None:
        """Write back the resolved outcome to pending diagnoser rows.

        Per spec §8 line 305: "once a retry resolves (success,
        escalation, or close), its outcome is written back to
        ``dispatcher.diagnoses.outcome``."

        Updates ``dispatcher.diagnoses.outcome`` for any rows where
        ``agent_id = %s AND outcome IS NULL`` with the JSONB shape::

            {
              "retry_outcome": "succeeded" | "failed",
              "final_status":  "<agent.status>",
              "resolved_at":   "<ISO-8601 ts>"
            }

        Idempotent — the ``outcome IS NULL`` predicate prevents a
        repeat call from overwriting existing outcome data.
        """
        assert self._conn is not None, "connect() must run before update"

        # ``plan_blocked`` (#2857) is the "plan correctly declined" terminal —
        # operationally a correct outcome, not a failure. ``needs_review``
        # (#2856) likewise: ralph produced real reviewer-approved code
        # and the draft PR + issue comment + label swap is a correct
        # outcome — it is the operator's call whether to ship. Classify
        # both alongside ``succeeded`` for the retry_outcome enum so
        # diagnoser effectiveness dashboards don't count correct-triage
        # decisions against the retry-success rate.
        retry_outcome = (
            "succeeded"
            if final_status in ("succeeded", "plan_blocked", "needs_review")
            else "failed"
        )
        outcome = {
            "retry_outcome": retry_outcome,
            "final_status": final_status,
            "resolved_at": _now_iso(),
        }
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.diagnoses "
                    "SET outcome = %s::jsonb "
                    "WHERE agent_id = %s AND outcome IS NULL",
                    (json.dumps(outcome), agent_id),
                )
                rowcount = cur.rowcount or 0
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.diagnosis_outcome_update_failed",
                extra={
                    "event": "diagnosis_outcome_update_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "final_status": final_status,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return

        if rowcount > 0:
            self._log.info(
                "daemon.diagnosis_outcome_written",
                extra={
                    "event": "diagnosis_outcome_written",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "final_status": final_status,
                    "retry_outcome": retry_outcome,
                    "rows_updated": rowcount,
                },
            )

    # ── Phase 3C failure detection + retry machinery (supervisor-tick) ─
    #
    # Issue #2791. Three supervisor-tick checks detect failures and
    # enqueue retry markers; a fourth processor drains the marker table
    # and resets agents. Each check is independent and wrapped in
    # try/except so one check's failure cannot kill siblings.
    #
    #   _check_stuck_agents     → stuck_timeout   failures + retry markers
    #   _check_gh_rate_limit    → gh_rate_exhausted failure + skip flag
    #   _process_retry_markers  → agent reset via fresh worktree
    #
    # The subprocess classifier (_classify_subprocess_failure) is called
    # from _run_subprocess_or_fail (Phase 3A/3B entry points) — it
    # returns the §8 tier-1 category so the retry path can decide
    # whether to enqueue a marker or escalate.

    def _write_failure(
        self,
        *,
        agent_id: str | None,
        category: str,
        detected_by: str,
        details: dict[str, Any],
    ) -> None:
        """INSERT one row into ``dispatcher.failures``.

        ``agent_id=None`` is permitted (the schema allows it) and used by
        the GitHub rate-limit guard which is a daemon-level signal not
        attributable to any single agent. Failures here are best-effort:
        a DB hiccup logs + rolls back but does not propagate, mirroring
        the hook-side behaviour in ``emit_failure.py`` (§9).
        """
        assert self._conn is not None, "connect() must run before failure write"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.failures "
                    "    (agent_id, category, detected_by, details) "
                    "VALUES (%s, %s, %s, %s)",
                    (agent_id, category, detected_by, json.dumps(details, default=str)),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.failure_insert_failed",
                extra={
                    "event": "failure_insert_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "category": category,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass

    @staticmethod
    def _classify_subprocess_failure(
        runner: str, exit_code: int, stderr_tail: str
    ) -> str:
        """Classify a non-zero subprocess exit per spec §8 table.

        Runner-aware: Gemini CLI returns distinct exit codes per spike
        0.4 (#2686) so the exit code alone classifies; Claude-p returns 1
        for everything and we fall back to stderr/stdout regex. An
        unknown runner is treated as ``claude`` (the Phase-3 default).

        Returns one of ``FAILURE_CATEGORY_*``:

        * ``subprocess_turn_limit`` — ``Reached max turns`` in stderr
          (Claude) or exit 53 (Gemini).
        * ``subprocess_auth_fail`` — ``Invalid API key`` /
          ``401 Unauthorized`` (Claude) or exit 41 (Gemini).
        * ``subprocess_crash`` — catch-all for non-zero exit that does
          not match a known pattern.
        """
        if runner == "gemini":
            mapped = GEMINI_EXIT_CODE_TO_CATEGORY.get(int(exit_code))
            if mapped is not None:
                return mapped
            return FAILURE_CATEGORY_SUBPROCESS_CRASH

        # Claude-p: stderr regex fallback. ``re.IGNORECASE`` so casing
        # differences in vendor error formats do not slip past us.
        import re  # noqa: PLC0415 — lazy import

        tail = stderr_tail or ""
        for pattern, category in _SUBPROCESS_STDERR_PATTERNS:
            if re.search(pattern, tail, re.IGNORECASE):
                return category
        return FAILURE_CATEGORY_SUBPROCESS_CRASH

    # ── stuck-timeout detection ────────────────────────────────────────

    def _stuck_timeout_for_phase(
        self, phase: str | None, *, overrides: dict[str, int] | None = None
    ) -> int:
        """Return the stuck_timeout threshold in seconds for a given phase.

        Resolution order (first hit wins):

        1. ``overrides`` argument (a pre-read JSONB object from
           ``dispatcher.config.stuck_timeout_s_by_phase`` — the
           supervisor tick reads this once per sweep and passes it in).
           Operators can live-edit any phase's threshold via this
           config row without a redeploy.
        2. :data:`STUCK_TIMEOUT_SECONDS_BY_PHASE` — module-level
           defaults based on observed phase runtime distributions.
        3. :data:`STUCK_TIMEOUT_SECONDS` — 30-minute fallback for any
           phase not covered above.

        A ``None`` or unknown phase falls back to the global default.
        Issue #2872 Bug B.
        """
        key = (phase or "").strip()
        if overrides and key and key in overrides:
            try:
                value = int(overrides[key])
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        if key and key in STUCK_TIMEOUT_SECONDS_BY_PHASE:
            return STUCK_TIMEOUT_SECONDS_BY_PHASE[key]
        return STUCK_TIMEOUT_SECONDS

    def _read_stuck_timeout_overrides(self) -> dict[str, int]:
        """Read the live ``stuck_timeout_s_by_phase`` config override.

        Returns an empty dict on missing row, malformed JSON, or any
        read error — the caller's fallback chain
        (:meth:`_stuck_timeout_for_phase`) picks up the module defaults
        cleanly in that case.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("stuck_timeout_s_by_phase",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return {}
        if row is None or row[0] is None:
            return {}
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def _check_stuck_agents(self) -> int:
        """Find running agents whose current phase has been stuck too long.

        Per-phase thresholds (issue #2872 Bug B) replace the old single
        30-minute global threshold. Each running agent's elapsed time
        since the most-recent ``phase_transitions.ts`` (or
        ``started_at`` if no transitions yet) is compared against the
        threshold for its current ``phase`` —
        :meth:`_stuck_timeout_for_phase` handles the lookup with live
        config overrides.

        Each flagged agent gets a ``dispatcher.failures`` row with
        ``category='stuck_timeout'``, is flipped to ``status='crashed'``,
        and enqueues a retry marker so
        :meth:`_process_retry_markers` can reset it with a fresh
        worktree. After #2927 every running row is daemon-owned
        (label-only /task coordination), so no kind-filter is needed
        — the #2903 task-skill guard has been removed.

        Returns the number of stuck agents flagged this tick (for
        logging). Exceptions are caught per-agent + logged; one bad
        row cannot stall the scan.
        """
        assert self._conn is not None, "connect() must run before stuck check"

        # Candidate rows: every running agent, regardless of elapsed
        # time. The per-phase threshold comparison happens in Python
        # so we can consult both the live config override and the
        # module-level defaults without expressing them as SQL.
        #
        # Fields: agent_id, issue_number, phase, elapsed_seconds.
        candidates: list[tuple[str, int | None, str | None, float]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT a.agent_id, a.issue_number, a.phase, "
                    "       EXTRACT(EPOCH FROM ("
                    "           now() - COALESCE(pt.last_ts, a.started_at)"
                    "       )) AS elapsed_seconds "
                    "FROM dispatcher.agents a "
                    "LEFT JOIN LATERAL ("
                    "    SELECT MAX(ts) AS last_ts "
                    "    FROM dispatcher.phase_transitions "
                    "    WHERE agent_id = a.agent_id"
                    ") pt ON TRUE "
                    "WHERE a.status = 'running'",
                )
                rows = cur.fetchall()
                for row in rows:
                    candidates.append(
                        (
                            str(row[0]),
                            int(row[1]) if row[1] is not None else None,
                            str(row[2]) if row[2] is not None else None,
                            float(row[3]) if row[3] is not None else 0.0,
                        )
                    )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.stuck_scan_failed",
                extra={
                    "event": "stuck_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return 0

        # Read the per-phase override map once per sweep. Empty on
        # missing row / malformed JSON — ``_stuck_timeout_for_phase``
        # falls back to module defaults cleanly.
        overrides = self._read_stuck_timeout_overrides()

        flagged = 0
        for agent_id, issue_number, phase, elapsed_seconds in candidates:
            threshold = self._stuck_timeout_for_phase(phase, overrides=overrides)
            if elapsed_seconds < threshold:
                continue
            try:
                self._write_failure(
                    agent_id=agent_id,
                    category=FAILURE_CATEGORY_STUCK_TIMEOUT,
                    detected_by="supervisor",
                    details={
                        "stuck_seconds": int(elapsed_seconds),
                        "threshold_seconds": threshold,
                        "last_known_phase": phase,
                        "issue_number": issue_number,
                    },
                )
                self._mark_agent_terminal(
                    agent_id,
                    status="crashed",
                    phase=phase or "unknown",
                    exit_code=None,
                )
                self._log.warning(
                    "daemon.failure_detected",
                    extra={
                        "event": "failure_detected",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "issue_number": issue_number,
                        "category": FAILURE_CATEGORY_STUCK_TIMEOUT,
                        "phase": phase,
                        "stuck_seconds": int(elapsed_seconds),
                        "threshold_seconds": threshold,
                    },
                )
                # Enqueue the tier-1 retry marker. The processor picks
                # it up on the next supervisor tick once the backoff
                # window elapses.
                self._create_retry_marker(
                    agent_id=agent_id,
                    reason=FAILURE_CATEGORY_STUCK_TIMEOUT,
                )
                flagged += 1
            except Exception:
                # Per-agent failure must not stall the whole scan.
                self._log.exception(
                    "daemon.stuck_flag_failed",
                    extra={
                        "event": "stuck_flag_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                    },
                )
        return flagged

    # ── GitHub rate-limit guard ────────────────────────────────────────

    def _gh_rate_skip_active(self) -> bool:
        """Return True if the GitHub rate-limit skip window is still active.

        Clears ``self._gh_rate_skip_until`` in place once the reset
        epoch has elapsed so subsequent calls return False without the
        caller needing to reset it explicitly.
        """
        if self._gh_rate_skip_until is None:
            return False
        if datetime.now(UTC) >= self._gh_rate_skip_until:
            self._log.info(
                "daemon.gh_rate_skip_cleared",
                extra={
                    "event": "gh_rate_skip_cleared",
                    "run_id": self._run_id,
                    "cleared_at": _now_iso(),
                },
            )
            self._gh_rate_skip_until = None
            return False
        return True

    def _check_gh_rate_limit(self) -> dict[str, Any] | None:
        """Probe ``gh api rate_limit`` and set the skip flag when low.

        Returns a dict with ``remaining`` + ``reset_ts`` on success, or
        ``None`` on subprocess failure. Subprocess failures are logged
        as warnings but do not block the tick — the daemon must survive
        transient GitHub hiccups.

        When ``remaining < GH_RATE_LIMIT_THRESHOLD``, writes a failure
        row with ``agent_id=NULL, category='gh_rate_exhausted'`` and
        sets ``self._gh_rate_skip_until`` to the UTC datetime equivalent
        of the ``reset`` epoch. On subsequent ticks,
        ``_gh_rate_skip_active`` returns True until that time passes.
        """
        cmd = [
            "gh",
            "api",
            "rate_limit",
            "--jq",
            ".resources.core",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_RATE_CHECK_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            self._log.warning(
                "daemon.gh_missing",
                extra={
                    "event": "gh_missing",
                    "run_id": self._run_id,
                    "detail": "rate_limit probe",
                },
            )
            return None
        except subprocess.TimeoutExpired:
            self._log.warning(
                "daemon.gh_rate_probe_timeout",
                extra={
                    "event": "gh_rate_probe_timeout",
                    "run_id": self._run_id,
                },
            )
            return None

        if result.returncode != 0:
            self._log.warning(
                "daemon.gh_rate_probe_failed",
                extra={
                    "event": "gh_rate_probe_failed",
                    "run_id": self._run_id,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return None

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            self._log.warning(
                "daemon.gh_rate_probe_invalid_json",
                extra={
                    "event": "gh_rate_probe_invalid_json",
                    "run_id": self._run_id,
                },
            )
            return None

        remaining = payload.get("remaining")
        reset_epoch = payload.get("reset")
        if not isinstance(remaining, int) or not isinstance(reset_epoch, int):
            # Malformed payload — treat as "can't say", skip enforcement.
            return None

        if remaining >= GH_RATE_LIMIT_THRESHOLD:
            # Healthy. If we had a prior skip window it already cleared
            # itself in ``_gh_rate_skip_active``; nothing to do here.
            self._log.info(
                "daemon.gh_rate_check",
                extra={
                    "event": "gh_rate_check",
                    "run_id": self._run_id,
                    "remaining": remaining,
                    "threshold": GH_RATE_LIMIT_THRESHOLD,
                },
            )
            return {"remaining": remaining, "reset_ts": reset_epoch}

        # Budget exhausted. Write the failure row and set the skip flag.
        reset_dt = datetime.fromtimestamp(reset_epoch, tz=UTC)
        self._write_failure(
            agent_id=None,
            category=FAILURE_CATEGORY_GH_RATE_EXHAUSTED,
            detected_by="supervisor",
            details={
                "remaining": remaining,
                "threshold": GH_RATE_LIMIT_THRESHOLD,
                "reset_ts": reset_dt.isoformat(),
                "reset_epoch": reset_epoch,
            },
        )
        self._gh_rate_skip_until = reset_dt
        self._log.warning(
            "daemon.gh_rate_limited",
            extra={
                "event": "gh_rate_limited",
                "run_id": self._run_id,
                "remaining": remaining,
                "threshold": GH_RATE_LIMIT_THRESHOLD,
                "reset_ts": reset_dt.isoformat(),
            },
        )
        return {"remaining": remaining, "reset_ts": reset_epoch}

    # ── retry markers ──────────────────────────────────────────────────

    def _backoff_seconds(self) -> tuple[int, ...]:
        """Read the ``backoff_seconds`` schedule from ``dispatcher.config``.

        Falls back to :data:`DEFAULT_BACKOFF_SECONDS` on any malformed
        row (missing key, non-list JSON, non-int entries, wrong length).
        The schedule must have exactly one entry per attempt (1..3) so
        `_create_retry_marker` can index by attempt number.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("backoff_seconds",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return DEFAULT_BACKOFF_SECONDS

        if row is None or row[0] is None:
            return DEFAULT_BACKOFF_SECONDS
        raw = row[0]
        # psycopg returns JSONB as the already-decoded Python object.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return DEFAULT_BACKOFF_SECONDS
        if not isinstance(raw, list) or len(raw) != MAX_RETRY_ATTEMPTS:
            return DEFAULT_BACKOFF_SECONDS
        try:
            parsed = tuple(int(x) for x in raw)
        except (TypeError, ValueError):
            return DEFAULT_BACKOFF_SECONDS
        if any(x <= 0 for x in parsed):
            return DEFAULT_BACKOFF_SECONDS
        return parsed

    def _create_retry_marker(self, *, agent_id: str, reason: str) -> int | None:
        """Enqueue a retry for ``agent_id`` at the next backoff interval.

        Returns the new ``attempt`` (1..3) on success, or ``None`` if
        the 3-attempt cap has been reached for this ``agent_id+reason``
        pair. When capped, the agent is flipped to ``status='failed'``
        and a structured ``daemon.retry_escalated`` log line fires so
        3D's diagnoser can pick it up.

        ``reason`` must be a tier-1 auto-retry category — see
        :data:`AUTO_RETRY_CATEGORIES`. Passing a non-retry category
        (e.g. ``subprocess_auth_fail``) is a caller bug; the method
        logs + returns None without writing.

        After #2927 every ``dispatcher.agents`` row is daemon-owned
        (label-only /task coordination replaced the DB-row interlock),
        so the #2903 task-skill defensive guard was removed.
        """
        assert self._conn is not None, "connect() must run before marker insert"

        if reason not in AUTO_RETRY_CATEGORIES:
            # Defensive: callers should gate on AUTO_RETRY_CATEGORIES
            # already, but a typo or future category addition should not
            # silently create retry markers for tier-2/3 failures.
            self._log.warning(
                "daemon.retry_marker_skipped",
                extra={
                    "event": "retry_marker_skipped",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "reason": reason,
                    "detail": "not a tier-1 auto-retry category",
                },
            )
            return None

        # Count existing markers for this agent+reason pair. The CHECK
        # constraint caps attempt at 3, so a fourth retry would fail
        # the INSERT anyway — catch it here first with a structured log
        # line so operators see the escalation.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM dispatcher.retry_markers "
                    "WHERE agent_id = %s AND reason = %s",
                    (agent_id, reason),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.retry_count_failed",
                extra={
                    "event": "retry_count_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        prior_count = int(row[0] or 0) if row else 0
        next_attempt = prior_count + 1

        if next_attempt > MAX_RETRY_ATTEMPTS:
            self._log.warning(
                "daemon.retry_escalated",
                extra={
                    "event": "retry_escalated",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "reason": reason,
                    "attempts_used": prior_count,
                    "detail": (
                        "max retries exceeded — 3D diagnoser picks up from failed"
                    ),
                },
            )
            # Flip to 'failed' so 3D's diagnoser picks it up. Preserve
            # the existing phase so operators can see where it got
            # stuck.
            self._mark_agent_terminal(
                agent_id, status="failed", phase="retry_exhausted", exit_code=None
            )
            return None

        backoff = self._backoff_seconds()
        # next_attempt is 1-indexed; backoff is 0-indexed.
        delay_seconds = backoff[next_attempt - 1]
        retry_after = datetime.now(UTC).timestamp() + delay_seconds

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.retry_markers "
                    "    (agent_id, reason, attempt, retry_after_ts) "
                    "VALUES (%s, %s, %s, to_timestamp(%s))",
                    (agent_id, reason, next_attempt, retry_after),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.retry_marker_insert_failed",
                extra={
                    "event": "retry_marker_insert_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "reason": reason,
                    "attempt": next_attempt,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        self._log.info(
            "daemon.retry_marker_created",
            extra={
                "event": "retry_marker_created",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "reason": reason,
                "attempt": next_attempt,
                "delay_seconds": delay_seconds,
                "retry_after_ts": datetime.fromtimestamp(
                    retry_after, tz=UTC
                ).isoformat(),
            },
        )
        return next_attempt

    def _drop_worktree_best_effort(self, worktree_path: str) -> bool:
        """Remove the agent's worktree so the retry starts from a fresh tree.

        Tries ``scripts/cleanup_worktree.sh`` first (the laptop-dispatcher
        convention — it handles stray lock files, uncommitted state, and
        submodule detritus). Falls back to ``git worktree remove --force``.
        Returns True on success, False on any failure (logged as a
        warning). The caller proceeds with the retry even on False —
        spec §8 calls this a mechanical fix, and an orphaned worktree
        is a much smaller problem than a stuck retry marker.

        The cleanup-script lookup uses ``_repo_root()`` (the daemon's
        CWD, which contains the ``scripts/`` tree in both the Fargate
        image and local-dev mode). The ``git worktree remove`` fallback
        uses ``_git_parent_root()`` instead — in Fargate, ``_repo_root()``
        is ``/app`` (no ``.git`` child), while ``_git_parent_root()`` is
        the baseline clone at ``/var/lib/dispatcher/judgemind``. Running
        ``git worktree remove`` from ``/app`` fails with "not a git
        repository" (#2821); running it with ``-C <git_parent>`` works
        from any CWD.
        """
        if not worktree_path:
            return False

        if not Path(worktree_path).exists():
            self._log.info(
                "daemon.worktree_already_gone",
                extra={
                    "event": "worktree_already_gone",
                    "run_id": self._run_id,
                    "worktree_path": worktree_path,
                },
            )
            return True

        repo_root = self._repo_root()
        git_parent = self._git_parent_root()
        cleanup_script = repo_root / "scripts" / "cleanup_worktree.sh"

        if cleanup_script.exists():
            try:
                result = subprocess.run(
                    [str(cleanup_script), worktree_path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode == 0:
                    return True
                self._log.warning(
                    "daemon.cleanup_worktree_nonzero",
                    extra={
                        "event": "cleanup_worktree_nonzero",
                        "run_id": self._run_id,
                        "worktree_path": worktree_path,
                        "exit_code": result.returncode,
                        "stderr_tail": _stderr_tail(result.stderr),
                    },
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                self._log.warning(
                    "daemon.cleanup_worktree_subprocess_error",
                    extra={
                        "event": "cleanup_worktree_subprocess_error",
                        "run_id": self._run_id,
                        "worktree_path": worktree_path,
                        "detail": str(exc),
                    },
                )

        # Fall back to raw ``git worktree remove --force``. This bypasses
        # ``cleanup_worktree.sh``'s safety checks (see CLAUDE.md §Never
        # bypass a safety check), but the retry marker processor has
        # already decided the worktree is toast — the only question is
        # whether we leave an orphan entry in ``git worktree list``.
        # ``-C <git_parent>`` anchors the command to the baseline clone's
        # ``.git`` rather than the daemon's CWD — see docstring.
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(git_parent),
                    "worktree",
                    "remove",
                    "--force",
                    worktree_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.worktree_remove_subprocess_error",
                extra={
                    "event": "worktree_remove_subprocess_error",
                    "run_id": self._run_id,
                    "worktree_path": worktree_path,
                    "detail": str(exc),
                },
            )
            return False
        if result.returncode == 0:
            return True
        self._log.warning(
            "daemon.worktree_remove_nonzero",
            extra={
                "event": "worktree_remove_nonzero",
                "run_id": self._run_id,
                "worktree_path": worktree_path,
                "exit_code": result.returncode,
                "stderr_tail": _stderr_tail(result.stderr),
            },
        )
        return False

    def _process_retry_markers(self, *, only_infra_preemption: bool = False) -> int:
        """Drain due retry markers, reset each agent, resolve the marker.

        Called from the supervisor tick (full drain, default) and from
        the scheduler tick with ``only_infra_preemption=True`` so
        infra-preemption retries resume before fresh queue claims on
        the same tick (issue #2949). For each unresolved marker whose
        ``retry_after_ts`` has elapsed:

        1. Best-effort drop the existing worktree (see
           :meth:`_drop_worktree_best_effort`).
        2. UPDATE the agent to ``status='retrying' phase='claiming'``.
           The next scheduler tick's claim path sees the retry state
           and re-orchestrates (creates a fresh worktree via 3A's
           ``_create_worktree``).

           ``retries_used`` increments only when the retry's reason is
           **not** in :data:`_INFRA_PREEMPTION_CATEGORIES`. Infra-
           preemption retries (daemon restart, operator killswitch)
           preserve the prior attempt count so that an agent which
           caught two dispatcher redeploys during a rapid-merge stretch
           doesn't burn its retry budget on infrastructure churn
           (issue #2936). Budgeted retries (``subprocess_crash``,
           ``stuck_timeout``, ``gh_rate_exhausted``) still increment so
           the 3B fix-ci cap and 3D diagnoser tier-3
           (``ci_red_after_retries``) see the full retry history.
        3. Mark the retry marker ``resolved_at = now()``.

        Returns the number of markers processed this tick. DB errors on
        any single marker are logged + rolled back; the loop continues.

        **Infra-preemption terminal-and-reclaim branch (issue #2925).**
        When the marker's reason is in :data:`_INFRA_PREEMPTION_CATEGORIES`
        (``daemon_restart_abandoned``, ``paused_by_killswitch``), the method
        takes a different path instead of resetting to ``retrying``:

        1. Call :meth:`_mark_agent_terminal` with ``status='failed'``,
           ``phase=reason`` — sets ``ended_at=now()``, drops the row from
           the active-issue partial UNIQUE INDEX, and removes
           ``status/in-progress`` from the issue.
        2. Add ``agent/ready`` back to the issue so the next
           ``_scan_queue_and_snapshot`` tick re-discovers it.
        3. Resolve the marker via ``UPDATE dispatcher.retry_markers
           SET resolved_at = now()``.
        4. Skip the ``phase_transitions ('retry_reset')`` insert — the row
           is terminal, so no future stuck_timeout MAX(ts) reset is needed.
        5. Emit ``daemon.retry_terminal_and_reclaim`` (distinct from
           ``retry_processed``) so CloudWatch can separate the two paths.

        For non-infra reasons (``subprocess_crash``, ``stuck_timeout``,
        ``gh_rate_exhausted``), the existing reset-to-retrying behavior is
        unchanged — that path is exercised by 3D's diagnoser and the
        ``_resume_retrying_agent`` handoff in steady state.

        The emitted ``daemon.retry_processed`` log event includes a
        ``retry_counted`` boolean so CloudWatch queries can separate
        budgeted retries (``retry_counted=true``) from free infra
        preemption retries (``retry_counted=false``).

        **Scheduler-tick pre-scan drain (issue #2949).** When
        ``only_infra_preemption=True`` the SELECT is narrowed to rows
        whose ``reason`` is in :data:`_INFRA_PREEMPTION_CATEGORIES`.
        Budgeted retries (``subprocess_crash``, ``stuck_timeout``,
        ``gh_rate_exhausted``, ``operator_retry``) are left for the
        supervisor-tick drain later in the cycle. Rationale:
        infra-preemption markers re-add ``agent/ready`` to the issue
        via the terminal-and-reclaim path; processing them before the
        queue scan ensures the interrupted issue lands in this tick's
        snapshot and is picked over any fresh claim of equal priority.
        Budgeted retries reset the existing row to
        ``status='retrying' phase='claiming'`` and are handled by
        :meth:`_resume_retrying_agent` on the orchestration worker
        thread, so their processing cadence does not affect claim
        ordering and can remain on the supervisor tick.
        """
        assert self._conn is not None, "connect() must run before marker drain"

        try:
            with self._conn.cursor() as cur:
                if only_infra_preemption:
                    # Issue #2949 — scheduler-tick pre-scan drain.
                    # Narrow to infra-preemption rows so the supervisor
                    # tick still owns the steady-state budgeted retry
                    # drain. ``reason = ANY(%s)`` uses a bind parameter
                    # so psycopg handles the list-to-array conversion.
                    cur.execute(
                        "SELECT m.marker_id, m.agent_id, m.reason, m.attempt, "
                        "       a.worktree_path, a.issue_number "
                        "FROM dispatcher.retry_markers m "
                        "JOIN dispatcher.agents a ON a.agent_id = m.agent_id "
                        "WHERE m.resolved_at IS NULL "
                        "  AND m.retry_after_ts <= now() "
                        "  AND m.reason = ANY(%s) "
                        "ORDER BY m.retry_after_ts ASC",
                        (list(_INFRA_PREEMPTION_CATEGORIES),),
                    )
                else:
                    cur.execute(
                        "SELECT m.marker_id, m.agent_id, m.reason, m.attempt, "
                        "       a.worktree_path, a.issue_number "
                        "FROM dispatcher.retry_markers m "
                        "JOIN dispatcher.agents a ON a.agent_id = m.agent_id "
                        "WHERE m.resolved_at IS NULL "
                        "  AND m.retry_after_ts <= now() "
                        "ORDER BY m.retry_after_ts ASC",
                    )
                due = cur.fetchall()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.retry_marker_scan_failed",
                extra={
                    "event": "retry_marker_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return 0

        processed = 0
        for row in due:
            marker_id = int(row[0])
            agent_id = str(row[1])
            reason = str(row[2])
            attempt = int(row[3])
            worktree_path = str(row[4]) if row[4] is not None else ""
            issue_number = int(row[5]) if row[5] is not None else None

            # Classify the retry: infra-preemption reasons are "free"
            # — the agent never ran under its own power, so the retry
            # should leave ``retries_used`` untouched. Any other
            # reason is an actual runtime problem and counts toward
            # the attempt budget (issue #2936).
            retry_counted = reason not in _INFRA_PREEMPTION_CATEGORIES

            try:
                self._drop_worktree_best_effort(worktree_path)

                if not retry_counted:
                    # ── Infra-preemption terminal-and-reclaim (#2925) ──
                    # Mark the old row terminal so ``idx_dispatcher_agents_active_issue``
                    # releases its slot and ``status/in-progress`` is removed.
                    # Then re-add ``agent/ready`` so the scheduler re-discovers
                    # the issue without a zombie retrying row lingering.
                    # Skip the ``phase_transitions('retry_reset')`` insert —
                    # the row is terminal and no future stuck_timeout clock
                    # reset is needed.
                    self._mark_agent_terminal(
                        agent_id,
                        status="failed",
                        phase=reason,
                        exit_code=None,
                        issue_number=issue_number,
                    )
                    if issue_number is not None:
                        self._gh_issue_add_labels(issue_number, ["agent/ready"])
                    with self._conn.cursor() as cur:
                        cur.execute(
                            "UPDATE dispatcher.retry_markers "
                            "SET resolved_at = now() "
                            "WHERE marker_id = %s",
                            (marker_id,),
                        )
                    self._conn.commit()
                    self._log.info(
                        "daemon.retry_terminal_and_reclaim",
                        extra={
                            "event": "retry_terminal_and_reclaim",
                            "run_id": self._run_id,
                            "agent_id": agent_id,
                            "issue_number": issue_number,
                            "reason": reason,
                            "attempt": attempt,
                            "marker_id": marker_id,
                        },
                    )
                    processed += 1
                    continue

                # ── Budgeted retry: reset to retrying/claiming ──
                # Two near-identical UPDATE statements — the difference
                # is the ``retries_used`` clause. Keeping them as
                # distinct string literals (rather than a dynamic
                # join) preserves the grep-ability of SQL statements
                # in the daemon, which the fakes in
                # ``test_daemon_phase3c.py`` rely on.
                reset_sql = (
                    "UPDATE dispatcher.agents "
                    "SET status = 'retrying', "
                    "    phase = 'claiming', "
                    "    retries_used = retries_used + 1, "
                    "    exit_code = NULL, "
                    "    ended_at = NULL "
                    "WHERE agent_id = %s"
                )

                with self._conn.cursor() as cur:
                    cur.execute(reset_sql, (agent_id,))
                    # #2872 — write a fresh ``phase_transitions`` row so
                    # the supervisor's stuck_timeout MAX(ts) comparison
                    # restarts its clock. Without this, the stale
                    # pre-retry ``plan`` transition from hours ago
                    # remains the MAX(ts), and the next stuck_scan
                    # fires ``stuck_timeout`` within seconds of the
                    # reset — exactly the cascading loop observed in
                    # the 2026-04-19 timeline.
                    cur.execute(
                        "INSERT INTO dispatcher.phase_transitions "
                        "    (agent_id, phase) "
                        "VALUES (%s, %s)",
                        (agent_id, "retry_reset"),
                    )
                    cur.execute(
                        "UPDATE dispatcher.retry_markers "
                        "SET resolved_at = now() "
                        "WHERE marker_id = %s",
                        (marker_id,),
                    )
                self._conn.commit()
                self._log.info(
                    "daemon.retry_processed",
                    extra={
                        "event": "retry_processed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "issue_number": issue_number,
                        "reason": reason,
                        "attempt": attempt,
                        "marker_id": marker_id,
                        "retry_counted": retry_counted,
                    },
                )
                processed += 1
            except Exception:
                self._log.exception(
                    "daemon.retry_process_failed",
                    extra={
                        "event": "retry_process_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "marker_id": marker_id,
                    },
                )
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover
                    pass
        return processed

    # ── Phase 3D (#2795): tier-2/3 diagnoser ───────────────────────────

    def _diagnoser_enabled(self) -> bool:
        """Read ``dispatcher.config.diagnoser_enabled`` and coerce to bool.

        Defaults to ``True`` when the config row is missing or malformed.
        The circuit breaker (:meth:`_check_diagnoser_circuit_breaker`)
        flips this to ``false`` via UPDATE when the 24h fallback rate
        exceeds :meth:`_diagnoser_fallback_threshold`.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("diagnoser_enabled",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return True

        if row is None or row[0] is None:
            return True
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return True
        if isinstance(raw, bool):
            return raw
        # JSON ``true``/``false`` → Python bool; anything else (numbers,
        # strings, dicts) is malformed — default to enabled.
        return True

    def _diagnoser_fallback_threshold(self) -> float:
        """Read ``diagnoser_fallback_rate_threshold`` from ``dispatcher.config``.

        Falls back to :data:`DEFAULT_CIRCUIT_BREAKER_THRESHOLD` (0.30)
        on missing row, malformed JSON, or out-of-range value
        (<0 or >1). The circuit breaker compares the 24h fallback
        ratio against this and trips when ratio > threshold AND the
        minimum diagnosis count is reached.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("diagnoser_fallback_rate_threshold",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return DEFAULT_CIRCUIT_BREAKER_THRESHOLD

        if row is None or row[0] is None:
            return DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        try:
            threshold = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        if threshold < 0.0 or threshold > 1.0:
            return DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        return threshold

    def _check_diagnoser_circuit_breaker(self) -> bool:
        """Trip the circuit breaker if the 24h fallback rate is too high.

        Spec §8 "Budget & safety": when >30% of diagnoses in the last
        24 h fell back (timeout, malformed JSON, subprocess crash) AND
        at least :data:`CIRCUIT_BREAKER_MIN_DIAGNOSES` diagnoses have
        run in the same window, flip
        ``dispatcher.config.diagnoser_enabled`` to ``false`` and log a
        ``daemon.diagnoser_circuit_breaker_tripped`` event. Operator
        manually re-enables.

        Returns True if the breaker was tripped this call, False
        otherwise. Called once per supervisor tick (cheap — one COUNT
        aggregation over a narrow time range).
        """
        assert self._conn is not None, "connect() must run before breaker check"

        threshold = self._diagnoser_fallback_threshold()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT "
                    "    COUNT(*) FILTER (WHERE status = 'failed'), "
                    "    COUNT(*) "
                    "FROM dispatcher.diagnoses "
                    "WHERE started_at > now() - make_interval(secs => %s)",
                    (CIRCUIT_BREAKER_WINDOW_SECONDS,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.diagnoser_circuit_breaker_scan_failed",
                extra={
                    "event": "diagnoser_circuit_breaker_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        if row is None:
            return False
        failed_n = int(row[0] or 0)
        total_n = int(row[1] or 0)

        if total_n < CIRCUIT_BREAKER_MIN_DIAGNOSES:
            # Not enough samples to judge — noisy early runs must not
            # trip the breaker. Spec §8 gates on both "rate > threshold"
            # AND "total diagnoses ≥ 5".
            return False

        fallback_rate = failed_n / total_n if total_n else 0.0
        if fallback_rate <= threshold:
            return False

        # Trip the breaker. Write the config update and log.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.config "
                    "SET value = 'false', "
                    "    updated_at = now(), "
                    "    updated_by = 'diagnoser_circuit_breaker' "
                    "WHERE key = %s",
                    ("diagnoser_enabled",),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.diagnoser_circuit_breaker_flip_failed",
                extra={
                    "event": "diagnoser_circuit_breaker_flip_failed",
                    "run_id": self._run_id,
                    "fallback_rate": round(fallback_rate, 3),
                    "total_diagnoses": total_n,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        self._log.warning(
            "daemon.diagnoser_circuit_breaker_tripped",
            extra={
                "event": "diagnoser_circuit_breaker_tripped",
                "run_id": self._run_id,
                "fallback_rate": round(fallback_rate, 3),
                "threshold": threshold,
                "total_diagnoses_24h": total_n,
                "failed_diagnoses_24h": failed_n,
                "min_diagnoses": CIRCUIT_BREAKER_MIN_DIAGNOSES,
            },
        )
        return True

    # ── Overnight-safety circuit breaker (#2860) ───────────────────────

    def _cb_config_int(self, key: str, default: int) -> int:
        """Read a numeric key from ``dispatcher.config`` with a fallback.

        Shared helper for the three overnight-safety knobs
        (``circuit_breaker_window_minutes``, ``_window_size``,
        ``_bad_outcome_threshold``). Returns ``default`` on missing row,
        malformed JSON, non-integer value, or DB error — the breaker
        must never crash a terminal transition.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return default
        if row is None or row[0] is None:
            return default
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return default
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return default
        if n < 0:
            return default
        return n

    def _cb_enabled(self) -> bool:
        """Read ``dispatcher.config.circuit_breaker_enabled`` as a bool.

        Defaults to ``True`` when the config row is missing or malformed —
        the overnight-safety rail is the safe default. Operators who want
        to disable it must explicitly set ``false``.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("circuit_breaker_enabled",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return True
        if row is None or row[0] is None:
            return True
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return True
        if isinstance(raw, bool):
            return raw
        return True

    def _read_cap_flipped_by(self) -> str | None:
        """Read ``dispatcher.config.cap_flipped_by``. ``None`` if unset or error.

        Used by the scheduler tick to detect when the operator has
        manually flipped ``concurrency_cap`` back up after the breaker
        opened (diagnostic trail cleared; breaker auto-closes).

        **JSONB handling.** psycopg3 natively decodes jsonb → Python
        values — a JSONB string like ``"circuit_breaker"`` comes back
        as the Python string ``"circuit_breaker"`` (quotes unwrapped);
        JSONB ``null`` comes back as Python ``None``; JSONB number /
        bool / array / object come back as their Python equivalents.
        Test fixtures sometimes feed us the raw JSONB bytes (e.g.
        ``'"circuit_breaker"'`` — JSON-encoded with quotes) via the
        fake cursor, so we try ``json.loads`` first on strings and
        fall back to the raw string if that fails. Either shape
        resolves to the same Python string.
        """
        assert self._conn is not None, "connect() must run before config read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("cap_flipped_by",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None or row[0] is None:
            return None
        raw = row[0]
        if isinstance(raw, str):
            # Try to JSON-decode first. If the string is a JSON-encoded
            # string literal (fake-cursor test path) ``json.loads``
            # gives us the unwrapped value. If it is already the
            # unwrapped Python string (psycopg3 production path) the
            # decode fails with ``JSONDecodeError`` and we use the raw
            # string directly.
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            if isinstance(decoded, str):
                return decoded
            return None
        return None

    def _write_terminal_outcome(self, agent_id: str, status: str) -> None:
        """Append a row to ``dispatcher.terminal_outcomes``.

        Called by :meth:`_mark_agent_terminal` for every terminal
        status. ``issue_number`` is looked up from ``dispatcher.agents``
        so the outcome row is self-contained (no JOIN needed at scan
        time). Append-only: the ring-buffer semantics come from the
        rolling-window scan in :meth:`_evaluate_circuit_breaker`, not
        from deleting rows on write.
        """
        assert self._conn is not None, "connect() must run before outcome write"
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT issue_number FROM dispatcher.agents WHERE agent_id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
            issue_number = (
                int(row[0]) if row is not None and row[0] is not None else None
            )
            cur.execute(
                "INSERT INTO dispatcher.terminal_outcomes "
                "    (agent_id, issue_number, status, ended_at) "
                "VALUES (%s, %s, %s, now())",
                (agent_id, issue_number, status),
            )
        self._conn.commit()

    def _is_bad_outcome(self, status: str) -> bool:
        """Classify a terminal status for the overnight-safety breaker.

        Tolerant classifier — any status not explicitly known to be
        "good" (i.e. ``succeeded``) counts as bad. This matches the
        issue #2860 scope note: if #2856 ships ``needs_review`` after
        this PR merges (or a later PR adds another correct-outcome
        terminal), that status is conservatively counted as bad until
        :data:`OVERNIGHT_CB_GOOD_OUTCOME_STATUSES` is updated to
        include it. Erring on the side of opening the breaker is the
        safer bias for overnight operation.
        """
        return status not in OVERNIGHT_CB_GOOD_OUTCOME_STATUSES

    def _evaluate_circuit_breaker(self, agent_id: str) -> bool:
        """Trip the overnight-safety circuit breaker if the streak is bad.

        Runs post-terminal-transition (called from
        :meth:`_mark_agent_terminal`). Scans
        ``dispatcher.terminal_outcomes`` for the last ``window_size``
        rows whose ``ended_at`` falls inside the last ``window_minutes``,
        counts how many are in :data:`OVERNIGHT_CB_GOOD_OUTCOME_STATUSES`-
        complement via :meth:`_is_bad_outcome`, and flips
        ``concurrency_cap`` to 0 when the bad count reaches the
        threshold.

        Idempotent: if ``concurrency_cap`` is already 0 the breaker
        does nothing (no log spam, no duplicate Telegram alert). The
        ``cap_flipped_by`` trail is always rewritten to
        :data:`CAP_FLIPPED_BY_CIRCUIT_BREAKER` when the breaker triggers
        so the admin cockpit shows the open banner even if the cap
        happened to already be 0 (e.g. operator paused, then cascade
        hit threshold).

        Returns True when the breaker opened (or re-asserted an already-
        open state) this call; False when the threshold was not met or
        the breaker is disabled via config.
        """
        assert self._conn is not None, "connect() must run before breaker eval"

        if not self._cb_enabled():
            return False

        window_minutes = self._cb_config_int(
            "circuit_breaker_window_minutes", DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES
        )
        window_size = self._cb_config_int(
            "circuit_breaker_window_size", DEFAULT_OVERNIGHT_CB_WINDOW_SIZE
        )
        threshold = self._cb_config_int(
            "circuit_breaker_bad_outcome_threshold",
            DEFAULT_OVERNIGHT_CB_BAD_OUTCOME_THRESHOLD,
        )

        # Defensive: the seed rows should never produce these, but if
        # an operator sets ``window_size=0`` or
        # ``threshold=0`` via the config panel, treat the breaker as
        # disabled rather than tripping on the empty-window edge case.
        if window_size <= 0 or threshold <= 0:
            return False

        try:
            with self._conn.cursor() as cur:
                # #2927: /task subagents no longer write to
                # ``dispatcher.agents`` (label-only coordination), so
                # every ``terminal_outcomes`` row is daemon-owned by
                # construction and the breaker query reverts to its
                # pre-#2866 shape. The ``a.kind = 'task'`` JOIN added
                # by #2921 is no longer needed.
                #
                # #2942: correlate each terminal_outcomes row with the
                # most-recent dispatcher.failures.category for the same
                # agent_id so infra-preempted outcomes
                # (daemon_restart_abandoned, paused_by_killswitch) can
                # be filtered out before the bad-outcome count. The
                # correlated subquery mirrors _build_failure_summary's
                # "freshest failure row per agent" semantics. agent_id
                # may be NULL on synthetic test rows; the subquery
                # simply returns NULL → treated as non-infra.
                cur.execute(
                    "SELECT t.status, "
                    "       (SELECT f.category FROM dispatcher.failures f "
                    "        WHERE f.agent_id = t.agent_id "
                    "        ORDER BY f.ts DESC LIMIT 1) AS latest_category "
                    "FROM dispatcher.terminal_outcomes t "
                    "WHERE t.ended_at > now() - make_interval(mins => %s) "
                    "ORDER BY t.ended_at DESC "
                    "LIMIT %s",
                    (window_minutes, window_size),
                )
                rows = cur.fetchall()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.circuit_breaker_scan_failed",
                extra={
                    "event": "circuit_breaker_scan_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        # #2942: strip infra-preempted rows before counting bad outcomes.
        # Infra preemption (daemon restart, operator killswitch) is not
        # an agent-driven failure; a streak of redeploys must not trip
        # the breaker. Reuse the module-level frozenset — same set as
        # _process_retry_markers uses for free-retry classification.
        raw_pairs = [(str(r[0]), r[1]) for r in rows if r and r[0] is not None]
        filtered = [
            (s, c) for s, c in raw_pairs if c not in _INFRA_PREEMPTION_CATEGORIES
        ]
        skipped_infra_count = len(raw_pairs) - len(filtered)
        statuses = [s for s, _c in filtered]
        bad_count = sum(1 for s in statuses if self._is_bad_outcome(s))
        window_total = len(raw_pairs)

        if bad_count < threshold:
            # Emit an INFO event when infra rows were filtered so post-
            # incident CloudWatch queries can confirm the filter worked
            # even when the breaker did not trip.
            if skipped_infra_count > 0:
                self._log.info(
                    "daemon.circuit_breaker_eval_skipped_infra",
                    extra={
                        "event": "circuit_breaker_eval_skipped_infra",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "skipped_infra_count": skipped_infra_count,
                        "window_total": window_total,
                        "bad_count": bad_count,
                        "threshold": threshold,
                    },
                )
            return False

        # Threshold met. Flip ``concurrency_cap`` to 0 and stamp
        # ``cap_flipped_by``. Use a single UPDATE per config row so a
        # partial failure (e.g. the ``cap_flipped_by`` write 500s) does
        # not roll back the cap flip — the cap flip is the safety
        # action, the flag is the diagnostic trail.
        current_cap = self._cb_config_int("concurrency_cap", -1)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.config "
                    "SET value = '0', "
                    "    updated_at = now(), "
                    "    updated_by = %s "
                    "WHERE key = 'concurrency_cap'",
                    (CAP_FLIPPED_BY_CIRCUIT_BREAKER,),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.circuit_breaker_flip_failed",
                extra={
                    "event": "circuit_breaker_flip_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "bad_count": bad_count,
                    "window_size": window_size,
                    "threshold": threshold,
                    "window_minutes": window_minutes,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.config "
                    "SET value = %s, "
                    "    updated_at = now(), "
                    "    updated_by = %s "
                    "WHERE key = 'cap_flipped_by'",
                    (
                        json.dumps(CAP_FLIPPED_BY_CIRCUIT_BREAKER),
                        CAP_FLIPPED_BY_CIRCUIT_BREAKER,
                    ),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.circuit_breaker_flag_failed",
                extra={
                    "event": "circuit_breaker_flag_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            # Continue — the cap flip above is the safety action; the
            # flag is diagnostic. Still log the open event.

        # Idempotence signal: if cap was already 0 we are re-asserting
        # the same state, not opening fresh. The log level + Telegram
        # alert branch downstream uses this to avoid notification spam.
        was_already_open = current_cap == 0

        self._log.warning(
            "daemon.circuit_breaker_opened",
            extra={
                "event": "circuit_breaker_opened",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "bad_count": bad_count,
                "window_size": window_size,
                "threshold": threshold,
                "window_minutes": window_minutes,
                "previous_cap": current_cap,
                "was_already_open": was_already_open,
                "recent_statuses": statuses,
                "skipped_infra_count": skipped_infra_count,
                "window_total": window_total,
            },
        )

        if not was_already_open:
            # Only alert operator on fresh opens — re-asserting an
            # already-open state is a no-op from the operator's
            # perspective.
            try:
                self._send_circuit_breaker_telegram_alert(
                    bad_count=bad_count,
                    window_size=window_size,
                    window_minutes=window_minutes,
                    statuses=statuses,
                )
            except Exception:
                self._log.exception(
                    "daemon.circuit_breaker_telegram_failed",
                    extra={
                        "event": "circuit_breaker_telegram_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                    },
                )
        return True

    def _send_circuit_breaker_telegram_alert(
        self,
        *,
        bad_count: int,
        window_size: int,
        window_minutes: int,
        statuses: list[str],
    ) -> None:
        """Fire a Telegram alert via ``scripts/notify-telegram.sh``.

        Best-effort — the helper script exits 0 when Telegram is
        unconfigured (secret missing, no allowed user IDs) so the
        daemon never depends on Telegram being wired up in a given
        environment. A non-zero exit is logged as a warning and the
        breaker is still considered "opened" (the cap flip is the
        safety action; the alert is operator-UX).
        """
        repo_root = self._repo_root_for_notify_script()
        notify_script = repo_root / NOTIFY_TELEGRAM_SCRIPT_RELPATH
        if not notify_script.exists():
            self._log.info(
                "daemon.circuit_breaker_telegram_skipped_no_script",
                extra={
                    "event": "circuit_breaker_telegram_skipped_no_script",
                    "run_id": self._run_id,
                    "script_path": str(notify_script),
                },
            )
            return

        # Write the message to a temp file and pass via --message-file
        # so the daemon does not inline secret-ish content (bad status
        # strings) into argv where it could leak to ps listings.
        message = self._render_circuit_breaker_telegram_message(
            bad_count=bad_count,
            window_size=window_size,
            window_minutes=window_minutes,
            statuses=statuses,
        )
        tmp_dir = repo_root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        msg_path = tmp_dir / f"circuit-breaker-alert-{self._run_id or 'unknown'}.txt"
        msg_path.write_text(message, encoding="utf-8")

        try:
            result = subprocess.run(
                [str(notify_script), "--message-file", str(msg_path)],
                capture_output=True,
                text=True,
                timeout=NOTIFY_TELEGRAM_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._log.warning(
                "daemon.circuit_breaker_telegram_timeout",
                extra={
                    "event": "circuit_breaker_telegram_timeout",
                    "run_id": self._run_id,
                    "timeout_s": NOTIFY_TELEGRAM_SUBPROCESS_TIMEOUT_SECONDS,
                },
            )
            return

        if result.returncode == 0:
            self._log.info(
                "daemon.circuit_breaker_telegram_sent",
                extra={
                    "event": "circuit_breaker_telegram_sent",
                    "run_id": self._run_id,
                },
            )
        else:
            self._log.warning(
                "daemon.circuit_breaker_telegram_nonzero_exit",
                extra={
                    "event": "circuit_breaker_telegram_nonzero_exit",
                    "run_id": self._run_id,
                    "exit_code": result.returncode,
                    "stderr_tail": (result.stderr or "")[-500:],
                },
            )

    def _render_circuit_breaker_telegram_message(
        self,
        *,
        bad_count: int,
        window_size: int,
        window_minutes: int,
        statuses: list[str],
    ) -> str:
        """Render the Telegram alert body.

        Plain text (``notify-telegram.sh`` uses ``parse_mode=HTML`` but
        HTML entities would leak through as literal characters on a
        misconfigured client — plain text is safer). The recent-status
        list is comma-joined newest first so the operator can see the
        cascade pattern at a glance.
        """
        recent = ", ".join(statuses[: min(window_size, 10)]) if statuses else "(none)"
        return (
            "Dispatcher circuit breaker OPENED\n"
            f"{bad_count}/{window_size} of the last terminal outcomes in the "
            f"last {window_minutes} min were bad.\n"
            f"concurrency_cap has been set to 0 — the daemon will not spawn "
            f"new agents.\n"
            f"Recent statuses (newest first): {recent}\n"
            "Manually flip cap back to ≥1 in the admin cockpit once you've "
            "triaged the underlying failure pattern."
        )

    def _repo_root_for_notify_script(self) -> Path:
        """Resolve the repo root for ``scripts/notify-telegram.sh``.

        In production the daemon runs inside the Fargate container with
        the repo at ``/var/lib/dispatcher/repo`` (see
        :data:`DEFAULT_BASELINE_REPO_ROOT`). Tests inject a different
        root by setting ``self._cfg.baseline_repo_root``. Fall back to
        the daemon module's parent (``scripts/dispatcher/`` → two
        levels up to the repo root) when neither is set.
        """
        baseline = getattr(self._cfg, "baseline_repo_root", None)
        if baseline:
            return Path(baseline)
        return Path(__file__).resolve().parents[2]

    def _check_circuit_breaker_auto_close(self, current_cap: int) -> bool:
        """Auto-close the breaker if the operator manually raised the cap.

        Called by scheduler_tick once the live ``concurrency_cap`` has
        been read (passed as ``current_cap``). When:

          - ``current_cap >= 1``, AND
          - ``cap_flipped_by == 'circuit_breaker'``,

        the operator has explicitly re-enabled the dispatcher after
        the breaker opened — log ``daemon.circuit_breaker_closed`` and
        clear ``cap_flipped_by`` back to null so subsequent cap flips
        by the operator don't get mis-attributed to the breaker.

        Returns True when the breaker was auto-closed this tick; False
        otherwise. The common case (``cap_flipped_by`` null or not the
        breaker) is a single SELECT + early return.
        """
        assert self._conn is not None, "connect() must run before auto-close"
        if current_cap < 1:
            return False

        flipped_by = self._read_cap_flipped_by()
        if flipped_by != CAP_FLIPPED_BY_CIRCUIT_BREAKER:
            return False

        # Operator manually raised cap — clear the flag.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.config "
                    "SET value = 'null', "
                    "    updated_at = now(), "
                    "    updated_by = 'circuit_breaker_auto_close' "
                    "WHERE key = 'cap_flipped_by'",
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.circuit_breaker_auto_close_failed",
                extra={
                    "event": "circuit_breaker_auto_close_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False

        self._log.info(
            "daemon.circuit_breaker_closed",
            extra={
                "event": "circuit_breaker_closed",
                "run_id": self._run_id,
                "new_cap": current_cap,
            },
        )
        return True

    def _find_diagnoser_candidates(self) -> list[dict[str, Any]]:
        """Find tier-2/3 failures that need a diagnosis this tick.

        A "candidate" is a ``dispatcher.failures`` row that:

        * Has not yet triggered a diagnosis (no ``dispatcher.diagnoses``
          row with matching ``failure_id``).
        * Is in a tier-2 recurrence category with a prior failure of
          the same category for the same agent within the recurrence
          window, OR in :data:`TIER_2_FIRST_OCCURRENCE_CATEGORIES`,
          OR in :data:`TIER_3_CATEGORIES`.

        Returns a list of ``{failure_id, agent_id, category, tier,
        issue_number, details, failure_ts}`` dicts, newest first.
        Caller spawns one diagnoser subprocess per candidate.
        """
        assert self._conn is not None, "connect() must run before candidate scan"

        all_trigger_categories = list(
            TIER_2_RECURRENCE_CATEGORIES
            | TIER_2_FIRST_OCCURRENCE_CATEGORIES
            | TIER_3_CATEGORIES
        )

        candidates: list[dict[str, Any]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT f.failure_id, f.agent_id, f.category, f.details, f.ts "
                    "FROM dispatcher.failures f "
                    "LEFT JOIN dispatcher.diagnoses d "
                    "    ON d.failure_id = f.failure_id "
                    "WHERE d.failure_id IS NULL "
                    "  AND f.agent_id IS NOT NULL "
                    "  AND f.category = ANY(%s) "
                    "  AND f.ts > now() - make_interval(secs => %s) "
                    "ORDER BY f.ts DESC "
                    "LIMIT 20",
                    (all_trigger_categories, TIER_2_RECURRENCE_WINDOW_SECONDS),
                )
                rows = cur.fetchall()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.diagnoser_candidate_scan_failed",
                extra={
                    "event": "diagnoser_candidate_scan_failed",
                    "run_id": self._run_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return []

        for row in rows:
            failure_id = int(row[0])
            agent_id = str(row[1]) if row[1] is not None else None
            category = str(row[2])
            details = row[3] or {}
            failure_ts = row[4]
            if agent_id is None:
                # Daemon-level failures (``gh_rate_exhausted`` with
                # ``agent_id=NULL``) are never diagnosed — no agent to
                # recover.
                continue
            # Parse details if it's a JSON string.
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except json.JSONDecodeError:
                    details = {}

            issue_number: int | None = None
            if isinstance(details, dict):
                issue_number = details.get("issue_number")
                if issue_number is not None:
                    try:
                        issue_number = int(issue_number)
                    except (TypeError, ValueError):
                        issue_number = None

            # Determine tier. First-occurrence and tier-3 categories
            # diagnose immediately. Tier-2 recurrence categories
            # diagnose ONLY if a prior failure of the same category
            # exists for the same agent within the recurrence window.
            tier: int
            if category in TIER_3_CATEGORIES:
                tier = 3
            elif category in TIER_2_FIRST_OCCURRENCE_CATEGORIES:
                tier = 2
            elif category in TIER_2_RECURRENCE_CATEGORIES:
                if not self._has_prior_same_category_failure(
                    agent_id=agent_id,
                    category=category,
                    before_failure_id=failure_id,
                ):
                    # First occurrence of a tier-1 mechanical category —
                    # the mechanical retry path handles it, not the
                    # diagnoser.
                    continue
                tier = 2
            else:
                # Defensive: category was in the SQL filter but doesn't
                # match any known bucket.
                continue

            candidates.append(
                {
                    "failure_id": failure_id,
                    "agent_id": agent_id,
                    "category": category,
                    "tier": tier,
                    "issue_number": issue_number,
                    "details": details if isinstance(details, dict) else {},
                    "failure_ts": failure_ts,
                }
            )
        return candidates

    def _has_prior_same_category_failure(
        self, *, agent_id: str, category: str, before_failure_id: int
    ) -> bool:
        """True if the agent has a prior failure of ``category`` within the window.

        Used by :meth:`_find_diagnoser_candidates` to gate tier-2
        recurrence categories — the diagnoser only fires on the second
        failure in the pair, not the first.
        """
        assert self._conn is not None, "connect() must run before recurrence check"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM dispatcher.failures "
                    "WHERE agent_id = %s "
                    "  AND category = %s "
                    "  AND failure_id < %s "
                    "  AND ts > now() - make_interval(secs => %s) "
                    "LIMIT 1",
                    (
                        agent_id,
                        category,
                        before_failure_id,
                        TIER_2_RECURRENCE_WINDOW_SECONDS,
                    ),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return False
        return row is not None

    def _build_diagnoser_context(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Assemble the JSONB context bundle passed to ``/diagnose-failure``.

        The skill reads this via
        ``SELECT context FROM dispatcher.diagnoses`` after the daemon
        INSERTs the pending row. Shape matches the input contract in
        ``.claude/skills/diagnose-failure/SKILL.md``.

        All sub-queries are individually try/except'd and fall back to
        empty/null on failure — a stale or missing context is better
        than no diagnosis. The skill's decision-tree defaults to
        ``escalate`` on sparse context.
        """
        assert self._conn is not None, "connect() must run before context build"

        agent_id = candidate["agent_id"]
        failure_id = candidate["failure_id"]

        # Fetch the agent row for worktree_path + the failure envelope.
        agent_row: dict[str, Any] = {}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issue_number, worktree_path, pr_number "
                    "FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
            if row is not None:
                agent_row = {
                    "issue_number": int(row[0]) if row[0] is not None else None,
                    "worktree_path": str(row[1]) if row[1] is not None else "",
                    "pr_number": int(row[2]) if row[2] is not None else None,
                }
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

        issue_number = candidate.get("issue_number") or agent_row.get("issue_number")

        # Recent phase transitions — last ~10.
        phase_transitions: list[dict[str, Any]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT phase, ts FROM dispatcher.phase_transitions "
                    "WHERE agent_id = %s "
                    "ORDER BY ts DESC LIMIT 10",
                    (agent_id,),
                )
                rows = cur.fetchall()
            self._conn.commit()
            for r in rows:
                phase_transitions.append(
                    {
                        "phase": str(r[0]) if r[0] is not None else None,
                        "ts": r[1].isoformat() if r[1] is not None else None,
                    }
                )
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

        # Prior failures on the same issue — across all agents.
        prior_failures: list[dict[str, Any]] = []
        if issue_number is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT f.failure_id, f.category, f.ts, f.details "
                        "FROM dispatcher.failures f "
                        "JOIN dispatcher.agents a "
                        "    ON a.agent_id = f.agent_id "
                        "WHERE a.issue_number = %s "
                        "  AND f.failure_id <> %s "
                        "ORDER BY f.ts DESC LIMIT 20",
                        (issue_number, failure_id),
                    )
                    rows = cur.fetchall()
                self._conn.commit()
                for r in rows:
                    prior_failures.append(
                        {
                            "failure_id": int(r[0]),
                            "category": str(r[1]),
                            "ts": r[2].isoformat() if r[2] is not None else None,
                            "details": r[3] if isinstance(r[3], dict) else {},
                        }
                    )
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover
                    pass

        # ralph-done.txt content if present.
        ralph_done_content: str | None = None
        worktree_path = agent_row.get("worktree_path") or ""
        if worktree_path:
            ralph_done_path = Path(worktree_path) / "tmp" / "ralph" / "ralph-done.txt"
            try:
                if ralph_done_path.exists():
                    ralph_done_content = ralph_done_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
            except Exception:  # pragma: no cover — best-effort read
                ralph_done_content = None

        # Issue title + body — fetch lazily via gh so the daemon does
        # not duplicate the ``_fetch_issue_bundle`` MCP path. The skill
        # can always re-fetch if the context is stale.
        issue_title = ""
        issue_body = ""
        if issue_number is not None:
            try:
                result = subprocess.run(
                    [
                        "gh",
                        "issue",
                        "view",
                        str(issue_number),
                        "--repo",
                        self._cfg.github_repo,
                        "--json",
                        "title,body",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    payload = json.loads(result.stdout)
                    issue_title = str(payload.get("title") or "")
                    issue_body = str(payload.get("body") or "")
            except (
                FileNotFoundError,
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
            ):
                pass

        # prior_mechanical_fix — tier 2 only, describes what was tried.
        prior_mechanical_fix: dict[str, Any] | None = None
        if (
            candidate["tier"] == 2
            and candidate["category"] in TIER_2_RECURRENCE_CATEGORIES
        ):
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT attempt, retry_after_ts, resolved_at "
                        "FROM dispatcher.retry_markers "
                        "WHERE agent_id = %s AND reason = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (agent_id, candidate["category"]),
                    )
                    r = cur.fetchone()
                self._conn.commit()
                if r is not None:
                    prior_mechanical_fix = {
                        "category": candidate["category"],
                        "attempt": int(r[0]) if r[0] is not None else None,
                        "retry_after_ts": r[1].isoformat()
                        if r[1] is not None
                        else None,
                        "outcome": "resolved" if r[2] is not None else "pending",
                    }
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover
                    pass

        details = candidate.get("details") or {}
        pr_number = agent_row.get("pr_number")
        if pr_number is None and isinstance(details, dict):
            pr_number = details.get("pr_number")
        pr_url: str | None = None
        if pr_number is not None:
            pr_url = f"https://github.com/{self._cfg.github_repo}/pull/{pr_number}"
        ci_log_url: str | None = None
        if isinstance(details, dict):
            ci_log_url = details.get("ci_log_url") or None

        # Issue #3010 — AC-infeasibility context extras. The diagnoser
        # skill's per-category guidance for ``ralph_ac_infeasible`` and
        # ``summary_ac_infeasible`` reads these fields; on other
        # categories they stay absent so the bundle shape is unchanged.
        infeasible_acs: list[dict[str, Any]] | None = None
        deferred_acs: list[dict[str, Any]] | None = None
        ralph_diff: str | None = None
        summary_ac_mapping: list[dict[str, Any]] | None = None
        issue_acceptance_criteria: list[str] | None = None
        if candidate["category"] in (
            FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
            FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
        ):
            if isinstance(details, dict):
                raw_inf = details.get("infeasible_acs") or []
                if isinstance(raw_inf, list):
                    infeasible_acs = [e for e in raw_inf if isinstance(e, dict)]
            if issue_body:
                issue_acceptance_criteria = self._extract_acceptance_criteria(
                    issue_body
                )
            if candidate["category"] == FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE:
                if isinstance(details, dict):
                    raw_def = details.get("deferred_acs") or []
                    if isinstance(raw_def, list):
                        deferred_acs = [e for e in raw_def if isinstance(e, dict)]
                    raw_diff = details.get("ralph_diff")
                    if isinstance(raw_diff, str):
                        ralph_diff = raw_diff
                    raw_map = details.get("summary_ac_mapping") or []
                    if isinstance(raw_map, list):
                        summary_ac_mapping = [e for e in raw_map if isinstance(e, dict)]

        # Issue #3032 — expanded context bundle for the open-menu
        # diagnoser. ``prior_diagnoses_this_issue`` lets the LLM see
        # "I recommended retry twice and it failed twice — try
        # something else" without re-deriving from the failure log.
        # ``recent_fleet_decisions`` lets it detect fleet-wide spates
        # ("5 different issues hit the same PAT-scope failure today —
        # the right action is to file a prerequisite task, not patch
        # per issue"). Both are best-effort; empty list on any DB
        # error — the skill's decision tree tolerates sparse context.
        prior_diagnoses_this_issue: list[dict[str, Any]] = []
        if issue_number is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT d.diagnosis_id, f.category, d.recommendation, "
                        "       d.completed_at "
                        "FROM dispatcher.diagnoses d "
                        "JOIN dispatcher.agents a ON a.agent_id = d.agent_id "
                        "JOIN dispatcher.failures f ON f.failure_id = d.failure_id "
                        "WHERE a.issue_number = %s "
                        "  AND d.status = 'completed' "
                        "  AND d.failure_id <> %s "
                        "ORDER BY d.completed_at DESC LIMIT 10",
                        (issue_number, failure_id),
                    )
                    rows = cur.fetchall()
                self._conn.commit()
                for r in rows:
                    rec = r[2]
                    if isinstance(rec, str):
                        try:
                            rec = json.loads(rec)
                        except json.JSONDecodeError:
                            rec = {}
                    prior_diagnoses_this_issue.append(
                        {
                            "diagnosis_id": int(r[0]),
                            "failure_category": str(r[1]),
                            "recommendation": rec if isinstance(rec, dict) else {},
                            "completed_at": r[3].isoformat()
                            if r[3] is not None
                            else None,
                        }
                    )
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover
                    pass

        recent_fleet_decisions: list[dict[str, Any]] = []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT d.diagnosis_id, d.agent_id, a.issue_number, "
                    "       f.category, d.recommendation, d.completed_at "
                    "FROM dispatcher.diagnoses d "
                    "JOIN dispatcher.agents a ON a.agent_id = d.agent_id "
                    "JOIN dispatcher.failures f ON f.failure_id = d.failure_id "
                    "WHERE d.status = 'completed' "
                    "  AND d.completed_at > now() - interval '6 hours' "
                    "ORDER BY d.completed_at DESC LIMIT 20",
                )
                rows = cur.fetchall()
            self._conn.commit()
            for r in rows:
                rec = r[4]
                if isinstance(rec, str):
                    try:
                        rec = json.loads(rec)
                    except json.JSONDecodeError:
                        rec = {}
                action = ""
                reasoning = ""
                if isinstance(rec, dict):
                    raw_action = rec.get("action")
                    if isinstance(raw_action, str):
                        action = raw_action
                    raw_reasoning = rec.get("reasoning")
                    if isinstance(raw_reasoning, str):
                        reasoning = raw_reasoning
                recent_fleet_decisions.append(
                    {
                        "diagnosis_id": int(r[0]),
                        "agent_id": str(r[1]) if r[1] is not None else None,
                        "issue_number": int(r[2]) if r[2] is not None else None,
                        "failure_category": str(r[3]),
                        "action": action,
                        "reasoning": reasoning,
                        "completed_at": r[5].isoformat() if r[5] is not None else None,
                    }
                )
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

        bundle: dict[str, Any] = {
            "agent_id": agent_id,
            "failure_id": failure_id,
            "failure_category": candidate["category"],
            "tier": candidate["tier"],
            "issue_number": issue_number,
            "issue_title": issue_title,
            "issue_body": issue_body,
            "recent_phase_transitions": phase_transitions,
            "prior_failures": prior_failures,
            "prior_diagnoses_this_issue": prior_diagnoses_this_issue,
            "recent_fleet_decisions": recent_fleet_decisions,
            "ralph_done_content": ralph_done_content,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "ci_log_url": ci_log_url,
            "prior_mechanical_fix": prior_mechanical_fix,
            "worktree_path": worktree_path,
        }
        if infeasible_acs is not None:
            bundle["infeasible_acs"] = infeasible_acs
        if issue_acceptance_criteria is not None:
            bundle["issue_acceptance_criteria"] = issue_acceptance_criteria
        if deferred_acs is not None:
            bundle["deferred_acs"] = deferred_acs
        if ralph_diff is not None:
            bundle["ralph_diff"] = ralph_diff
        if summary_ac_mapping is not None:
            bundle["summary_ac_mapping"] = summary_ac_mapping
        return bundle

    def _insert_pending_diagnosis(
        self,
        *,
        failure_id: int,
        agent_id: str,
        context: dict[str, Any],
    ) -> int | None:
        """INSERT a ``status='pending'`` row and return the new diagnosis_id.

        Returns None on DB error. The caller falls back to the fixed
        mechanical escalation policy when this returns None.
        """
        assert self._conn is not None, "connect() must run before diagnosis insert"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.diagnoses "
                    "    (failure_id, agent_id, status, context) "
                    "VALUES (%s, %s, 'pending', %s) "
                    "RETURNING diagnosis_id",
                    (failure_id, agent_id, json.dumps(context, default=str)),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.diagnosis_insert_failed",
                extra={
                    "event": "diagnosis_insert_failed",
                    "run_id": self._run_id,
                    "failure_id": failure_id,
                    "agent_id": agent_id,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        if row is None:
            return None
        return int(row[0])

    def _mark_diagnosis_failed(self, diagnosis_id: int, reason: str) -> None:
        """UPDATE diagnosis row to ``status='failed'`` with a reason log event.

        Used by every fallback path (timeout, non-zero exit, malformed
        recommendation JSON, unknown action). The circuit breaker
        counts these rows against the fallback threshold.
        """
        assert self._conn is not None, "connect() must run before diagnosis update"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.diagnoses "
                    "SET status = 'failed', "
                    "    completed_at = now() "
                    "WHERE diagnosis_id = %s",
                    (diagnosis_id,),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.diagnosis_mark_failed_failed",
                extra={
                    "event": "diagnosis_mark_failed_failed",
                    "run_id": self._run_id,
                    "diagnosis_id": diagnosis_id,
                    "reason": reason,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
        self._log.warning(
            "daemon.diagnoser_fallback",
            extra={
                "event": "diagnoser_fallback",
                "run_id": self._run_id,
                "diagnosis_id": diagnosis_id,
                "reason": reason,
            },
        )

    def _spawn_diagnoser_subprocess(self, diagnosis_id: int) -> tuple[int | None, str]:
        """Spawn ``claude -p /diagnose-failure <diagnosis_id>`` synchronously.

        Returns ``(exit_code, stderr_tail)``. ``exit_code=None`` means
        the subprocess timed out or could not be launched. Isolated
        here so tests can monkeypatch ``subprocess.Popen`` without
        touching the surrounding DB-write logic.

        **Real-time stream forwarding (#3017).** Like
        :meth:`_spawn_phase_subprocess`, every stdout/stderr line from
        the diagnoser is structured-logged and mirrored to
        ``{repo_root}/tmp/.dispatcher/diagnose-<diagnosis_id>.jsonl`` so
        a malformed-JSON diagnoser recommendation (see
        ``recommendation_missing_or_malformed_json`` in
        :meth:`_consume_diagnosis`) has a triageable trail. ``agent_id``
        is logged as ``f"diagnose-{diagnosis_id}"`` so CloudWatch Log
        Insights can group diagnoser output alongside the failed agent
        it was diagnosing.

        The diagnoser runs at the repo root (no per-agent worktree) so
        the JSONL mirror goes under ``{repo_root}/tmp/.dispatcher/``.

        **Cwd must be the baseline clone (#3033).** The dispatcher
        Fargate image's ``WORKDIR`` is ``/app``, which does NOT contain
        ``.claude/skills/diagnose-failure/`` — only
        ``scripts/dispatcher/`` and a few hook/preflight files are
        ``COPY``ed into the image. The full ``.claude/skills/`` tree
        lives in the baseline git clone at
        ``self._cfg.baseline_repo_root`` (e.g.
        ``/var/lib/dispatcher/repo``). Without ``cwd=`` set, the
        ``claude`` CLI inherits ``/app`` as cwd and exits in <11s with
        a NULL recommendation because the ``/diagnose-failure`` skill
        isn't discoverable — the exact symptom documented on #3033
        (five consecutive failures with sub-11s durations and NULL
        recommendation). The same cwd anchor is applied to the JSONL
        mirror path so the triage trail lands alongside the phase
        spawns' own ``tmp/.dispatcher/`` files.
        """
        cmd = [
            "claude",
            "-p",
            f"/diagnose-failure {diagnosis_id}",
            "--max-turns",
            str(DIAGNOSER_MAX_TURNS),
            "--model",
            DIAGNOSER_MODEL,
            # See ``_spawn_phase_subprocess`` for the full rationale on
            # why the Fargate dispatcher runs subagents with
            # ``--dangerously-skip-permissions``. Same reasoning applies
            # to the diagnoser subagent — non-interactive, protected by
            # the narrowed preflight hook, needs to read PR/issue state
            # and touch log files that the default permission policy
            # would block. See issue #2982.
            "--dangerously-skip-permissions",
        ]

        repo_root = self._repo_root_for_notify_script()
        jsonl_path = (
            repo_root / "tmp" / ".dispatcher" / f"diagnose-{diagnosis_id}.jsonl"
        )
        stderr_buf: list[str] = []
        stdout_buf: list[str] = []

        class _ListSink:
            """File-like sink that appends each write to ``buf``."""

            def __init__(self, buf: list[str]) -> None:
                self._buf = buf

            def write(self, data: str) -> int:
                self._buf.append(data)
                return len(data)

            def flush(self) -> None:
                return None

        try:
            proc: subprocess.Popen[str] = subprocess.Popen(  # noqa: S603 — literal trusted cmd
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(repo_root),
            )
        except FileNotFoundError:
            return None, "claude binary not found"

        threads = stream_subprocess_output_async(
            proc,
            agent_id=f"diagnose-{diagnosis_id}",
            issue_number=None,
            phase="diagnose",
            logger=self._log,
            jsonl_path=jsonl_path,
            stdout_sink=_ListSink(stdout_buf),
            stderr_sink=_ListSink(stderr_buf),
        )
        try:
            returncode = proc.wait(timeout=DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            threads.join(timeout=10)
            tail = ("".join(stderr_buf))[-500:]
            return None, tail

        threads.join(timeout=10)
        stderr_tail = ("".join(stderr_buf))[-500:]
        return returncode, stderr_tail

    def _read_recommendation(self, diagnosis_id: int) -> dict[str, Any] | None:
        """Read ``dispatcher.diagnoses.recommendation`` for the given row.

        Returns the recommendation dict, or None if the row is missing,
        the recommendation column is NULL, or the JSON is malformed.
        None means "fall back to mechanical escalation".
        """
        assert self._conn is not None, "connect() must run before recommendation read"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT recommendation FROM dispatcher.diagnoses "
                    "WHERE diagnosis_id = %s",
                    (diagnosis_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return None

        if row is None or row[0] is None:
            return None
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _validate_recommendation(self, recommendation: dict[str, Any]) -> str | None:
        """Return the action string if valid, None otherwise.

        Shape check: ``action`` must be in :data:`DIAGNOSER_ACTIONS`.
        Per-action required-payload checks:
          - ``retry_with_hint`` — non-empty string ``hint``.
          - ``reissue`` — non-empty string ``new_scope``.
          - ``file_prerequisite_task`` — non-empty string ``title`` and
            ``body``.
          - ``block_on_existing_task`` — positive integer
            ``blocker_issue_number``.
        All other fields are advisory.

        Returns None when the action is missing, not in the known set,
        or when a required payload field for the action is missing or
        malformed. Callers treat None as "escalate fallback" per the
        hardened parse path added in issue #3032 — never silently
        retrying on malformed input.
        """
        action = recommendation.get("action")
        if not isinstance(action, str) or action not in DIAGNOSER_ACTIONS:
            return None
        if action == "retry_with_hint":
            hint = recommendation.get("hint")
            if not isinstance(hint, str) or not hint.strip():
                return None
        if action == "reissue":
            new_scope = recommendation.get("new_scope")
            if not isinstance(new_scope, str) or not new_scope.strip():
                return None
        if action == "file_prerequisite_task":
            title = recommendation.get("title")
            body = recommendation.get("body")
            if not isinstance(title, str) or not title.strip():
                return None
            if not isinstance(body, str) or not body.strip():
                return None
        if action == "block_on_existing_task":
            blocker = recommendation.get("blocker_issue_number")
            if (
                not isinstance(blocker, int)
                or isinstance(blocker, bool)
                or blocker <= 0
            ):
                return None
        return action

    def _consume_diagnosis(self, diagnosis_id: int, candidate: dict[str, Any]) -> str:
        """Read the recommendation and execute the deterministic action.

        Returns the action string that was consumed (one of
        :data:`DIAGNOSER_ACTIONS`, or ``"escalate_fallback"`` when the
        recommendation was malformed and we escalated mechanically).

        Issue #3032 hardens the parse path:
          - Missing recommendation (row absent or JSONB NULL) → escalate
            with reason ``recommendation_missing_or_malformed_json``.
          - Valid dict but unknown ``action`` string → escalate AND
            persist a row to ``dispatcher.unrecognized_diagnoser_actions``
            so operators can review.
          - Valid dict + known action but required payload missing
            (e.g. ``retry_with_hint`` without ``hint``) → escalate.
          - Every branch now logs the raw LLM output via
            ``daemon.diagnoser_parse_failed`` so prompt tuning has a
            signal trail. Never silently retries.
        """
        recommendation = self._read_recommendation(diagnosis_id)
        if recommendation is None:
            self._log.warning(
                "daemon.diagnoser_parse_failed",
                extra={
                    "event": "diagnoser_parse_failed",
                    "run_id": self._run_id,
                    "diagnosis_id": diagnosis_id,
                    "reason": "recommendation_missing_or_malformed_json",
                    "raw_output": None,
                },
            )
            self._mark_diagnosis_failed(
                diagnosis_id, reason="recommendation_missing_or_malformed_json"
            )
            self._apply_mechanical_escalation(candidate)
            return "escalate_fallback"

        action = self._validate_recommendation(recommendation)
        if action is None:
            # Distinguish unrecognized action strings (a novel LLM
            # proposal — persist for operator review) from missing /
            # malformed required payload fields.
            raw_action = recommendation.get("action")
            raw_output = self._serialize_raw_output(recommendation)
            if (
                isinstance(raw_action, str)
                and raw_action.strip()
                and raw_action not in DIAGNOSER_ACTIONS
            ):
                self._persist_unrecognized_action(
                    diagnosis_id=diagnosis_id,
                    action_name=raw_action,
                    payload=recommendation,
                )
                reason = "unrecognized_action"
            elif not isinstance(raw_action, str) or not raw_action.strip():
                reason = "action_field_missing_or_non_string"
            else:
                reason = "missing_required_payload"
            self._log.warning(
                "daemon.diagnoser_parse_failed",
                extra={
                    "event": "diagnoser_parse_failed",
                    "run_id": self._run_id,
                    "diagnosis_id": diagnosis_id,
                    "reason": reason,
                    "raw_output": raw_output,
                },
            )
            # Keep the legacy ``diagnosis_action_unknown`` event so
            # existing CloudWatch dashboards do not regress.
            self._log.warning(
                "daemon.diagnosis_action_unknown",
                extra={
                    "event": "diagnosis_action_unknown",
                    "run_id": self._run_id,
                    "diagnosis_id": diagnosis_id,
                    "reason": reason,
                    "raw_recommendation": recommendation,
                },
            )
            self._mark_diagnosis_failed(diagnosis_id, reason=reason)
            self._apply_mechanical_escalation(candidate)
            return "escalate_fallback"

        # Valid action — dispatch.
        agent_id = candidate["agent_id"]
        issue_number = candidate.get("issue_number")
        reasoning = str(recommendation.get("reasoning") or "")

        self._log.info(
            "daemon.diagnosis_consumed",
            extra={
                "event": "diagnosis_consumed",
                "run_id": self._run_id,
                "diagnosis_id": diagnosis_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "action": action,
            },
        )

        if action == "retry":
            self._consume_action_retry(agent_id=agent_id)
        elif action == "retry_with_hint":
            self._consume_action_retry_with_hint(
                agent_id=agent_id,
                issue_number=issue_number,
                hint=str(recommendation.get("hint") or ""),
            )
        elif action == "reissue":
            self._consume_action_reissue(
                agent_id=agent_id,
                issue_number=issue_number,
                diagnosis_id=diagnosis_id,
                reasoning=reasoning,
                new_scope=str(recommendation.get("new_scope") or ""),
            )
        elif action == "escalate":
            self._consume_action_escalate(
                agent_id=agent_id,
                issue_number=issue_number,
                reasoning=reasoning,
            )
        elif action == "close":
            self._consume_action_close(
                agent_id=agent_id,
                issue_number=issue_number,
                reasoning=reasoning,
            )
        elif action == "block_and_comment":
            self._consume_action_block_and_comment(
                agent_id=agent_id,
                issue_number=issue_number,
                reasoning=reasoning,
            )
        elif action == "file_prerequisite_task":
            self._consume_action_file_prerequisite_task(
                agent_id=agent_id,
                issue_number=issue_number,
                title=str(recommendation.get("title") or ""),
                body=str(recommendation.get("body") or ""),
                block_labels=self._coerce_str_list(recommendation.get("block_labels")),
                reasoning=reasoning,
            )
        elif action == "block_on_existing_task":
            blocker_issue_number = recommendation.get("blocker_issue_number")
            self._consume_action_block_on_existing_task(
                agent_id=agent_id,
                issue_number=issue_number,
                blocker_issue_number=int(blocker_issue_number)
                if isinstance(blocker_issue_number, int)
                and not isinstance(blocker_issue_number, bool)
                else 0,
                reasoning=reasoning,
            )

        return action

    @staticmethod
    def _coerce_str_list(value: Any) -> list[str]:
        """Return ``value`` as a list of strings, dropping non-string items."""
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def _serialize_raw_output(recommendation: Any) -> str:
        """Serialize the raw LLM recommendation for the parse-fail log.

        Always returns a bounded JSON string so CloudWatch Insights
        queries do not blow out memory on a pathological LLM output.
        Failures fall back to ``str(...)`` truncated at 4 KiB — we'd
        rather log a partial representation than drop the field
        entirely.
        """
        try:
            text = json.dumps(recommendation, default=str, sort_keys=True)
        except Exception:  # pragma: no cover — defensive
            text = str(recommendation)
        return text[:4096]

    def _persist_unrecognized_action(
        self, *, diagnosis_id: int, action_name: str, payload: Any
    ) -> None:
        """Insert a row into ``dispatcher.unrecognized_diagnoser_actions``.

        Issue #3032 opens the diagnoser action menu — the LLM may
        propose a novel action the daemon does not recognize. Rather
        than silently drop it, persist the action name + the full
        recommendation payload so operators can review and decide
        whether to implement a handler. The caller still falls through
        to ``escalate`` so the current failure is not stuck.

        Row-insert failures here are logged and swallowed — the
        escalate fallback is the authoritative recovery path, and a
        broken log row must not block it.
        """
        assert self._conn is not None, "connect() must run before action persist"
        try:
            payload_json = json.dumps(payload, default=str)
        except Exception:  # pragma: no cover — defensive
            payload_json = "{}"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.unrecognized_diagnoser_actions "
                    "    (diagnosis_id, action_name, payload) "
                    "VALUES (%s, %s, %s)",
                    (diagnosis_id, action_name, payload_json),
                )
            self._conn.commit()
        except Exception:
            self._log.exception(
                "daemon.unrecognized_action_persist_failed",
                extra={
                    "event": "unrecognized_action_persist_failed",
                    "run_id": self._run_id,
                    "diagnosis_id": diagnosis_id,
                    "action_name": action_name,
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

    def _consume_action_retry(self, *, agent_id: str) -> None:
        """Create a tier-1-shaped retry marker via the existing machinery.

        Reuses :data:`FAILURE_CATEGORY_SUBPROCESS_CRASH` as the marker
        reason — all three :data:`AUTO_RETRY_CATEGORIES` are functionally
        equivalent at this point (they share the backoff schedule and
        the worktree-drop step), and ``subprocess_crash`` is the most
        neutral category for a diagnoser-initiated retry.
        """
        self._create_retry_marker(
            agent_id=agent_id, reason=FAILURE_CATEGORY_SUBPROCESS_CRASH
        )

    def _consume_action_retry_with_hint(
        self, *, agent_id: str, issue_number: int | None, hint: str
    ) -> None:
        """Post the hint as an issue comment, then create a retry marker."""
        if issue_number is not None and hint:
            self._gh_issue_comment(issue_number, hint)
        self._create_retry_marker(
            agent_id=agent_id, reason=FAILURE_CATEGORY_SUBPROCESS_CRASH
        )

    def _consume_action_reissue(
        self,
        *,
        agent_id: str,
        issue_number: int | None,
        diagnosis_id: int,
        reasoning: str,
        new_scope: str,
    ) -> None:
        """Post diagnosis summary, replace issue body, ensure agent/ready, retry."""
        if issue_number is not None:
            summary = (
                "## Diagnosis (3D reissue)\n\n"
                f"{reasoning}\n\n"
                "_Scope replaced below — see updated body._"
            )
            self._gh_issue_comment(issue_number, summary)
            if new_scope:
                self._gh_issue_set_body(issue_number, new_scope)
            # Ensure agent/ready remains so the next scheduler tick can
            # re-claim. --add-label is idempotent.
            self._gh_issue_add_labels(issue_number, ["agent/ready"])
        self._create_retry_marker(
            agent_id=agent_id, reason=FAILURE_CATEGORY_SUBPROCESS_CRASH
        )

    def _consume_action_escalate(
        self,
        *,
        agent_id: str,
        issue_number: int | None,
        reasoning: str,
    ) -> None:
        """Add needs-human + p1 labels, post diagnosis, mark agent failed."""
        if issue_number is not None:
            summary = (
                "## Diagnosis (3D escalation)\n\n"
                f"{reasoning}\n\n"
                "_Needs human review — diagnoser could not auto-resolve._"
            )
            self._gh_issue_comment(issue_number, summary)
            self._gh_issue_add_labels(
                issue_number, ["status/needs-human", "priority/p1"]
            )
        self._mark_agent_terminal(
            agent_id, status="failed", phase="diagnoser_escalate", exit_code=None
        )

    def _consume_action_close(
        self,
        *,
        agent_id: str,
        issue_number: int | None,
        reasoning: str,
    ) -> None:
        """Add status/invalid, close with diagnosis as close comment."""
        if issue_number is not None:
            summary = (
                "## Diagnosis (3D close)\n\n"
                f"{reasoning}\n\n"
                "_Diagnoser determined this issue is not actionable as filed._"
            )
            self._gh_issue_add_labels(issue_number, ["status/invalid"])
            self._gh_issue_close(issue_number, comment=summary, reason="not planned")
        self._mark_agent_terminal(
            agent_id, status="failed", phase="diagnoser_close", exit_code=None
        )

    def _consume_action_block_and_comment(
        self,
        *,
        agent_id: str,
        issue_number: int | None,
        reasoning: str,
    ) -> None:
        """Apply ``status/blocked``, remove ``agent/ready``, post a comment.

        Issue #3032. Used when the diagnoser identifies a deterministic
        operator-action blocker (PAT scope, missing secret, branch
        protection, infra gap) that does not yet deserve a tracking
        issue of its own. The current issue is marked blocked so no
        other agent picks it up, the ``agent/ready`` label is stripped
        so the dispatcher queue-scan skips it, and the reasoning is
        posted as a comment so the operator sees the context.
        """
        if issue_number is not None:
            summary = (
                "## Diagnosis (3D block)\n\n"
                f"{reasoning}\n\n"
                "_Blocked — operator action required before work resumes._"
            )
            self._gh_issue_comment(issue_number, summary)
            self._gh_issue_add_labels(issue_number, ["status/blocked"])
            self._gh_issue_remove_labels(issue_number, ["agent/ready"])
        self._mark_agent_terminal(
            agent_id,
            status="failed",
            phase="diagnoser_block_and_comment",
            exit_code=None,
        )

    def _consume_action_file_prerequisite_task(
        self,
        *,
        agent_id: str,
        issue_number: int | None,
        title: str,
        body: str,
        block_labels: list[str] | None,
        reasoning: str,
    ) -> None:
        """Create a new tracking issue and block the current issue on it.

        Issue #3032. Used when the diagnoser identifies a root cause
        that deserves its own tracking issue (e.g. "add workflow scope
        to dispatcher PAT"). Runs ``gh issue create`` with the
        diagnoser-supplied title + body, captures the new issue
        number, then appends ``Blocked by #<new>`` to the current
        issue body and applies ``status/blocked``. If the new-issue
        create fails, falls back to :meth:`_consume_action_escalate`
        so the current failure still surfaces to the operator.
        """
        new_issue_number = self._gh_issue_create(
            title=title, body=body, labels=block_labels or None
        )
        if new_issue_number is None or issue_number is None:
            # Fall back to escalate — record the intended action in the
            # reasoning so the operator sees what the diagnoser wanted.
            fallback_reasoning = (
                f"{reasoning}\n\n"
                "_Diagnoser proposed `file_prerequisite_task` "
                f"(title={title!r}) but the new-issue create failed "
                "or the current issue has no number. Escalating instead._"
            )
            self._consume_action_escalate(
                agent_id=agent_id,
                issue_number=issue_number,
                reasoning=fallback_reasoning,
            )
            return

        summary = (
            "## Diagnosis (3D block-on-prerequisite)\n\n"
            f"{reasoning}\n\n"
            f"Filed a prerequisite tracking issue: #{new_issue_number}. "
            "This issue is blocked until that lands.\n\n"
            "_Blocked via `file_prerequisite_task`._"
        )
        self._gh_issue_comment(issue_number, summary)
        self._gh_issue_append_body(issue_number, f"\n\nBlocked by #{new_issue_number}")
        self._gh_issue_add_labels(issue_number, ["status/blocked"])
        self._gh_issue_remove_labels(issue_number, ["agent/ready"])
        self._mark_agent_terminal(
            agent_id,
            status="failed",
            phase="diagnoser_file_prerequisite_task",
            exit_code=None,
        )

    def _consume_action_block_on_existing_task(
        self,
        *,
        agent_id: str,
        issue_number: int | None,
        blocker_issue_number: int,
        reasoning: str,
    ) -> None:
        """Block the current issue on an already-open tracking issue.

        Issue #3032. Used when the diagnoser identifies an existing
        open issue that tracks the blocker (avoids duplicate tickets).
        Validates the blocker exists and is open via ``gh issue view``;
        if validation fails, falls back to escalate.
        """
        if issue_number is None:
            # No current issue to update — escalate.
            self._consume_action_escalate(
                agent_id=agent_id,
                issue_number=issue_number,
                reasoning=reasoning,
            )
            return
        if not self._gh_issue_is_open(blocker_issue_number):
            fallback_reasoning = (
                f"{reasoning}\n\n"
                f"_Diagnoser proposed `block_on_existing_task` "
                f"(blocker=#{blocker_issue_number}) but the target "
                "issue is not open (not found, closed, or validation "
                "error). Escalating instead._"
            )
            self._consume_action_escalate(
                agent_id=agent_id,
                issue_number=issue_number,
                reasoning=fallback_reasoning,
            )
            return

        summary = (
            "## Diagnosis (3D block-on-existing)\n\n"
            f"{reasoning}\n\n"
            f"This issue is blocked on existing tracking issue "
            f"#{blocker_issue_number}.\n\n"
            "_Blocked via `block_on_existing_task`._"
        )
        self._gh_issue_comment(issue_number, summary)
        self._gh_issue_append_body(
            issue_number, f"\n\nBlocked by #{blocker_issue_number}"
        )
        self._gh_issue_add_labels(issue_number, ["status/blocked"])
        self._gh_issue_remove_labels(issue_number, ["agent/ready"])
        self._mark_agent_terminal(
            agent_id,
            status="failed",
            phase="diagnoser_block_on_existing_task",
            exit_code=None,
        )

    def _apply_mechanical_escalation(self, candidate: dict[str, Any]) -> None:
        """Fallback path: behave like ``escalate`` with a canned reasoning.

        Spec §8 "Budget & safety": diagnoser timeout / malformed JSON /
        subprocess crash → fall back to fixed mechanical policy, which
        for tier 2/3 means escalate-to-human. No retry — if the
        diagnoser itself is broken, another retry won't help.
        """
        agent_id = candidate["agent_id"]
        issue_number = candidate.get("issue_number")
        category = candidate.get("category")
        reasoning = (
            f"Diagnoser fallback after {category} failure. "
            "Diagnoser subprocess returned no valid recommendation "
            "(timeout, non-zero exit, or malformed JSON). Escalating "
            "to human review per spec §8 Budget & safety."
        )
        self._consume_action_escalate(
            agent_id=agent_id,
            issue_number=issue_number,
            reasoning=reasoning,
        )

    def _gh_issue_comment(self, issue_number: int, body: str) -> None:
        """Post a comment on the given issue via ``gh issue comment``.

        Writes the body to a temp file first (CLAUDE.md preflight
        blocks heredocs; shelling out with ``--body`` inline has quoting
        pitfalls for markdown comments). Subprocess failures are logged
        and swallowed — the diagnoser's decision already landed in the
        DB, a missing comment is a visibility regression but not a
        correctness regression.
        """
        tmp_file = self._write_gh_tmp_body(body, prefix="diagnoser-comment")
        if tmp_file is None:
            return
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--body-file",
                    str(tmp_file),
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.diagnoser_gh_comment_failed",
                extra={
                    "event": "diagnoser_gh_comment_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.diagnoser_gh_comment_nonzero",
                extra={
                    "event": "diagnoser_gh_comment_nonzero",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )

    def _gh_issue_set_body(self, issue_number: int, new_body: str) -> None:
        """Replace the issue body via ``gh issue edit --body-file``."""
        tmp_file = self._write_gh_tmp_body(new_body, prefix="diagnoser-body")
        if tmp_file is None:
            return
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--body-file",
                    str(tmp_file),
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.diagnoser_gh_body_failed",
                extra={
                    "event": "diagnoser_gh_body_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.diagnoser_gh_body_nonzero",
                extra={
                    "event": "diagnoser_gh_body_nonzero",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )

    def _gh_issue_add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Add one or more labels via ``gh issue edit --add-label``.

        ``gh`` accepts a comma-separated label list. Idempotent — adding
        a label that already exists is a no-op.
        """
        if not labels:
            return
        label_csv = ",".join(labels)
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--add-label",
                    label_csv,
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.diagnoser_gh_label_failed",
                extra={
                    "event": "diagnoser_gh_label_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.diagnoser_gh_label_nonzero",
                extra={
                    "event": "diagnoser_gh_label_nonzero",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )

    def _gh_issue_remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Remove one or more labels via ``gh issue edit --remove-label``.

        Mirror of :meth:`_gh_issue_add_labels`. Idempotent — removing a
        label that is already absent is a no-op. Used by the claim
        interlock (#2866) to drop the ``status/in-progress`` label on
        agent terminal so the issue becomes re-claimable (for retries /
        follow-ups) and operators see it is no longer active.
        """
        if not labels:
            return
        label_csv = ",".join(labels)
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--remove-label",
                    label_csv,
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.label_remove_failed",
                extra={
                    "event": "label_remove_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.label_remove_nonzero",
                extra={
                    "event": "label_remove_nonzero",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )

    def _gh_issue_close(self, issue_number: int, *, comment: str, reason: str) -> None:
        """Close the issue via ``gh issue close`` with a close comment."""
        tmp_file = self._write_gh_tmp_body(comment, prefix="diagnoser-close")
        if tmp_file is None:
            return
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "close",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--reason",
                    reason,
                    "--comment",
                    comment,
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.diagnoser_gh_close_failed",
                extra={
                    "event": "diagnoser_gh_close_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.diagnoser_gh_close_nonzero",
                extra={
                    "event": "diagnoser_gh_close_nonzero",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )

    def _write_gh_tmp_body(self, body: str, *, prefix: str) -> Path | None:
        """Write ``body`` to a temp file under the repo root's ``tmp/``.

        Returns the Path on success or None on failure. Using the repo
        root's tmp/ (rather than /tmp/ or each worktree's tmp/) keeps
        diagnoser-generated comments inspectable after the worktree is
        cleaned up.
        """
        try:
            tmp_dir = self._repo_root() / "tmp" / "dispatcher-diagnoser"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            # Use monotonic time + pid to keep the filename unique across
            # concurrent diagnoser calls in the same tick.
            name = f"{prefix}-{int(time.time() * 1000)}-{os.getpid()}.txt"
            tmp_file = tmp_dir / name
            tmp_file.write_text(body, encoding="utf-8")
            return tmp_file
        except Exception:
            self._log.exception(
                "daemon.diagnoser_tmp_body_write_failed",
                extra={
                    "event": "diagnoser_tmp_body_write_failed",
                    "run_id": self._run_id,
                    "prefix": prefix,
                },
            )
            return None

    def _gh_issue_create(
        self, *, title: str, body: str, labels: list[str] | None
    ) -> int | None:
        """Create a new GitHub issue via ``gh issue create``.

        Returns the integer issue number of the created issue, or None
        on any failure (logged). Added by issue #3032 for the
        ``file_prerequisite_task`` diagnoser action: the daemon needs
        to synthesize a tracking issue from LLM-provided title + body.
        """
        tmp_file = self._write_gh_tmp_body(body, prefix="diagnoser-prereq-body")
        if tmp_file is None:
            return None
        cmd = [
            "gh",
            "issue",
            "create",
            "--repo",
            self._cfg.github_repo,
            "--title",
            title,
            "--body-file",
            str(tmp_file),
        ]
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.diagnoser_gh_create_failed",
                extra={
                    "event": "diagnoser_gh_create_failed",
                    "run_id": self._run_id,
                    "title": title,
                    "detail": str(exc),
                },
            )
            return None
        if result.returncode != 0:
            self._log.warning(
                "daemon.diagnoser_gh_create_nonzero",
                extra={
                    "event": "diagnoser_gh_create_nonzero",
                    "run_id": self._run_id,
                    "title": title,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return None
        # gh issue create prints the URL on stdout: last non-empty line.
        stdout = (result.stdout or "").strip()
        if not stdout:
            return None
        last_line = stdout.splitlines()[-1].strip()
        import re  # noqa: PLC0415 — local import matches _parse_pr_number pattern

        match = re.search(r"/issues/(\d+)", last_line)
        if not match:
            self._log.warning(
                "daemon.diagnoser_gh_create_parse_failed",
                extra={
                    "event": "diagnoser_gh_create_parse_failed",
                    "run_id": self._run_id,
                    "title": title,
                    "stdout_tail": last_line[-200:],
                },
            )
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):  # pragma: no cover — regex already gated
            return None

    def _gh_issue_append_body(self, issue_number: int, addition: str) -> None:
        """Append ``addition`` to the issue's body via gh.

        Reads the current body via ``gh issue view --json body``,
        concatenates, then writes back via :meth:`_gh_issue_set_body`.
        Best-effort — a read or write failure is logged and swallowed
        so the diagnoser's recommendation still lands.
        """
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--json",
                    "body",
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "daemon.diagnoser_gh_append_failed",
                extra={
                    "event": "diagnoser_gh_append_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "detail": str(exc),
                },
            )
            return
        if result.returncode != 0:
            self._log.warning(
                "daemon.diagnoser_gh_append_nonzero",
                extra={
                    "event": "diagnoser_gh_append_nonzero",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "exit_code": result.returncode,
                    "stderr_tail": _stderr_tail(result.stderr),
                },
            )
            return
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            self._log.warning(
                "daemon.diagnoser_gh_append_parse_failed",
                extra={
                    "event": "diagnoser_gh_append_parse_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                },
            )
            return
        current_body = str(payload.get("body") or "")
        new_body = current_body + addition
        self._gh_issue_set_body(issue_number, new_body)

    def _gh_issue_is_open(self, issue_number: int) -> bool:
        """Return True if the issue exists and is in ``state=OPEN``.

        Used by :meth:`_consume_action_block_on_existing_task` to
        validate the diagnoser-proposed blocker before wiring up the
        dependency. Any read failure (not-found, transient gh error)
        returns False — the caller will fall back to escalate.
        """
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    self._cfg.github_repo,
                    "--json",
                    "state",
                ],
                capture_output=True,
                text=True,
                timeout=GH_POLL_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False
        # gh reports state as "OPEN" / "CLOSED".
        state = str(payload.get("state") or "").upper()
        return state == "OPEN"

    def _run_diagnoser_pass(self) -> int:
        """Find tier-2/3 candidates, spawn diagnosers, consume recommendations.

        Returns the number of diagnoses that ran this tick (regardless
        of action). Called from the supervisor tick. Gated on
        ``dispatcher.config.diagnoser_enabled = true`` at the top —
        a tripped circuit breaker short-circuits the whole pass. Each
        candidate is handled independently so a bad subprocess crash
        on one cannot stall the others.
        """
        if not self._diagnoser_enabled():
            return 0

        candidates = self._find_diagnoser_candidates()
        if not candidates:
            return 0

        ran = 0
        for candidate in candidates:
            try:
                context = self._build_diagnoser_context(candidate)
                diagnosis_id = self._insert_pending_diagnosis(
                    failure_id=candidate["failure_id"],
                    agent_id=candidate["agent_id"],
                    context=context,
                )
                if diagnosis_id is None:
                    # Could not create the diagnosis row — fall back
                    # directly without spawning.
                    self._apply_mechanical_escalation(candidate)
                    continue

                self._log.info(
                    "daemon.diagnoser_spawn",
                    extra={
                        "event": "diagnoser_spawn",
                        "run_id": self._run_id,
                        "diagnosis_id": diagnosis_id,
                        "failure_id": candidate["failure_id"],
                        "agent_id": candidate["agent_id"],
                        "category": candidate["category"],
                        "tier": candidate["tier"],
                    },
                )

                exit_code, stderr_tail = self._spawn_diagnoser_subprocess(diagnosis_id)
                ran += 1

                if exit_code is None:
                    # Timeout or subprocess could not be launched.
                    self._mark_diagnosis_failed(
                        diagnosis_id,
                        reason="subprocess_timeout_or_launch_failure",
                    )
                    self._apply_mechanical_escalation(candidate)
                    continue
                if exit_code != 0:
                    self._log.warning(
                        "daemon.diagnoser_nonzero_exit",
                        extra={
                            "event": "diagnoser_nonzero_exit",
                            "run_id": self._run_id,
                            "diagnosis_id": diagnosis_id,
                            "exit_code": exit_code,
                            "stderr_tail": stderr_tail,
                        },
                    )
                    self._mark_diagnosis_failed(
                        diagnosis_id, reason="subprocess_nonzero_exit"
                    )
                    self._apply_mechanical_escalation(candidate)
                    continue

                # Exit 0 — read the recommendation and consume it.
                self._consume_diagnosis(diagnosis_id, candidate)
            except Exception:
                self._log.exception(
                    "daemon.diagnoser_pass_iteration_failed",
                    extra={
                        "event": "diagnoser_pass_iteration_failed",
                        "run_id": self._run_id,
                        "failure_id": candidate.get("failure_id"),
                        "agent_id": candidate.get("agent_id"),
                    },
                )
                # Best-effort fallback — don't let one bad candidate
                # block later ones.
                try:
                    self._apply_mechanical_escalation(candidate)
                except Exception:
                    self._log.exception(
                        "daemon.diagnoser_pass_fallback_failed",
                        extra={
                            "event": "diagnoser_pass_fallback_failed",
                            "run_id": self._run_id,
                        },
                    )

        return ran

    # ── supervisor tick (every ``tick_supervisor_seconds``) ─────────────

    def supervisor_tick(self) -> dict[str, int]:
        """Run one supervisor tick. Writes heartbeat; advances running agents.

        Steps (in order):
            1. UPDATE ``dispatcher.runs.heartbeat_ts`` — keeps the lease
               alive and signals to the ``HeartbeatAge`` CloudWatch
               alarm that the daemon is healthy.
            2. Count ``dispatcher.failures`` rows in the last hour — the
               read doubles as a connection smoke-test.
            3. **Phase 3C (#2791):** run the failure-detection +
               retry-marker passes. Each pass is independent + wrapped
               in try/except so one bad scan cannot stall siblings.
               Stuck-timeout detection writes ``stuck_timeout`` failure
               rows + enqueues retry markers; the GitHub rate-limit
               guard sets ``self._gh_rate_skip_until`` when the budget
               is low.
            4. **Phase 3B (#2787):** call ``_advance_running_agents``.
               Iterates agents in ``awaiting_ci``/``awaiting_deploy``
               and drives each one forward by one state-machine step.
               Errors are caught per-agent so one bad row cannot stall
               siblings or crash the tick. Skipped when the rate-limit
               flag is set — every advance does a ``gh pr view``.
            5. **Phase 3C (#2791):** drain due retry markers — reset the
               corresponding agent back to ``claiming`` so the next
               scheduler tick re-orchestrates with a fresh worktree.
            6. Emit the ``HeartbeatAge`` CloudWatch metric.

        Returns a summary dict for logs + tests.
        """
        assert self._conn is not None, "connect() must run before ticks"
        assert self._run_id is not None, "register the run before ticking"

        failures_last_hour = 0

        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE dispatcher.runs SET heartbeat_ts = now() WHERE run_id = %s",
                (self._run_id,),
            )
            # Smoke-test read — counts are surfaced via the GraphQL
            # ``DispatcherState.recentFailures`` query separately, but
            # this read also exercises the connection on every tick so
            # the daemon notices DB loss quickly.
            cur.execute(
                "SELECT count(*) FROM dispatcher.failures "
                "WHERE ts > now() - interval '1 hour'",
            )
            row = cur.fetchone()
            if row is not None:
                failures_last_hour = int(row[0] or 0)
        self._conn.commit()

        # Phase 3C (#2791): stuck-timeout scan. Runs first so any newly
        # crashed agents land retry markers early in the tick, giving
        # _process_retry_markers a chance to re-arm them later in the
        # same tick if their backoff is already zero (not the common
        # case — all tier-1 backoffs are ≥60s — but it keeps the code
        # robust to future schedule edits).
        stuck_flagged = 0
        try:
            stuck_flagged = self._check_stuck_agents()
        except Exception:
            self._log.exception(
                "daemon.stuck_check_failed",
                extra={
                    "event": "stuck_check_failed",
                    "run_id": self._run_id,
                },
            )

        # Phase 3C (#2791): rate-limit guard. Write the failure row +
        # set the skip flag before the 3B advance pass so this tick
        # already respects the flag.
        try:
            self._check_gh_rate_limit()
        except Exception:
            self._log.exception(
                "daemon.gh_rate_check_failed",
                extra={
                    "event": "gh_rate_check_failed",
                    "run_id": self._run_id,
                },
            )

        rate_skip_active = self._gh_rate_skip_active()

        # Phase 3B (#2787): advance agents that are past push_and_pr.
        # Wrapped in try/except — the heartbeat + metric emission below
        # must still run even if the advance pass throws unexpectedly.
        # Skipped when the rate-limit flag is set (every advance step
        # calls ``gh pr view`` or ``gh run list`` which would burn the
        # remaining budget and delay the reset).
        agents_advanced = 0
        if rate_skip_active:
            self._log.info(
                "daemon.advance_skipped_rate_limited",
                extra={
                    "event": "advance_skipped_rate_limited",
                    "run_id": self._run_id,
                    "skip_until": self._gh_rate_skip_until.isoformat()
                    if self._gh_rate_skip_until is not None
                    else None,
                },
            )
        else:
            try:
                agents_advanced = self._advance_running_agents()
            except Exception:
                self._log.exception(
                    "daemon.advance_pass_failed",
                    extra={
                        "event": "advance_pass_failed",
                        "run_id": self._run_id,
                    },
                )

        # Phase 3C (#2791): drain due retry markers. Runs AFTER the
        # advance pass so a marker created earlier in this tick (via
        # _check_stuck_agents) can still be caught on the NEXT tick —
        # the backoff interval (≥60s) always keeps the processor from
        # firing on a marker created in the same tick.
        retry_processed = 0
        try:
            retry_processed = self._process_retry_markers()
        except Exception:
            self._log.exception(
                "daemon.retry_process_pass_failed",
                extra={
                    "event": "retry_process_pass_failed",
                    "run_id": self._run_id,
                },
            )

        # Phase 3D (#2795): circuit breaker + diagnoser pass. The
        # breaker check runs FIRST so a bad 24h window disables the
        # diagnoser before the pass tries to spawn more subprocesses.
        # The pass is gated on ``dispatcher.config.diagnoser_enabled``
        # inside :meth:`_run_diagnoser_pass`; rate-limit-skipped ticks
        # still run the pass because diagnoses can proceed without
        # calling ``gh`` when the issue + PR context is available via
        # DB state alone. (The skill itself makes GH reads; the daemon
        # side only writes comments/labels if the recommendation
        # action requires them, and those are unavoidable regardless
        # of budget.)
        try:
            self._check_diagnoser_circuit_breaker()
        except Exception:
            self._log.exception(
                "daemon.diagnoser_circuit_breaker_check_failed",
                extra={
                    "event": "diagnoser_circuit_breaker_check_failed",
                    "run_id": self._run_id,
                },
            )
        diagnoses_ran = 0
        try:
            diagnoses_ran = self._run_diagnoser_pass()
        except Exception:
            self._log.exception(
                "daemon.diagnoser_pass_failed",
                extra={
                    "event": "diagnoser_pass_failed",
                    "run_id": self._run_id,
                },
            )

        self._supervisor_ticks += 1
        self._last_heartbeat_at = datetime.now(UTC)

        # Emit the ``HeartbeatAge`` CloudWatch metric. Emit AFTER the DB
        # heartbeat update so a successful metric means the DB round-trip
        # also just succeeded — "age=0" is the ground-truth freshness
        # indicator for the alarm in
        # ``infra/terraform/modules/dispatcher-daemon/main.tf``.
        metric_emitted = self._emit_heartbeat_metric()

        self._log.info(
            "daemon.supervisor_tick",
            extra={
                "event": "supervisor_tick",
                "run_id": self._run_id,
                "tick_n": self._supervisor_ticks,
                "failures_last_hour": failures_last_hour,
                "heartbeat_metric_emitted": metric_emitted,
                "agents_advanced": agents_advanced,
                "stuck_flagged": stuck_flagged,
                "retry_markers_processed": retry_processed,
                "rate_skip_active": rate_skip_active,
                "diagnoses_ran": diagnoses_ran,
            },
        )
        return {
            "failures_last_hour": failures_last_hour,
            "heartbeat_metric_emitted": 1 if metric_emitted else 0,
            "agents_advanced": agents_advanced,
            "stuck_flagged": stuck_flagged,
            "retry_markers_processed": retry_processed,
            "rate_skip_active": 1 if rate_skip_active else 0,
            "diagnoses_ran": diagnoses_ran,
        }

    # ── heartbeat metric emission (supervisor-tick step 3) ──────────────

    def _make_cloudwatch_client(self) -> Any:
        """Build a boto3 CloudWatch client. Isolated so tests can mock it.

        Lazy import of boto3 mirrors psycopg — tests that do not exercise
        the heartbeat path should not have to install boto3.
        """
        import boto3  # noqa: PLC0415  — lazy import

        return boto3.client("cloudwatch", region_name=self._cfg.aws_region)

    def _emit_heartbeat_metric(self) -> bool:
        """Publish ``HeartbeatAge=0`` to the configured CloudWatch namespace.

        Returns True on success, False on failure. Failures log but do
        NOT raise — a missing metric point triggers the existing alarm
        after 5 minutes of staleness, which is the correct behaviour
        (the daemon either recovers or the alarm fires).

        Value is always ``0`` seconds: the metric is emitted immediately
        after the DB heartbeat update, so the freshest reading is by
        definition "0 seconds stale". The alarm in terraform compares
        the metric's ``Maximum`` over a 1-minute period against a 300s
        threshold; emitting 0 on every supervisor tick (every 120s by
        default) means a healthy daemon keeps the alarm in OK.
        """
        # Service dimension: the terraform alarm filters on
        # ``Service = judgemind-dispatcher-<env>``, so we must set the
        # same dimension here. Fall back to the hostname when the env
        # var is unset (test / local-dev case).
        service_dim = self._cfg.dispatcher_service_name or self._cfg.host

        try:
            if self._cloudwatch_client is None:
                self._cloudwatch_client = self._make_cloudwatch_client()
            self._cloudwatch_client.put_metric_data(
                Namespace=self._cfg.heartbeat_metric_namespace,
                MetricData=[
                    {
                        "MetricName": "HeartbeatAge",
                        "Dimensions": [
                            {"Name": "Service", "Value": service_dim},
                        ],
                        "Value": 0.0,
                        "Unit": "Seconds",
                    }
                ],
            )
        except Exception as exc:
            # Reset the client so the next tick re-creates it — e.g. a
            # recoverable credential refresh or endpoint outage should
            # not poison the whole daemon lifetime.
            self._cloudwatch_client = None
            self._log.warning(
                "daemon.heartbeat_metric_failed",
                extra={
                    "event": "heartbeat_metric_failed",
                    "run_id": self._run_id,
                    "namespace": self._cfg.heartbeat_metric_namespace,
                    "detail": str(exc),
                },
            )
            return False

        self._log.info(
            "daemon.heartbeat_metric_emitted",
            extra={
                "event": "heartbeat_metric_emitted",
                "run_id": self._run_id,
                "namespace": self._cfg.heartbeat_metric_namespace,
                "service": service_dim,
            },
        )
        return True

    # ── housekeeping tick (every ``tick_housekeeping_seconds``) ─────────

    #: Retention targets the housekeeping tick prunes. Each entry is a
    #: ``(table, timestamp_column, config_key, default_days)`` tuple.
    #: - ``table``: short name under the ``dispatcher.`` schema.
    #: - ``timestamp_column``: the column the cutoff compares against
    #:   (``now() - INTERVAL 'N days'``).
    #: - ``config_key``: row in ``dispatcher.config`` that overrides the
    #:   default retention window. Missing or invalid value = fall back
    #:   to ``default_days``.
    #: - ``default_days``: hardcoded floor used when no config row is set.
    #:
    #: Table names and column names are hardcoded class constants (never
    #: interpolated from user input or config), so composing the DELETE
    #: with an f-string is safe from SQL injection here. Cutoff days are
    #: parameterized normally via psycopg's ``%s`` placeholder.
    #:
    #: Extending: issue #2779 added ``phase_outputs`` (ts column) and
    #: ``notifications`` (created_at column). Both default to 30 days,
    #: overridable via ``phase_output_retention_days`` and
    #: ``notification_retention_days`` rows in ``dispatcher.config``.
    _HOUSEKEEPING_TARGETS: tuple[tuple[str, str, str, int], ...] = (
        (
            "queue_snapshots",
            "observed_at",
            "queue_snapshot_retention_days",
            DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS,
        ),
        (
            # Same retention as queue_snapshots — append-only history
            # with the same "latest row wins" admin-page read pattern.
            # Issue #2820. Reuses the queue_snapshot_retention_days
            # config key deliberately so operators only tune one knob.
            "blocked_snapshots",
            "observed_at",
            "queue_snapshot_retention_days",
            DEFAULT_QUEUE_SNAPSHOT_RETENTION_DAYS,
        ),
        (
            "phase_outputs",
            "ts",
            "phase_output_retention_days",
            DEFAULT_PHASE_OUTPUT_RETENTION_DAYS,
        ),
        (
            "notifications",
            "created_at",
            "notification_retention_days",
            DEFAULT_NOTIFICATION_RETENTION_DAYS,
        ),
        (
            # Safety-net only — happy-path cleanup is the DELETE inside
            # ``_push_and_open_pr`` after a successful ``gh pr create``.
            # The 7-day TTL catches pathological cases (daemon crash
            # between SHIP and PR create, operator force-stop) without
            # keeping large patch blobs indefinitely. Issue #3012.
            "ralph_patches",
            "created_at",
            "ralph_patch_retention_days",
            DEFAULT_RALPH_PATCH_RETENTION_DAYS,
        ),
    )

    def _read_retention_days(self, config_key: str, default_days: int) -> int:
        """Look up the retention override for ``config_key`` in ``dispatcher.config``.

        Returns the configured value if present and castable to a
        positive int, otherwise ``default_days``. The lookup runs in its
        own tiny transaction so a missing/malformed row does not poison
        the surrounding DELETE.
        """
        assert self._conn is not None, "connect() must run before reading config"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    (config_key,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            # Config read failure is non-fatal — the caller will fall
            # back to the hardcoded default. Rollback is best-effort.
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover — best-effort
                pass
            return default_days

        if row is None:
            return default_days
        value = row[0]
        if value is None:
            return default_days
        try:
            configured = int(value)
        except (TypeError, ValueError):
            return default_days
        if configured <= 0:
            return default_days
        return configured

    def _housekeeping_tick(self) -> dict[str, int]:
        """Run one housekeeping tick. Prunes stale rows from dispatcher tables.

        Iterates :attr:`_HOUSEKEEPING_TARGETS` and issues a DELETE per
        target bounded by the target's retention window. Each target is
        a separate transaction so one failure does not poison siblings.

        Returns a mapping of ``table`` → ``rows_deleted`` (with ``-1``
        sentinel on per-target failure). The return value is surfaced to
        tests — the daemon itself just logs and continues.
        """
        assert self._conn is not None, "connect() must run before ticks"

        per_table: dict[str, int] = {}
        for (
            table,
            timestamp_column,
            config_key,
            default_days,
        ) in self._HOUSEKEEPING_TARGETS:
            cutoff_days = self._read_retention_days(config_key, default_days)
            # ``table`` and ``timestamp_column`` are class constants
            # (never user-controlled), so composing the SQL with f-string
            # is safe. The cutoff is bound via %s.
            delete_sql = (
                f"DELETE FROM dispatcher.{table} "
                f"WHERE {timestamp_column} < now() - make_interval(days => %s)"
            )
            try:
                with self._conn.cursor() as cur:
                    cur.execute(delete_sql, (cutoff_days,))
                    rows_deleted = cur.rowcount or 0
                self._conn.commit()
            except Exception as exc:
                # DB hiccup on one table must not crash the daemon or
                # starve sibling tables of their housekeeping. Log, roll
                # back, record the sentinel, and keep going.
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover — best-effort
                    pass
                self._log.warning(
                    "daemon.housekeeping_failed",
                    extra={
                        "event": "housekeeping_failed",
                        "run_id": self._run_id,
                        "table": table,
                        "cutoff_days": cutoff_days,
                        "detail": str(exc),
                    },
                )
                per_table[table] = -1
                continue

            per_table[table] = rows_deleted
            self._log.info(
                "daemon.housekeeping_tick",
                extra={
                    "event": "housekeeping_tick",
                    "run_id": self._run_id,
                    "table": table,
                    "rows_deleted": rows_deleted,
                    "cutoff_days": cutoff_days,
                },
            )

        self._housekeeping_ticks += 1
        return per_table

    # ── signal handling ────────────────────────────────────────────────

    def request_stop(self, signum: int | None = None, _frame: Any = None) -> None:
        """SIGTERM / SIGINT handler — sets the stop event; the run loop checks it."""
        if signum is not None:
            self._log.info(
                "daemon.signal_received",
                extra={"event": "signal_received", "signum": int(signum)},
            )
        self._stop.set()

    # ── run loop ───────────────────────────────────────────────────────

    def run_forever(self) -> int:
        """Block until SIGTERM/SIGINT; run scheduler + supervisor on their cadences.

        Returns an exit code (0 on clean shutdown).
        """
        assert self._conn is not None, "connect() must run before run_forever"
        assert self._run_id is not None, "register the run before run_forever"

        last_scheduler = 0.0
        last_supervisor = 0.0
        last_housekeeping = 0.0
        # Tick once on boot so the first scheduler/supervisor cycle is
        # observable immediately in logs + DB, rather than after the
        # first full cadence elapses. Housekeeping is NOT fired on boot
        # — it would double-emit on every deploy (rolling restart) and
        # hourly cadence already bounds the worst-case staleness at 1h.
        try:
            self.scheduler_tick()
            last_scheduler = time.monotonic()
            self.supervisor_tick()
            last_supervisor = time.monotonic()
            # Seed the housekeeping timer so the first prune happens
            # after a full interval, not immediately.
            last_housekeeping = time.monotonic()
        except Exception:
            self._log.exception("daemon.initial_tick_failed")
            return 1

        # Poll the stop flag in 1-second slices so SIGTERM lands promptly.
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now - last_scheduler >= self._cfg.tick_scheduler_seconds:
                    self.scheduler_tick()
                    last_scheduler = now
                if now - last_supervisor >= self._cfg.tick_supervisor_seconds:
                    self.supervisor_tick()
                    last_supervisor = now
                if now - last_housekeeping >= self._cfg.tick_housekeeping_seconds:
                    self._housekeeping_tick()
                    last_housekeeping = now
            except Exception:
                # Any tick exception is logged and the loop continues —
                # the next tick will try again. Real DB failure will
                # repeat and show up in the logs; the ECS task does not
                # crash-restart for a single blip.
                self._log.exception("daemon.tick_failed")
                # Back off briefly before retrying to avoid a tight loop
                # on a persistent DB outage.
                self._stop.wait(1.0)
                continue
            # Sleep at most 1s at a time so shutdown is snappy.
            self._stop.wait(1.0)

        self._log.info("daemon.shutdown_begin", extra={"event": "shutdown_begin"})
        # #2847: signal the orchestration worker thread (if any) to
        # abort at its next phase boundary, then give it a short
        # window to exit cleanly. The thread is daemon=True so a
        # hung ``claude -p`` subprocess will not block process exit
        # past this timeout — the SIGTERM → exit path will still
        # complete even if the subprocess is unresponsive.
        self._pause_requested.set()
        thread = self._orchestration_thread
        if thread is not None and thread.is_alive():
            self._log.info(
                "daemon.orchestration_join_wait",
                extra={
                    "event": "orchestration_join_wait",
                    "run_id": self._run_id,
                    "thread_name": thread.name,
                    "timeout_seconds": ORCHESTRATION_JOIN_TIMEOUT_SECONDS,
                },
            )
            thread.join(timeout=ORCHESTRATION_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                self._log.warning(
                    "daemon.orchestration_join_timeout",
                    extra={
                        "event": "orchestration_join_timeout",
                        "run_id": self._run_id,
                        "thread_name": thread.name,
                    },
                )
        self.mark_stopped()
        return 0


# --------------------------------------------------------------------------
# Argparse + entrypoint
# --------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.dispatcher.daemon",
        description=(
            "Dispatcher v2 daemon — Phase 2 shadow mode (queue scan + "
            "heartbeat metric; no subprocess spawn)."
        ),
    )
    parser.add_argument(
        "--tick-scheduler-seconds",
        type=int,
        default=DEFAULT_SCHEDULER_TICK_SECONDS,
        help="Seconds between scheduler ticks (default: 30).",
    )
    parser.add_argument(
        "--tick-supervisor-seconds",
        type=int,
        default=DEFAULT_SUPERVISOR_TICK_SECONDS,
        help="Seconds between supervisor ticks (default: 120).",
    )
    parser.add_argument(
        "--tick-housekeeping-seconds",
        type=int,
        default=DEFAULT_HOUSEKEEPING_TICK_SECONDS,
        help="Seconds between housekeeping ticks (default: 3600).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO).",
    )
    parser.add_argument(
        "--config-override",
        default=None,
        help=(
            "JSON object merged into DaemonConfig.config_override for testing; "
            "unused at runtime by Phase 1 skeleton."
        ),
    )
    return parser.parse_args(argv)


def _build_config(
    args: argparse.Namespace, env: dict[str, str] | None = None
) -> DaemonConfig:
    """Build a :class:`DaemonConfig` from argparse + environment variables."""
    env = env if env is not None else dict(os.environ)
    database_url = env.get("DATABASE_URL", "")
    version_sha = env.get("GIT_SHA", "unknown")
    host = env.get("HOSTNAME") or socket.gethostname() or "unknown-host"
    github_repo = env.get("GITHUB_REPO") or DEFAULT_GITHUB_REPO
    dispatcher_service_name = env.get("DISPATCHER_SERVICE_NAME", "")
    heartbeat_namespace = (
        env.get("HEARTBEAT_METRIC_NAMESPACE") or DEFAULT_HEARTBEAT_METRIC_NAMESPACE
    )
    aws_region = (
        env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or DEFAULT_AWS_REGION
    )
    # Baseline clone path is opt-in via env var. The Dockerfile sets it
    # to :data:`DEFAULT_BASELINE_REPO_ROOT` so Fargate always runs in
    # baseline-clone mode; local dev and unit tests leave it unset and
    # the daemon falls back to ``os.getcwd()`` (preserves pre-#2804
    # behavior). An empty-string value is treated the same as unset so
    # operators can disable baseline-clone mode without touching the
    # task definition.
    raw_baseline = env.get("DISPATCHER_BASELINE_REPO_ROOT", "").strip()
    baseline_repo_root = Path(raw_baseline) if raw_baseline else None

    override: dict[str, Any] = {}
    if args.config_override:
        try:
            parsed = json.loads(args.config_override)
            if isinstance(parsed, dict):
                override = parsed
        except (json.JSONDecodeError, ValueError):
            # Intentionally swallow — bad --config-override is a test-path nit,
            # not a startup failure. Logged by caller once the logger is up.
            override = {"__parse_error__": args.config_override}

    return DaemonConfig(
        database_url=database_url,
        tick_scheduler_seconds=args.tick_scheduler_seconds,
        tick_supervisor_seconds=args.tick_supervisor_seconds,
        tick_housekeeping_seconds=args.tick_housekeeping_seconds,
        log_level=args.log_level,
        version_sha=version_sha,
        host=host,
        pid=os.getpid(),
        github_repo=github_repo,
        dispatcher_service_name=dispatcher_service_name,
        heartbeat_metric_namespace=heartbeat_namespace,
        aws_region=aws_region,
        baseline_repo_root=baseline_repo_root,
        config_override=override,
    )


def _install_signal_handlers(daemon: DispatcherDaemon) -> None:
    """Wire SIGTERM + SIGINT to :meth:`DispatcherDaemon.request_stop`."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, daemon.request_stop)
        except (ValueError, OSError):  # pragma: no cover — non-main thread in tests
            # Tests that drive the daemon off the main thread (e.g. to
            # simulate SIGTERM via request_stop() directly) can't install
            # signal handlers — that's fine, call the handler directly.
            continue


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns an exit code."""
    try:
        args = _parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    cfg = _build_config(args)
    log = _configure_logging(cfg.log_level)

    if not cfg.database_url:
        log.error(
            "daemon.startup_failed",
            extra={"event": "startup_failed", "reason": "DATABASE_URL not set"},
        )
        return 1

    if "__parse_error__" in cfg.config_override:
        log.warning(
            "daemon.config_override_parse_error",
            extra={
                "event": "config_override_parse_error",
                "raw": cfg.config_override["__parse_error__"],
            },
        )

    log.info(
        "daemon.startup",
        extra={
            "event": "startup",
            "version_sha": cfg.version_sha,
            "host": cfg.host,
            "pid": cfg.pid,
            "tick_scheduler_seconds": cfg.tick_scheduler_seconds,
            "tick_supervisor_seconds": cfg.tick_supervisor_seconds,
            "tick_housekeeping_seconds": cfg.tick_housekeeping_seconds,
        },
    )

    daemon = DispatcherDaemon(cfg, log)
    _install_signal_handlers(daemon)

    try:
        daemon.connect()
    except Exception as exc:
        log.exception("daemon.connect_failed", extra={"event": "connect_failed"})
        # Ensure we return 1 without swallowing the traceback in logs.
        del exc
        return 1

    try:
        daemon.check_lease_and_register_run()
    except LeaseError as exc:
        log.error(
            "daemon.lease_held",
            extra={"event": "lease_held", "detail": str(exc)},
        )
        daemon.close()
        return 1
    except Exception:
        log.exception("daemon.register_failed", extra={"event": "register_failed"})
        daemon.close()
        return 1

    # Idempotently create labels the diagnoser's escalate path depends
    # on (issue #2872 Bug D). Non-fatal — the diagnoser's DB terminal
    # is authoritative; this just keeps the operator-visible GitHub
    # signal intact.
    try:
        daemon.ensure_required_labels()
    except Exception:
        log.exception(
            "daemon.ensure_required_labels_failed",
            extra={"event": "ensure_required_labels_failed"},
        )

    # Restart-recovery sweep (issue #2872). Reclaim any ``status=
    # 'running'`` agents left behind by a prior daemon run. Marks each
    # ``crashed`` with a restart-abandoned retry marker; the standard
    # retry path then rebuilds the worktree and re-runs the phase
    # pipeline. Failure here is non-fatal — the supervisor's
    # stuck_timeout sweep is the backstop.
    try:
        daemon.recover_abandoned_agents()
    except Exception:
        log.exception(
            "daemon.recover_abandoned_agents_failed",
            extra={"event": "recover_abandoned_agents_failed"},
        )

    # Bootstrap the baseline git clone before the first scheduler tick
    # so ``_create_worktree`` has a ``.git`` parent to run ``git -C ...
    # worktree add`` from (issue #2804). No-op in local-dev / unit-test
    # mode (``DISPATCHER_BASELINE_REPO_ROOT`` unset).
    try:
        daemon.ensure_baseline_clone()
    except Exception:
        log.exception(
            "daemon.baseline_clone_failed",
            extra={"event": "baseline_clone_failed"},
        )
        daemon.mark_stopped()
        daemon.close()
        return 1

    try:
        return daemon.run_forever()
    finally:
        daemon.close()


if __name__ == "__main__":  # pragma: no cover — exercised via `python -m`
    sys.exit(main())
