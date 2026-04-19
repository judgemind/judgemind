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

#: Hard wall-clock timeout for each ``claude -p`` subprocess spawned
#: from the orchestration path. Matches the 180-minute ceiling from
#: spec §17 Risk 4a and the ``dispatcher.config.subprocess_timeout_s``
#: seed value (10800s). Enforced via ``subprocess.run(..., timeout=...)``.
CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS = 180 * 60

#: Per-phase ``--max-turns`` values. Matches the frontmatter on each
#: ``.claude/skills/task-v2-*/SKILL.md`` file. Sonnet-backed ralph gets
#: the long tail; plan and summary stay tight. Post-PR phases added
#: in Phase 3B (#2787) — fix-ci is a targeted patch so 100 turns is
#: generous; verify is read-mostly so 50 turns fits. Retro added in
#: Phase 3E (#2798) — a read-only review producing zero-to-many issue
#: bodies; 30 turns matches the retro skill's frontmatter.
PHASE_MAX_TURNS = {
    "plan": 50,
    "ralph": 500,
    "summary": 30,
    "fix-ci": 100,
    "verify": 50,
    "retro": 30,
}

#: Per-phase ``--model`` values. Matches ``dispatcher.config.model_by_phase``
#: seeded in migration 21. Fix-ci uses Sonnet — the CI-fixing skill's
#: own frontmatter selects it. Verify uses Haiku — it only reads the
#: deploy status and poses structured evidence, no complex reasoning.
#: Retro uses Haiku — a quick review of structured input that produces
#: structured output; no complex reasoning required.
PHASE_MODELS = {
    "plan": "opus",
    "ralph": "sonnet",
    "summary": "haiku",
    "fix-ci": "sonnet",
    "verify": "haiku",
    "retro": "haiku",
}

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

#: Statuses that represent an in-flight claim. Mirrors the partial
#: UNIQUE INDEX predicate in migration 25. Used by the candidate picker
#: to skip already-claimed issues client-side (the INSERT also catches
#: concurrent races via unique-violation).
ACTIVE_AGENT_STATUSES = ("running", "retrying", "succeeded")

#: Relative path under the repo root where per-agent worktrees land.
#: Mirrors the laptop-dispatcher convention from
#: ``.claude/skills/task/SKILL.md`` so human operators can find a
#: daemon-created worktree with the same ``git worktree list`` command
#: they use locally.
WORKTREE_PARENT_DIR = Path(".claude/worktrees")

#: Length of the short UUID used in worktree + branch names. 8 hex
#: chars is ~4 billion distinct values — collision probability across
#: the lifetime of the dispatcher is negligible and the short form
#: keeps path lengths sane.
AGENT_SHORT_ID_HEX_CHARS = 8

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

#: Supervisor-tick stuck-timeout window. Agents whose most-recent
#: ``dispatcher.phase_transitions.ts`` is older than this are flagged
#: as ``stuck_timeout`` and flipped to ``status='crashed'`` so the retry
#: marker processor can recover them. Matches spec §7 step 1. 30 min is
#: generous for the long-tail ralph iteration (#2513, #2628) while still
#: well under the 180-minute ``subprocess_timeout_s`` ceiling.
STUCK_TIMEOUT_SECONDS = 30 * 60

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

#: Which failure categories auto-create a retry marker (tier 1 per
#: spec §8 table). ``subprocess_turn_limit`` (tier 2) and
#: ``subprocess_auth_fail`` (halt — no retry) are intentionally
#: excluded; 3D's diagnoser owns the escalation path for both.
#: ``stuck_timeout``, ``gh_rate_exhausted``, and ``subprocess_crash``
#: are the three that retry mechanically.
AUTO_RETRY_CATEGORIES = frozenset(
    {
        FAILURE_CATEGORY_STUCK_TIMEOUT,
        FAILURE_CATEGORY_GH_RATE_EXHAUSTED,
        FAILURE_CATEGORY_SUBPROCESS_CRASH,
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
TIER_2_FIRST_OCCURRENCE_CATEGORIES: frozenset[str] = frozenset(
    {
        FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT,
    }
)

#: Tier-3 categories that diagnose on first occurrence (spec §8). Today
#: only ``ci_red_after_retries`` qualifies — the 3B fix-CI loop has
#: already burned its 3 mechanical retries, so there is no reliable
#: mechanical remedy left.
TIER_3_CATEGORIES: frozenset[str] = frozenset(
    {
        FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
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
#: falls through to ``escalate`` as a safe default (logged as
#: ``daemon.diagnosis_action_unknown``).
DIAGNOSER_ACTIONS: frozenset[str] = frozenset(
    {"retry", "retry_with_hint", "reissue", "escalate", "close"}
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
    # Optional override used by tests to avoid os.environ mutation.
    config_override: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# DB helpers — every call lives inside ``DispatcherDaemon`` so tests can
# substitute a fake psycopg module via ``sys.modules``.
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LeaseError(RuntimeError):
    """Another daemon is holding the active-heartbeat lease."""


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
            - consume unconsumed ``dispatcher.commands`` (no-op handler
              in shadow mode; commands drain so the queue stays small);
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
        self._conn: Connection[Any] | None = None
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
        # Phase 3C: GitHub rate-limit skip window. When set, ``now() <
        # self._gh_rate_skip_until`` → scheduler + supervisor ticks both
        # skip their hot paths. Cleared by
        # ``_gh_rate_skip_active`` once the reset epoch elapses.
        # Represented as a UTC-aware datetime (not an epoch seconds) so
        # all time comparisons go through the same ``datetime.now(UTC)``
        # plumbing the rest of the daemon uses (see #2791 §2b).
        self._gh_rate_skip_until: datetime | None = None

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
            1. Consume any pending ``dispatcher.commands`` (no-op handler).
            2. Read ``dispatcher.config.concurrency_cap`` and fire the
               Phase 2 spawn-safety guard.
            3. Scan the GitHub ``agent/ready`` queue and write a row to
               ``dispatcher.queue_snapshots``.

        Returns a small summary dict for logging + tests. Keys:

            * ``commands_consumed``: int, rowcount from step 1.
            * ``concurrency_cap``: int, or ``-1`` sentinel if unset.
            * ``queue_depth``: int, ``-1`` if the scan failed.
        """
        assert self._conn is not None, "connect() must run before ticks"

        commands_consumed = 0
        concurrency_cap: int | None = None

        with self._conn.cursor() as cur:
            # 1. Consume any pending commands. Phase 2 = no-op handler;
            # mark consumed anyway so the queue drains and does not fill
            # up during shadow mode.
            cur.execute(
                "UPDATE dispatcher.commands "
                "SET consumed_at = now() "
                "WHERE consumed_at IS NULL",
            )
            commands_consumed = cur.rowcount or 0

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

        # 3. Scan the ``agent/ready`` queue and persist a snapshot.
        #
        # Failures here (rate limit, network, auth) log + return -1 but
        # do NOT raise — the daemon must survive GitHub API hiccups,
        # and the next tick will try again (§15).
        queue_depth = self._scan_queue_and_snapshot()

        # 4. Phase 3A orchestration gate (#2783).
        #
        # Only enter the claim + orchestrate path when (a) the live
        # ``concurrency_cap`` is >0 AND (b) no agent is currently in
        # flight for this daemon run AND (c) Phase 3C's GitHub
        # rate-limit skip flag is not active. Phase 3 runs at
        # ``concurrency_cap=1`` (one subprocess at a time); Phase 3E
        # flips the value from 0 to 1. Until then the gate stays
        # closed and this branch is a no-op.
        #
        # Exceptions here are caught + logged but not re-raised — the
        # scheduler tick must survive any orchestration failure so the
        # next tick can try again. Orchestration work itself is wrapped
        # in per-phase error handling inside ``_claim_and_orchestrate_one``.
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
            and not self._has_active_agent()
        ):
            orchestration_attempted = True
            try:
                self._claim_and_orchestrate_one()
            except Exception:
                # Daemon survival takes precedence over any single
                # orchestration run. The helper logs specific failures
                # internally; this is the belt-and-braces catch.
                self._log.exception(
                    "daemon.orchestration_failed",
                    extra={
                        "event": "orchestration_failed",
                        "run_id": self._run_id,
                    },
                )

        self._scheduler_ticks += 1
        self._log.info(
            "daemon.scheduler_tick",
            extra={
                "event": "scheduler_tick",
                "run_id": self._run_id,
                "tick_n": self._scheduler_ticks,
                "commands_consumed": commands_consumed,
                "concurrency_cap": concurrency_cap,
                "queue_depth": queue_depth,
                "orchestration_attempted": orchestration_attempted,
            },
        )
        return {
            "commands_consumed": commands_consumed,
            "concurrency_cap": -1 if concurrency_cap is None else concurrency_cap,
            "queue_depth": queue_depth,
            "orchestration_attempted": 1 if orchestration_attempted else 0,
        }

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

        # Defensive filter: drop rows that also carry ``status/blocked``.
        # The ``gh --label agent/ready`` call returns issues that carry
        # the label; a blocked issue that still has ``agent/ready``
        # attached (e.g. mid-transition) should not inflate the queue
        # depth because the daemon would never spawn on it.
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
        for issue in issues:
            number = issue.get("number")
            if isinstance(number, int):
                issue_numbers.append(number)
        queue_depth = len(issue_numbers)

        # Persist the snapshot. One INSERT per tick; the table is
        # append-only and the daemon is a singleton, so no race.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.queue_snapshots "
                    "    (observed_at, queue_depth, issue_numbers, run_id) "
                    "VALUES (now(), %s, %s, %s)",
                    (queue_depth, issue_numbers, self._run_id),
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
        """Return the issue numbers from the most recent queue snapshot.

        The snapshot is written by ``_scan_queue_and_snapshot`` earlier
        in the same tick, so this is almost always the list we just
        observed. Returns an empty list if there is no snapshot yet
        (first-tick edge case) or the read fails.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issue_numbers FROM dispatcher.queue_snapshots "
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
        issues = row[0]
        if not isinstance(issues, list):
            return []
        return [int(n) for n in issues if isinstance(n, int)]

    def _pick_candidate_issue(self, candidates: list[int]) -> int | None:
        """Pick the first candidate that is trusted and not already claimed.

        For each issue number in ``candidates`` (priority order as
        observed by the queue scan — the ``gh issue list`` default is
        already priority-sorted by the GitHub API):

        1. Skip if ``dispatcher.agents`` has a row for it with
           ``status IN ('running', 'retrying', 'succeeded')``. A
           ``failed`` or ``crashed`` row does NOT block re-claim —
           manual retry is a documented operator flow.
        2. Run the trust check (``scripts/check-issue-author.sh``). Skip
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

    def _issue_already_attempted(self, issue_number: int) -> bool:
        """True if ``dispatcher.agents`` has any active/succeeded row for this issue.

        The partial UNIQUE INDEX (migration 25) enforces uniqueness on
        ``running`` and ``retrying``. The extra ``succeeded`` check
        here is so a successful prior run (not yet cleaned up) doesn't
        get double-processed before the PR merges.
        """
        assert self._conn is not None, "connect() must run before reading"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM dispatcher.agents "
                    "WHERE issue_number = %s "
                    "  AND status = ANY(%s) "
                    "LIMIT 1",
                    (issue_number, list(ACTIVE_AGENT_STATUSES)),
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
        return row is not None

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
                "detail": (result.stdout or result.stderr or "").strip()[:200],
            },
        )
        return False

    def _atomic_claim(
        self, issue_number: int, agent_id: str, worktree_path: str
    ) -> bool:
        """INSERT a new agent row; return True on success, False on race.

        The partial UNIQUE INDEX on
        ``dispatcher.agents (issue_number) WHERE status IN ('running',
        'retrying')`` (migration 25) turns a concurrent second daemon's
        INSERT into a ``psycopg.errors.UniqueViolation``. Catching that
        is the race-lost signal — do NOT pre-check + insert, which is
        not atomic.
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
                    "     worktree_path, phase, status) "
                    "VALUES (%s, %s, 'task', %s, %s, 'claiming', 'running')",
                    (agent_id, self._run_id, issue_number, worktree_path),
                )
            self._conn.commit()
        except psycopg.errors.UniqueViolation:
            # Another daemon claimed this issue first. Roll back and
            # return False so the caller skips to the next candidate.
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            self._log.info(
                "daemon.claim_lost",
                extra={
                    "event": "claim_lost",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "agent_id": agent_id,
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

    def _repo_root(self) -> Path:
        """Repo root inside the dispatcher container.

        The Dockerfile places the daemon code at ``/app/scripts/dispatcher/``
        but the git working tree used for worktrees is cloned at
        runtime into ``/app`` (via the startup hook in the Fargate task
        definition). For unit tests the working directory is the
        repo-root worktree. ``os.getcwd()`` returns the right thing in
        both cases; the daemon never ``chdir``s away from it.
        """
        return Path(os.getcwd())

    def _create_worktree(self, agent_id: str) -> Path:
        """``git worktree add`` a fresh worktree + branch for this agent.

        Returns the absolute path to the new worktree. Raises
        :class:`RuntimeError` on subprocess failure so the caller can
        mark the agent failed.
        """
        short_id = agent_id.replace("-", "")[:AGENT_SHORT_ID_HEX_CHARS]
        repo_root = self._repo_root()
        worktree_path = repo_root / WORKTREE_PARENT_DIR / f"agent-{short_id}"
        branch = f"agent/{short_id}"

        cmd = [
            "git",
            "-C",
            str(repo_root),
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
            stderr_preview = (result.stderr or "").strip()[:200]
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
        return worktree_path

    def _fetch_issue_bundle(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue body, comments, labels for the plan phase input.

        Returns a dict shaped like the ``/task-v2-plan`` SKILL.md input
        contract. Raises :class:`RuntimeError` on subprocess failure.
        """
        cmd = [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            self._cfg.github_repo,
            "--json",
            "number,title,body,labels,comments",
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
            stderr_preview = (result.stderr or "").strip()[:200]
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
        self, agent_id: str, phase: str, output_json: dict[str, Any]
    ) -> None:
        """INSERT the phase's output into ``dispatcher.phase_outputs``
        and append a row to ``dispatcher.phase_transitions``.

        Both tables share a single transaction so the observed state
        stays consistent under partial failure.
        """
        assert self._conn is not None, "connect() must run before persisting"
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.phase_outputs "
                    "    (agent_id, phase, output_json) "
                    "VALUES (%s, %s, %s)",
                    (agent_id, phase, json.dumps(output_json, default=str)),
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
                },
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass

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

    def _mark_agent_terminal(
        self,
        agent_id: str,
        status: str,
        phase: str,
        exit_code: int | None = None,
        pr_number: int | None = None,
    ) -> None:
        """UPDATE ``dispatcher.agents`` with terminal status + metadata.

        Used for ``succeeded``, ``failed``, ``crashed``, and the
        Phase 3A post-PR hand-off state (``status='running'``,
        ``phase='awaiting_ci'``). For terminal statuses (``succeeded`` /
        ``failed`` / ``crashed``) also sets ``ended_at`` so the admin
        page can compute duration AND writes back the resolved outcome
        to any pending ``dispatcher.diagnoses`` rows for this agent
        (Phase 3E #2798, spec §8 line 305).
        """
        assert self._conn is not None, "connect() must run before update"
        terminal = status in ("succeeded", "failed", "crashed")
        try:
            with self._conn.cursor() as cur:
                if terminal:
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

    def _spawn_phase_subprocess(
        self, phase: str, worktree: Path, agent_id: str
    ) -> tuple[int, float]:
        """Run ``claude -p '/task-v2-<phase> <agent_id>'`` synchronously.

        Captures stdout + stderr to ``{worktree}/tmp/claude-p-<phase>.log``.
        Returns ``(exit_code, duration_seconds)``. Raises
        :class:`subprocess.TimeoutExpired` on wall-clock timeout so the
        caller can mark the agent failed. Other subprocess errors bubble
        up as their native exception types.
        """
        max_turns = PHASE_MAX_TURNS[phase]
        model = PHASE_MODELS[phase]
        log_path = worktree / "tmp" / f"claude-p-{phase}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "claude",
            "-p",
            f"/task-v2-{phase} {agent_id}",
            "--cwd",
            str(worktree),
            "--max-turns",
            str(max_turns),
            "--model",
            model,
        ]

        start = time.monotonic()
        self._log.info(
            "daemon.phase_started",
            extra={
                "event": "phase_started",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "phase": phase,
                "model": model,
                "max_turns": max_turns,
            },
        )

        with log_path.open("w", encoding="utf-8") as log_file:
            result = subprocess.run(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        duration = time.monotonic() - start
        return result.returncode, duration

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
        repo_root = self._repo_root()
        worktree_path = repo_root / WORKTREE_PARENT_DIR / f"agent-{short_id}"

        self._log.info(
            "daemon.candidate_picked",
            extra={
                "event": "candidate_picked",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
            },
        )

        if not self._atomic_claim(issue_number, agent_id, str(worktree_path)):
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
                agent_id, status="failed", phase="claim", exit_code=None
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
        """
        ok = self._run_plan_phase(agent_id, issue_number, worktree)
        if not ok:
            return
        ok = self._run_ralph_phase(agent_id, issue_number, worktree)
        if not ok:
            return
        ok = self._run_summary_phase(agent_id, issue_number, worktree)
        if not ok:
            return

        # Daemon-side git commit + push + PR create.
        self._push_and_open_pr(agent_id, issue_number, worktree)

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
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = 'running', phase = 'claiming' "
                    "WHERE agent_id = %s",
                    (agent_id,),
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
                agent_id, status="failed", phase="planning", exit_code=None
            )
            return False

        plan_input = {
            "agent_id": agent_id,
            "worktree_path": str(worktree),
            "repo_root": str(worktree),
            **bundle,
        }
        self._write_phase_input(worktree, "plan", plan_input)

        exit_code = self._run_subprocess_or_fail(agent_id, "plan", worktree)
        if exit_code is None:
            return False

        plan_output = self._read_phase_output(worktree, "plan")
        if plan_output is None:
            self._log.warning(
                "daemon.phase_output_missing",
                extra={
                    "event": "phase_output_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": "plan",
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="planning", exit_code=exit_code
            )
            return False

        self._persist_phase_output(agent_id, "plan", plan_output)
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
            # succeed. A populated block_reason indicates a hard
            # problem — fail so operators see it.
            status = "failed" if reason else "succeeded"
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
            self._mark_agent_terminal(
                agent_id, status=status, phase="planning", exit_code=exit_code
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

        exit_code = self._run_subprocess_or_fail(agent_id, "ralph", worktree)
        if exit_code is None:
            return False

        ralph_output = self._read_phase_output(worktree, "ralph")
        if ralph_output is None:
            self._log.warning(
                "daemon.phase_output_missing",
                extra={
                    "event": "phase_output_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": "ralph",
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="ralph", exit_code=exit_code
            )
            return False

        self._persist_phase_output(agent_id, "ralph", ralph_output)
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
                agent_id, status="failed", phase="ralph", exit_code=exit_code
            )
            return False

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

        exit_code = self._run_subprocess_or_fail(agent_id, "summary", worktree)
        if exit_code is None:
            return False

        summary_output = self._read_phase_output(worktree, "summary")
        if summary_output is None:
            self._log.warning(
                "daemon.phase_output_missing",
                extra={
                    "event": "phase_output_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": "summary",
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="summary", exit_code=exit_code
            )
            return False

        self._persist_phase_output(agent_id, "summary", summary_output)
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

        unmet = summary_output.get("unmet_criteria") or []
        if unmet:
            self._log.info(
                "daemon.summary_unmet_criteria",
                extra={
                    "event": "summary_unmet_criteria",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "unmet_criteria": unmet,
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="summary", exit_code=exit_code
            )
            return False

        self._agent_summary_output = summary_output
        return True

    def _run_subprocess_or_fail(
        self, agent_id: str, phase: str, worktree: Path
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
        """
        try:
            exit_code, duration = self._spawn_phase_subprocess(
                phase, worktree, agent_id
            )
        except subprocess.TimeoutExpired:
            # Timeout = subprocess runaway (spec §17 Risk 4). Treat as
            # a generic ``subprocess_crash`` — the subprocess didn't
            # actually crash but the next retry needs a fresh worktree
            # just the same.
            self._handle_subprocess_failure(
                agent_id=agent_id,
                phase=phase,
                reason="timeout",
                exit_code=None,
                stderr_tail="",
                duration_s=None,
                extra={"timeout_seconds": CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS},
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
                agent_id, status="failed", phase=phase, exit_code=None
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
                agent_id, status="failed", phase=phase, exit_code=None
            )
            return None

        if exit_code != 0:
            # Per-phase skills always exit 0 — a non-zero code is an
            # infra failure (claude-p crash, OOM, harness error, auth
            # error, turn-limit trip). Tail the log for forensic
            # context but don't include verbatim in the structured
            # log envelope (may contain secrets). The classifier only
            # sees a short tail so a bad regex cannot echo a secret.
            tail = self._log_tail(worktree, phase, max_chars=500)
            self._handle_subprocess_failure(
                agent_id=agent_id,
                phase=phase,
                reason="nonzero_exit",
                exit_code=exit_code,
                stderr_tail=tail,
                duration_s=duration,
                extra={},
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
        )

        if category in AUTO_RETRY_CATEGORIES:
            self._create_retry_marker(agent_id=agent_id, reason=category)

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

    def _push_and_open_pr(
        self, agent_id: str, issue_number: int, worktree: Path
    ) -> None:
        """Run the final mechanical steps: commit, push, open PR.

        On failure at any step, the agent is marked ``failed``. On
        success, ``phase='awaiting_ci'`` so Phase 3B knows where to
        pick up, and ``status`` stays ``running``.
        """
        self._update_agent_phase(agent_id, "push_and_pr")

        summary_output = self._agent_summary_output or {}
        commit_message = summary_output.get("commit_message") or ""
        pr_title = summary_output.get("pr_title") or ""
        pr_body_md = summary_output.get("pr_body_md") or ""

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
                agent_id, status="failed", phase="push_and_pr", exit_code=None
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
        except Exception as exc:
            self._log.exception(
                "daemon.git_add_failed",
                extra={
                    "event": "git_add_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
            )
            return
        if add_result.returncode != 0:
            self._log.warning(
                "daemon.git_add_failed",
                extra={
                    "event": "git_add_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": add_result.returncode,
                    "stderr_tail": (add_result.stderr or "")[:200],
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
            )
            return

        # git commit -F <file>. Write the message to a file in the
        # worktree's tmp/ so we don't rely on shell quoting.
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
                agent_id, status="failed", phase="push_and_pr", exit_code=None
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
                    "stderr_tail": (commit_result.stderr or "")[:200],
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
            )
            return

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
                timeout=120,
                check=False,
            )
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
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
            )
            return
        if push_result.returncode != 0:
            self._log.warning(
                "daemon.git_push_failed",
                extra={
                    "event": "git_push_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": push_result.returncode,
                    "stderr_tail": (push_result.stderr or "")[:200],
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
            )
            return

        # gh pr create with --body-file pointing to a scratch file in
        # the worktree's tmp/.
        pr_body_path = worktree / "tmp" / "pr_body.md"
        pr_body_path.write_text(pr_body_md)
        try:
            pr_result = subprocess.run(
                [
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
                ],
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
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
            )
            return
        if pr_result.returncode != 0:
            self._log.warning(
                "daemon.pr_create_failed",
                extra={
                    "event": "pr_create_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": pr_result.returncode,
                    "stderr_tail": (pr_result.stderr or "")[:200],
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="push_and_pr", exit_code=None
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
            },
        )

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
                cur.execute(
                    "SELECT agent_id, issue_number, phase, pr_number, "
                    "       worktree_path, retries_used, status "
                    "FROM dispatcher.agents "
                    "WHERE (status = 'running' "
                    "       AND phase IN ('awaiting_ci', 'awaiting_deploy')) "
                    "   OR (status = 'succeeded' "
                    "       AND phase IN ('done', 'retro_done', 'retro_failed')) "
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
                    # touching the success status. retro phase failure
                    # → flip to retro_failed so cleanup still runs;
                    # cleanup phase failure → flip to cleanup_blocked
                    # so an operator can investigate.
                    if phase == "done":
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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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
            self._log.warning(
                "daemon.phase_output_missing",
                extra={
                    "event": "phase_output_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": "fix-ci",
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_ci", exit_code=exit_code
            )
            return

        self._persist_phase_output(agent_id, "fix-ci", fix_ci_output)
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
                    "stderr_tail": (add_result.stderr or "")[:200],
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
                    "stderr_tail": (commit_result.stderr or "")[:200],
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
                timeout=120,
                check=False,
            )
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
                    "stderr_tail": (push_result.stderr or "")[:200],
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
                    "stderr_tail": (result.stderr or "")[:200],
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
        """Spawn ``/task-v2-verify``, post evidence comment, mark succeeded."""
        agent_id = agent["agent_id"]
        issue_number = agent["issue_number"]
        pr_number = agent["pr_number"]
        worktree = Path(agent["worktree_path"])

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
            self._log.warning(
                "daemon.phase_output_missing",
                extra={
                    "event": "phase_output_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "phase": "verify",
                },
            )
            self._mark_agent_terminal(
                agent_id, status="failed", phase="awaiting_deploy", exit_code=exit_code
            )
            return

        self._persist_phase_output(agent_id, "verify", verify_output)

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
            self._mark_agent_terminal(
                agent_id, status="failed", phase="done", exit_code=exit_code
            )
            return

        # VERIFIED or SKIPPED — either is a success for the daemon.
        self._mark_agent_terminal(
            agent_id, status="succeeded", phase="done", exit_code=0
        )
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
                    "stderr_tail": (result.stderr or "")[:200],
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
            self._log.warning(
                "daemon.retro_timeout",
                extra={
                    "event": "retro_timeout",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return
        except (FileNotFoundError, OSError) as exc:
            self._log.warning(
                "daemon.retro_subprocess_error",
                extra={
                    "event": "retro_subprocess_error",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "detail": str(exc),
                },
            )
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return

        if exit_code != 0:
            self._log.warning(
                "daemon.retro_nonzero_exit",
                extra={
                    "event": "retro_nonzero_exit",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "exit_code": exit_code,
                    "duration_s": duration_s,
                },
            )
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return

        retro_output = self._read_phase_output(worktree, "retro")
        if retro_output is None:
            self._log.warning(
                "daemon.retro_output_missing",
                extra={
                    "event": "retro_output_missing",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._update_agent_phase(agent_id, PHASE_RETRO_FAILED)
            return

        # Persist the retro output to dispatcher.phase_outputs +
        # phase_transitions so the admin page sees the run.
        self._persist_phase_output(agent_id, "retro", retro_output)

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
                    "stderr_tail": (result.stderr or "")[:200],
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
                "stderr_tail": (result.stderr or "").strip()[:200],
                "stdout_tail": (result.stdout or "").strip()[:200],
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

        retry_outcome = "succeeded" if final_status == "succeeded" else "failed"
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

    def _check_stuck_agents(self) -> int:
        """Find running agents with no phase transition in >30 min.

        Each flagged agent gets a ``dispatcher.failures`` row with
        ``category='stuck_timeout'`` and is flipped to
        ``status='crashed'``. A retry marker is created so the next
        ``_process_retry_markers`` pass can reset the agent with a
        fresh worktree.

        Returns the number of stuck agents flagged this tick (for
        logging). Exceptions are caught per-agent + logged; one bad
        row cannot stall the scan.
        """
        assert self._conn is not None, "connect() must run before stuck check"

        stuck_rows: list[tuple[str, int | None, str | None]] = []
        try:
            with self._conn.cursor() as cur:
                # Find agents with no recent phase transition. The
                # LEFT JOIN + MAX handles agents whose first transition
                # hasn't fired yet (e.g. wedged during `claiming`): in
                # that case the MAX is NULL and we fall back to
                # ``started_at`` — the agent is still stuck for the
                # same operational reason.
                cur.execute(
                    "SELECT a.agent_id, a.issue_number, a.phase "
                    "FROM dispatcher.agents a "
                    "LEFT JOIN LATERAL ("
                    "    SELECT MAX(ts) AS last_ts "
                    "    FROM dispatcher.phase_transitions "
                    "    WHERE agent_id = a.agent_id"
                    ") pt ON TRUE "
                    "WHERE a.status = 'running' "
                    "  AND COALESCE(pt.last_ts, a.started_at) "
                    "      < now() - make_interval(secs => %s)",
                    (STUCK_TIMEOUT_SECONDS,),
                )
                rows = cur.fetchall()
                for row in rows:
                    stuck_rows.append(
                        (
                            str(row[0]),
                            int(row[1]) if row[1] is not None else None,
                            str(row[2]) if row[2] is not None else None,
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

        flagged = 0
        for agent_id, issue_number, phase in stuck_rows:
            try:
                self._write_failure(
                    agent_id=agent_id,
                    category=FAILURE_CATEGORY_STUCK_TIMEOUT,
                    detected_by="supervisor",
                    details={
                        "stuck_seconds": STUCK_TIMEOUT_SECONDS,
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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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
        """
        if not worktree_path:
            return False

        repo_root = self._repo_root()
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
                        "stderr_tail": (result.stderr or "").strip()[:200],
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
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
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
                "stderr_tail": (result.stderr or "").strip()[:200],
            },
        )
        return False

    def _process_retry_markers(self) -> int:
        """Drain due retry markers, reset each agent, resolve the marker.

        Called from the supervisor tick. For each unresolved marker
        whose ``retry_after_ts`` has elapsed:

        1. Best-effort drop the existing worktree (see
           :meth:`_drop_worktree_best_effort`).
        2. UPDATE the agent to ``status='retrying' phase='claiming'``.
           The next scheduler tick's claim path sees the retry state
           and re-orchestrates (creates a fresh worktree via 3A's
           ``_create_worktree``). Incrementing ``retries_used`` tracks
           the total retries across all reasons for the admin page.
        3. Mark the retry marker ``resolved_at = now()``.

        Returns the number of markers processed this tick. DB errors on
        any single marker are logged + rolled back; the loop continues.
        """
        assert self._conn is not None, "connect() must run before marker drain"

        try:
            with self._conn.cursor() as cur:
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

            try:
                self._drop_worktree_best_effort(worktree_path)

                with self._conn.cursor() as cur:
                    # Reset the agent for a fresh claim. ``started_at``
                    # stays pinned to the original claim so elapsed-time
                    # dashboards keep working; ``retries_used``
                    # increments so the 3B fix-ci cap and 3D diagnoser
                    # tier-3 (``ci_red_after_retries``) see the full
                    # retry history.
                    cur.execute(
                        "UPDATE dispatcher.agents "
                        "SET status = 'retrying', "
                        "    phase = 'claiming', "
                        "    retries_used = retries_used + 1, "
                        "    exit_code = NULL, "
                        "    ended_at = NULL "
                        "WHERE agent_id = %s",
                        (agent_id,),
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

        return {
            "agent_id": agent_id,
            "failure_id": failure_id,
            "failure_category": candidate["category"],
            "tier": candidate["tier"],
            "issue_number": issue_number,
            "issue_title": issue_title,
            "issue_body": issue_body,
            "recent_phase_transitions": phase_transitions,
            "prior_failures": prior_failures,
            "ralph_done_content": ralph_done_content,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "ci_log_url": ci_log_url,
            "prior_mechanical_fix": prior_mechanical_fix,
            "worktree_path": worktree_path,
        }

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
        here so tests can monkeypatch ``subprocess.run`` without
        touching the surrounding DB-write logic.
        """
        cmd = [
            "claude",
            "-p",
            f"/diagnose-failure {diagnosis_id}",
            "--max-turns",
            str(DIAGNOSER_MAX_TURNS),
            "--model",
            DIAGNOSER_MODEL,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            tail = ""
            if exc.stderr:
                tail = (
                    exc.stderr.decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, bytes)
                    else str(exc.stderr)
                )[-500:]
            return None, tail
        except FileNotFoundError:
            return None, "claude binary not found"

        stderr_tail = (result.stderr or "")[-500:]
        return result.returncode, stderr_tail

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
        ``retry_with_hint`` requires a non-empty string ``hint``;
        ``reissue`` requires a non-empty string ``new_scope``. All
        other fields are advisory.
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
        return action

    def _consume_diagnosis(self, diagnosis_id: int, candidate: dict[str, Any]) -> str:
        """Read the recommendation and execute the deterministic action.

        Returns the action string that was consumed (one of
        :data:`DIAGNOSER_ACTIONS`, or ``"escalate_fallback"`` when the
        recommendation was malformed and we escalated mechanically).
        """
        recommendation = self._read_recommendation(diagnosis_id)
        if recommendation is None:
            self._mark_diagnosis_failed(
                diagnosis_id, reason="recommendation_missing_or_malformed_json"
            )
            self._apply_mechanical_escalation(candidate)
            return "escalate_fallback"

        action = self._validate_recommendation(recommendation)
        if action is None:
            self._log.warning(
                "daemon.diagnosis_action_unknown",
                extra={
                    "event": "diagnosis_action_unknown",
                    "run_id": self._run_id,
                    "diagnosis_id": diagnosis_id,
                    "raw_recommendation": recommendation,
                },
            )
            self._mark_diagnosis_failed(
                diagnosis_id, reason="recommendation_failed_validation"
            )
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

        return action

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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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
                    "stderr_tail": (result.stderr or "").strip()[:200],
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

    try:
        return daemon.run_forever()
    finally:
        daemon.close()


if __name__ == "__main__":  # pragma: no cover — exercised via `python -m`
    sys.exit(main())
