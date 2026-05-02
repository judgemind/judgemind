"""dispatcher-v3 launcher main loop — claim + watch.

The single long-lived process for dispatcher-v3. Each tick (default 30s):

1. **Consume commands** — drain ``dispatcher.commands`` (start, stop,
   pause, force_kill, set_cap). Same control-plane shape as v2.
2. **Heartbeat** — UPDATE ``dispatcher.runs.heartbeat_ts`` so the
   CloudWatch staleness alarm stays green.
3. **Watch in-flight tasks** — for every v3-owned ``dispatcher.agents``
   row with ``task_arn IS NOT NULL AND ended_at IS NULL``, call
   ``ecs:DescribeTasks`` and resolve the row's terminal state when the
   task has STOPPED. For RUNNING tasks the silent-hang detector
   (issue #3881) calls ``cloudwatch_logs:DescribeLogStreams`` to read
   the session log's ``lastEventTimestamp``; if the stream has not
   received an event in ``silent_hang_minutes`` (default 30), the task
   is killed via ``ecs:StopTask`` and the agent row is marked
   ``failed`` with ``exit_reason='silent_hang'``. On any non-success
   terminal (STOPPED-non-zero or silent-hang kill) the launcher
   inline-spawns a ``diagnoser`` ECS task (issue #3882, spec §4.2)
   with ``AGENT_ID`` set, capping at 1 diagnoser per agent.
4. **Watch in-flight diagnosers** — for every v3-owned row with
   ``diagnoser_arn IS NOT NULL AND status='failed' AND outcome_summary
   IS NULL`` (the in-flight diagnoser predicate), call
   ``ecs:DescribeTasks`` against ``diagnoser_arn`` and resolve. STOPPED-0
   is a no-op for the launcher — the diagnoser SKILL has written
   ``outcome_summary`` itself; the launcher only writes a fallback
   sentinel if it's still null (so the watch query stops matching).
   STOPPED-non-zero / OOM / spot-reclaim → set ``status='needs_review'``
   and fire a Telegram alert via ``_mark_agent_needs_review`` (#3883).
5. **Recover partial claims** — rows with ``status='claiming'`` and
   ``task_arn IS NULL`` that have aged past
   :data:`PARTIAL_CLAIM_RECOVERY_AGE_SECONDS` are marked ``failed``
   with ``exit_reason='claim_abandoned'``. Belt-and-suspenders for the
   narrow window where a label flip or RunTask fails after the DB
   INSERT lands.
6. **Claim if cap allows** — read ``dispatcher.config.concurrency_cap_v3``
   (v3-scoped key — see issue #3880 notes — kept independent of v2's
   ``concurrency_cap`` so each daemon can ramp / kill-switch without
   touching the other), count v3 running agents, and for each ready
   issue under cap: trust-check, skip ``dispatcher/v2-only``, check
   per-issue claim budget, then run the atomic claim sequence.

The atomic claim sequence matches v2 (``daemon.py:5142-5240``) exactly:

  a. INSERT ``dispatcher.agents`` row (``status='running'``,
     ``parent_run_id=self._run_id``). The partial UNIQUE INDEX from
     migration 25 (``WHERE status IN ('running', 'retrying')``) is the
     atomic primitive — a concurrent second INSERT raises
     :class:`psycopg.errors.UniqueViolation` and the loser abandons.
     The v3 spec text says ``status='claiming'`` but ``'claiming'``
     is NOT in the unique-index predicate, so writing it would skip
     the atomic gate and produce duplicate claims under the v2/v3
     cohabitation race. Using ``'running'`` matches v2 and keeps the
     gate intact; the in-flight ``current_milestone='claiming'``
     column carries the human-visible "we are still wiring up" detail
     for the cockpit.
  b. Add ``status/in-progress`` GitHub label. (The label is the
     human/v2-skill coordination signal; the DB row is the atomic
     primitive — label-write failure is logged and does not roll back
     the DB claim.)
  c. Remove ``agent/ready`` GitHub label.
  d. ``ecs:RunTask`` against the task-runner task definition with
     ``AGENT_ID``, ``TASK_ISSUE_NUMBER``, ``RUNNER``,
     ``SESSIONS_BUCKET`` env-var overrides.
  e. UPDATE the agent row with the returned ``task_arn``.

Failure mid-sequence: step (b)/(c) failures are logged but tolerated
(the labels are best-effort signals). Step (d) failure marks the row
``failed`` with ``exit_reason='launch_failed'`` and removes the
``status/in-progress`` label so the issue can be re-claimed. Step (a)
failure (the UniqueViolation race) returns False with no side effects.

Boto3 and psycopg are imported lazily inside method bodies so the
module is import-clean for unit tests that mock the world.

Issue #3880.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable, Optional

from dispatcher_v3 import telegram
from dispatcher_v3.breaker import BreakerEvaluator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default tick interval for :meth:`Launcher.run_forever`. Mirrors the v3
#: spec §4.1's "loop runs every 30s".
DEFAULT_TICK_INTERVAL_SECONDS = 30

#: Default per-issue claim attempt budget when ``dispatcher.config``
#: does not set ``claim_attempts_max``. Matches v3 spec §4.1 default.
DEFAULT_CLAIM_ATTEMPTS_MAX = 3

#: Default v3 concurrency cap. Cohabitation default is 0 (off) — the
#: operator flips this to 1 to start the v3 ramp (spec §9 step 3).
DEFAULT_CONCURRENCY_CAP_V3 = 0

#: Default subprocess timeout for ``gh issue list`` / ``gh issue edit``
#: calls. Mirrors v2's GH_POLL_SUBPROCESS_TIMEOUT_SECONDS shape.
GH_SUBPROCESS_TIMEOUT_SECONDS = 30

#: Maximum age (seconds) a ``status='claiming'`` row can carry without
#: a populated ``task_arn`` before the partial-claim recovery path
#: marks it failed. Mirrors v3 spec §4.1's "5min" guidance.
PARTIAL_CLAIM_RECOVERY_AGE_SECONDS = 5 * 60

#: Default silent-hang threshold (minutes) when ``dispatcher.config``
#: does not set ``silent_hang_minutes``. Matches v3 spec §4.1: a
#: RUNNING task whose CloudWatch log stream has not received an event
#: in this many minutes is reaped via ``ecs:StopTask`` and the agent
#: row is marked ``failed`` with ``exit_reason='silent_hang'``.
#:
#: Threshold is in minutes (not seconds) for symmetry with the spec
#: text and the v2 ``silent_hang_minutes`` shape; conversion to
#: seconds happens once at the comparison site.
DEFAULT_SILENT_HANG_MINUTES = 30

#: Exit reason recorded on agent rows reaped by the silent-hang
#: detector. Matches v3 spec §4.1 verbatim. Diagnoser invocation
#: keys off this string (#3882, C5).
EXIT_REASON_SILENT_HANG = "silent_hang"

#: Default wall-clock cap (seconds) for a task-runner ECS task. The
#: launcher's ``_watch_in_flight`` loop ecs:StopTask's any task-runner
#: whose ``now - started_at`` exceeds this. Matches v3 spec §11 OQ#4:
#: 6h covers virtually every real ``/task`` run including a long ralph
#: + fix-CI cycle.
#:
#: Pre-#3940 this cap was rendered into the task-def's ``stopTimeout``
#: field, but Fargate rejects task-defs with ``stopTimeout > 120s``.
#: The fix moves enforcement to the launcher: the F2 task-defs module
#: passes the per-task-def cap via the ``TASK_RUNNER_WALL_CLOCK_SECONDS``
#: / ``DIAGNOSER_WALL_CLOCK_SECONDS`` /
#: ``SCHEDULED_SKILL_WALL_CLOCK_SECONDS`` env vars on the launcher
#: container; ``_build_launcher_from_env`` parses them.
#:
#: A non-positive value disables the wall-clock check entirely (graceful
#: degradation when the launcher is started outside an F2-rendered
#: task-def, e.g. local CLI runs).
DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS = 21600  # 6h

#: Default wall-clock cap for the diagnoser. The diagnoser is a one-shot
#: read + side-effect skill that should not run long; 1h is a generous
#: ceiling per issue body. Wired into ``_watch_diagnosers`` symmetrically
#: with the task-runner case in ``_watch_in_flight``.
DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS = 3600  # 1h

#: Default wall-clock cap for scheduled-skill tasks. Each scheduled
#: skill (audit / dispatcher-audit / spotcheck / dispatcher-daily-report)
#: sits comfortably under 2h.
#:
#: Note: scheduled-skill tasks are launched by EventBridge, NOT the
#: launcher's claim path, so they do not appear in ``dispatcher.agents``
#: and the launcher does not enforce this cap directly. The value is
#: still threaded through the launcher env for observability and so a
#: future ``_watch_scheduled_skills`` (paralleling ``_watch_in_flight``)
#: can reuse the same plumbing without a config refactor.
DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS = 7200  # 2h

#: Exit reason recorded on agent rows reaped by the wall-clock-cap
#: detector. Distinct from ``silent_hang`` so the cockpit / diagnoser
#: can branch on the kill class -- a wall-clock kill means the agent
#: was actively producing log output but never finished, while
#: ``silent_hang`` means it stopped producing output entirely.
EXIT_REASON_WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"

#: Default CloudWatch Logs group for the v3 ``task-runner`` ECS task
#: definition. The group name is wired into F2's task-def via the
#: ``awslogs-group`` log-driver option; the launcher needs the same
#: name to call ``DescribeLogStreams``. Default mirrors the v2
#: agent-runner naming convention (``/ecs/judgemind-...-${env}``);
#: F2 will override via the ``TASK_RUNNER_LOG_GROUP`` env var when
#: it lands the actual log group resource. Empty string disables the
#: silent-hang detector entirely (graceful degradation when F2 has
#: not shipped yet — the wall-clock cap remains the only liveness
#: signal).
DEFAULT_TASK_RUNNER_LOG_GROUP = ""

#: Default awslogs stream prefix for the ``task-runner`` container.
#: ECS awslogs driver builds stream names as
#: ``<prefix>/<container-name>/<task-id>`` where ``task-id`` is the
#: last URI segment of the task ARN. The launcher reconstructs the
#: stream name from the prefix + container name + task ARN so it can
#: filter ``DescribeLogStreams`` to a single stream per task. The
#: container name is fixed to ``task-runner`` in
#: :meth:`Launcher._run_task_for_agent`'s ``containerOverrides``.
DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX = "task-runner"

#: Default CloudWatch ``DescribeLogStreams`` API timeout (seconds).
#: A misbehaving CW endpoint must not wedge the watch loop — the
#: launcher catches and logs failures, so individual API stalls just
#: mean "skip the silent-hang check this tick" without affecting the
#: rest of the tick.
CLOUDWATCH_API_TIMEOUT_SECONDS = 10

#: GitHub label names the launcher reads/writes.
LABEL_AGENT_READY = "agent/ready"
LABEL_STATUS_IN_PROGRESS = "status/in-progress"
LABEL_STATUS_BLOCKED = "status/blocked"
LABEL_STATUS_NEEDS_HUMAN = "status/needs-human"
LABEL_DISPATCHER_V2_ONLY = "dispatcher/v2-only"

#: Page limit for ``gh issue list``. Matches v2's QUEUE_SCAN_PAGE_LIMIT.
QUEUE_SCAN_PAGE_LIMIT = 200

#: Run-id filter shared across reads/updates to keep v3 strictly scoped
#: to its own rows. The launcher only claims rows whose
#: ``parent_run_id`` is its own ``run_id``; this filter is applied as a
#: subquery against ``dispatcher.runs.dispatcher_version='v3'`` for
#: cross-restart reads (e.g. partial-claim recovery on boot).
V3_SCOPED_PARENT_RUN_FILTER = (
    "parent_run_id IN ("
    "SELECT run_id FROM dispatcher.runs WHERE dispatcher_version = 'v3'"
    ")"
)

#: Logger for the launcher module. The launcher writes structured
#: events (event-name + extras) so CloudWatch Logs Insights queries can
#: filter on ``event=claim_succeeded`` etc., the same convention v2
#: uses (#3850).
log = logging.getLogger("dispatcher_v3.launcher")


# ---------------------------------------------------------------------------
# Trust check helper (subprocess seam — patched by tests)
# ---------------------------------------------------------------------------


def _default_telegram_alerter(message: str, *, trigger: str, run_id: str) -> None:
    """Default Telegram alert hook — wraps :func:`telegram.send_alert`.

    Discards the ``(returncode, event_name)`` tuple — best-effort by
    design, the helper itself logs a structured event regardless of
    outcome. Tests inject a recorder via the :class:`Launcher`
    ``telegram_alerter`` constructor arg to assert call counts +
    arguments without spinning up the subprocess seam.
    """
    telegram.send_alert(message=message, trigger=trigger, run_id=run_id)


def _check_issue_author_trusted(
    issue_number: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Run ``scripts/check-issue-author.sh`` and return True iff exit 0.

    The script prints ``TRUSTED: ...`` on stdout for exit 0 and
    ``UNTRUSTED: ...`` for exit 1. Exit 2 indicates a transient API
    error — fail closed (return False) and let the next tick retry.
    Mirrors v2's ``DispatcherDaemon._issue_author_trusted`` shape.
    """
    cmd = ["scripts/check-issue-author.sh", str(issue_number)]
    try:
        result = runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=GH_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning(
            "launcher.trust_check_error",
            extra={
                "event": "trust_check_error",
                "issue_number": issue_number,
                "detail": str(exc),
            },
        )
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


class Launcher:
    """v3 launcher main loop.

    Constructor args are the orchestration knobs. Subprocess and AWS
    clients are passed in (or lazily-built) so tests can drive the loop
    with mocks.
    """

    def __init__(
        self,
        *,
        run_id: str,
        github_repo: str,
        ecs_cluster_arn: str,
        task_runner_task_definition: str,
        diagnoser_task_definition: str,
        agent_runner_subnet_ids: list[str],
        agent_runner_security_group_id: str,
        sessions_bucket: str,
        runner_name: str = "claude",
        task_runner_log_group: str = DEFAULT_TASK_RUNNER_LOG_GROUP,
        task_runner_log_stream_prefix: str = DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX,
        task_runner_wall_clock_seconds: int = DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS,
        diagnoser_wall_clock_seconds: int = DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS,
        scheduled_skill_wall_clock_seconds: int = DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS,
        conn: Any = None,
        ecs_client: Any = None,
        cloudwatch_logs_client: Any = None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[str]]
        | None = None,
        trust_checker: Callable[[int], bool] | None = None,
        breaker: BreakerEvaluator | None = None,
        telegram_alerter: Callable[..., None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._github_repo = github_repo
        self._ecs_cluster_arn = ecs_cluster_arn
        self._task_runner_task_definition = task_runner_task_definition
        self._diagnoser_task_definition = diagnoser_task_definition
        self._agent_runner_subnet_ids = list(agent_runner_subnet_ids)
        self._agent_runner_security_group_id = agent_runner_security_group_id
        self._sessions_bucket = sessions_bucket
        self._runner_name = runner_name
        self._task_runner_log_group = task_runner_log_group
        self._task_runner_log_stream_prefix = task_runner_log_stream_prefix
        self._task_runner_wall_clock_seconds = task_runner_wall_clock_seconds
        self._diagnoser_wall_clock_seconds = diagnoser_wall_clock_seconds
        self._scheduled_skill_wall_clock_seconds = scheduled_skill_wall_clock_seconds
        self._conn = conn
        self._ecs_client = ecs_client
        self._cloudwatch_logs_client = cloudwatch_logs_client
        self._subprocess_runner = subprocess_runner or subprocess.run
        self._trust_checker = trust_checker or (
            lambda n: _check_issue_author_trusted(n, runner=self._subprocess_runner)
        )
        # Telegram-alert seam: tests inject a recorder; production wraps
        # the module-level :func:`telegram.send_alert` via the default
        # below. The launcher's two outbound trigger sites
        # (``_mark_agent_needs_review`` and the breaker eval) both pipe
        # through this hook so a single test fake captures both.
        self._telegram_alerter = telegram_alerter or _default_telegram_alerter
        # Breaker is constructed lazily so a launcher built without a
        # connection (early in :class:`Launcher.__init__`-only tests)
        # doesn't crash. When ``conn`` is None the breaker is unused —
        # the watch-loop short-circuits before reaching it.
        self._breaker = breaker or BreakerEvaluator(
            conn=conn,
            run_id=run_id,
            telegram_alerter=self._telegram_alerter,
        )

    # -- public tick API ---------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Run one launcher iteration.

        Returns a dict summary used by tests / observability. The
        contents are intentionally cheap (counts, not row dumps) so the
        same shape can be logged each tick without flooding CloudWatch.
        """
        summary: dict[str, Any] = {
            "commands_consumed": 0,
            "heartbeat": False,
            "watched": 0,
            "transitions": [],
            "diagnosers_watched": 0,
            "diagnoser_transitions": [],
            "claims": [],
            "claim_skipped": [],
            "recovered": 0,
        }
        summary["commands_consumed"] = self._consume_commands()
        summary["heartbeat"] = self._heartbeat()
        watched, transitions = self._watch_in_flight()
        summary["watched"] = watched
        summary["transitions"] = transitions
        d_watched, d_transitions = self._watch_diagnosers()
        summary["diagnosers_watched"] = d_watched
        summary["diagnoser_transitions"] = d_transitions
        summary["recovered"] = self._recover_partial_claims()
        # Breaker auto-close runs BEFORE the claim step so a freshly-
        # closed breaker (operator reflip + or time-based eligibility)
        # restores the cap on the same tick the launcher then consults
        # ``concurrency_cap_v3`` for claiming.
        summary["breaker_auto_closed"] = self._maybe_auto_close_breaker()
        claims, skipped = self._claim_if_cap_allows()
        summary["claims"] = claims
        summary["claim_skipped"] = skipped
        return summary

    # -- breaker auto-close hop -------------------------------------------

    def _maybe_auto_close_breaker(self) -> bool:
        """Delegate to :meth:`BreakerEvaluator.maybe_auto_close`.

        Wrapped on the launcher so the tick step has a stable name and
        so test fakes can override the breaker entirely (constructor
        arg ``breaker=...``) without monkey-patching an attribute.
        """
        if self._conn is None:
            return False
        try:
            return self._breaker.maybe_auto_close()
        except Exception:
            log.exception(
                "launcher.breaker_auto_close_failed",
                extra={
                    "event": "breaker_auto_close_failed",
                    "run_id": self._run_id,
                },
            )
            return False

    def run_forever(
        self, *, tick_interval: int = DEFAULT_TICK_INTERVAL_SECONDS
    ) -> None:
        """Tick forever. The deployed entrypoint when not running tests."""
        log.info("launcher.boot", extra={"event": "boot", "run_id": self._run_id})
        while True:
            try:
                summary = self.tick()
                log.info(
                    "launcher.tick_complete",
                    extra={"event": "tick_complete", "run_id": self._run_id, **summary},
                )
            except Exception:  # noqa: BLE001 — top-level loop guard
                log.exception(
                    "launcher.tick_failed",
                    extra={"event": "tick_failed", "run_id": self._run_id},
                )
            time.sleep(tick_interval)

    # -- step 1: consume commands -----------------------------------------

    def _consume_commands(self) -> int:
        """Drain ``dispatcher.commands`` rows.

        v3 supports start, stop, pause, force_kill, set_cap. The
        per-command handlers live in :meth:`_handle_command`; an
        unknown command is logged and consumed (so it does not block
        the queue) but produces no side effects.
        """
        if self._conn is None:
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT command_id, command, payload "
                    "FROM dispatcher.commands "
                    "WHERE consumed_at IS NULL "
                    "ORDER BY issued_at ASC"
                )
                rows = list(cur.fetchall() or [])
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.commands_scan_failed",
                extra={"event": "commands_scan_failed", "run_id": self._run_id},
            )
            self._safe_rollback()
            return 0

        consumed = 0
        for row in rows:
            command_id = int(row[0])
            command = str(row[1])
            payload = row[2] if isinstance(row[2], dict) else {}
            try:
                self._handle_command(command, payload)
                with self._conn.cursor() as cur:
                    cur.execute(
                        "UPDATE dispatcher.commands "
                        "SET consumed_at = now() "
                        "WHERE command_id = %s",
                        (command_id,),
                    )
                self._conn.commit()
                consumed += 1
                log.info(
                    "launcher.command_consumed",
                    extra={
                        "event": "command_consumed",
                        "run_id": self._run_id,
                        "command_id": command_id,
                        "command": command,
                    },
                )
            except Exception:
                log.exception(
                    "launcher.command_handler_failed",
                    extra={
                        "event": "command_handler_failed",
                        "run_id": self._run_id,
                        "command_id": command_id,
                        "command": command,
                    },
                )
                self._safe_rollback()
        return consumed

    def _handle_command(self, command: str, payload: dict[str, Any]) -> None:
        """Per-command side effects.

        Writes to ``dispatcher.config.concurrency_cap_v3`` for cap
        flips. ``force_kill`` calls ``ecs:StopTask`` on the named
        agent's ``task_arn``. The unknown-command path is a no-op
        (logged) so a v2-only command name does not block the queue.
        """
        assert self._conn is not None  # guaranteed by _consume_commands caller
        if command == "start":
            target = int(payload.get("cap", 1))
            self._set_config("concurrency_cap_v3", str(target))
        elif command == "stop":
            self._set_config("concurrency_cap_v3", "0")
        elif command == "pause":
            self._set_config("concurrency_cap_v3", "0")
        elif command == "set_cap":
            target = int(payload.get("cap", 0))
            self._set_config("concurrency_cap_v3", str(max(0, target)))
        elif command == "force_kill":
            agent_id = str(payload.get("agent_id", ""))
            if agent_id:
                self._force_kill_agent(agent_id)
        else:
            log.warning(
                "launcher.unknown_command",
                extra={
                    "event": "unknown_command",
                    "run_id": self._run_id,
                    "command": command,
                },
            )

    def _set_config(self, key: str, value: str) -> None:
        """UPSERT ``dispatcher.config`` (key, value)."""
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dispatcher.config (key, value, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = now()",
                (key, value),
            )

    def _force_kill_agent(self, agent_id: str) -> None:
        """Stop running ECS tasks for *agent_id* (best-effort).

        Cascades to both ``task_arn`` and ``diagnoser_arn`` (spec §4.2):
        the operator's force-kill must also stop an in-flight diagnoser
        so we don't leave an orphan running after the operator has
        already decided. The row predicate intentionally drops the
        ``ended_at IS NULL`` filter when reading ``diagnoser_arn`` —
        the agent's row may carry ``ended_at`` (the original task ended
        and a diagnoser was spawned to inspect it) while the diagnoser
        ECS task itself is still in flight.
        """
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT task_arn, diagnoser_arn, ended_at FROM dispatcher.agents "
                "WHERE agent_id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
        if row is None:
            return
        task_arn = row[0]
        diagnoser_arn = row[1]
        ended_at = row[2]
        # The agent's task ARN is only "live" if the row hasn't ended.
        # Once ended, the task has STOPPED and stop_task is a no-op
        # (or fails); skip it. The diagnoser ARN is treated independently
        # of ``ended_at`` — it may be set on a row whose original task
        # already ended.
        targets: list[tuple[str, str]] = []
        if task_arn and ended_at is None:
            targets.append(("task_arn", str(task_arn)))
        if diagnoser_arn:
            targets.append(("diagnoser_arn", str(diagnoser_arn)))
        if not targets:
            return
        try:
            client = self._get_ecs_client()
        except Exception:
            log.exception(
                "launcher.force_kill_client_init_failed",
                extra={
                    "event": "force_kill_client_init_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            return
        for kind, arn in targets:
            try:
                client.stop_task(
                    cluster=self._ecs_cluster_arn,
                    task=arn,
                    reason="force_kill",
                )
            except Exception:
                log.exception(
                    "launcher.force_kill_failed",
                    extra={
                        "event": "force_kill_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                        "arn_kind": kind,
                    },
                )

    # -- step 2: heartbeat -------------------------------------------------

    def _heartbeat(self) -> bool:
        """UPDATE ``dispatcher.runs.heartbeat_ts``."""
        if self._conn is None:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.runs SET heartbeat_ts = now() WHERE run_id = %s",
                    (self._run_id,),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.heartbeat_failed",
                extra={"event": "heartbeat_failed", "run_id": self._run_id},
            )
            self._safe_rollback()
            return False
        return True

    # -- step 3: watch in-flight ------------------------------------------

    def _watch_in_flight(self) -> tuple[int, list[dict[str, Any]]]:
        """For each in-flight v3 agent, resolve STOPPED tasks and reap silent hangs.

        Three liveness signals are checked per RUNNING task this tick:

        - **ECS task lifecycle.** STOPPED + exit 0 → ``succeeded``;
          STOPPED + non-zero → ``failed`` with the ECS ``stoppedReason``
          captured. This is the coarse "the container exited" signal.
        - **Wall-clock cap.** RUNNING + ``now - started_at`` exceeds
          ``task_runner_wall_clock_seconds`` (default 21600, 6h) →
          ``ecs:StopTask`` + ``failed`` with
          ``exit_reason='wall_clock_exceeded'``. The wall-clock cap is
          the *coarse* liveness check per spec §11 OQ#4; pre-#3940 it
          was rendered into the task-def's ``stopTimeout`` field, but
          Fargate rejects task-defs with ``stopTimeout > 120s``, so the
          launcher enforces it here instead. Checked BEFORE the silent-
          hang detector so a wall-clock-killed task doesn't spend an
          extra tick waiting on a CloudWatch round-trip.
        - **CloudWatch session-log silence.** RUNNING + log-stream
          ``lastEventTimestamp`` older than ``silent_hang_minutes``
          (default 30) → ``ecs:StopTask`` + ``failed`` with
          ``exit_reason='silent_hang'``. This catches a wedged
          ``claude -p`` (Anthropic-side hang, network blip, hung HTTP)
          that produces no log output. Distinct from the wall-clock cap
          because a wall-clock kill means the agent was actively
          producing output but never finished, while ``silent_hang``
          means it stopped producing output entirely.

        On any non-success terminal — STOPPED-non-zero, wall-clock
        exceeded, or silent-hang reap — the launcher inline-spawns a
        ``diagnoser`` ECS task (issue #3882, spec §4.2) with
        ``AGENT_ID`` set. The per-agent cap of 1 diagnoser is enforced
        inside ``_launch_diagnoser`` via a fresh re-read of the row's
        ``diagnoser_arn`` value, and the ``exit_reason`` field carries
        the kill class so the diagnoser SKILL can branch on it when it
        reads the row.
        """
        if self._conn is None:
            return 0, []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT agent_id, task_arn, issue_number, started_at "
                    "FROM dispatcher.agents "
                    "WHERE task_arn IS NOT NULL AND ended_at IS NULL "
                    f"  AND {V3_SCOPED_PARENT_RUN_FILTER}",
                )
                rows = list(cur.fetchall() or [])
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.watch_scan_failed",
                extra={"event": "watch_scan_failed", "run_id": self._run_id},
            )
            self._safe_rollback()
            return 0, []
        if not rows:
            return 0, []
        descriptions = self._describe_tasks([str(r[1]) for r in rows])
        by_arn = {
            d["taskArn"]: d for d in descriptions if isinstance(d.get("taskArn"), str)
        }
        silent_hang_seconds = self._read_silent_hang_minutes() * 60
        # Wall-clock cap is constructor-injected (env-var driven via
        # `_build_launcher_from_env`); compute the deadline timestamp
        # once per tick.
        wall_clock_seconds = self._task_runner_wall_clock_seconds
        transitions: list[dict[str, Any]] = []
        for agent_id, task_arn, issue_number, started_at in rows:
            desc = by_arn.get(str(task_arn))
            if desc is None:
                continue  # transient API blip — re-check next tick
            issue_num = int(issue_number) if issue_number is not None else 0
            if str(desc.get("lastStatus") or "") == "STOPPED":
                transitions.append(
                    self._resolve_stopped_task(
                        agent_id=str(agent_id),
                        task_arn=str(task_arn),
                        issue_number=issue_num,
                        desc=desc,
                    )
                )
                continue
            # RUNNING — check the wall-clock cap first (#3940). Pre-
            # #3940 this was enforced by Fargate via the task-def's
            # ``stopTimeout`` field, but Fargate rejects task-defs
            # whose ``stopTimeout`` exceeds 120 seconds. The launcher
            # now owns the enforcement: if ``now - started_at`` exceeds
            # the cap, ``ecs:StopTask`` the task and mark the agent
            # failed with ``exit_reason='wall_clock_exceeded'``. The
            # cap is constructor-injected from the launcher's env vars
            # (``TASK_RUNNER_WALL_CLOCK_SECONDS``); a non-positive cap
            # disables the check (graceful degradation for tests /
            # local CLI runs / environments before F2 wired the env
            # var). Checked BEFORE the silent-hang detector so a
            # wall-clock-killed task doesn't spend an extra tick
            # waiting on a CloudWatch round-trip.
            wall_clock_result = self._maybe_reap_wall_clock(
                agent_id=str(agent_id),
                task_arn=str(task_arn),
                issue_number=issue_num,
                started_at=started_at,
                cap_seconds=wall_clock_seconds,
            )
            if wall_clock_result is not None:
                transitions.append(wall_clock_result)
                continue
            # RUNNING — silent-hang detector (C4, #3881).
            silent_hang_result = self._maybe_reap_silent_hang(
                agent_id=str(agent_id),
                task_arn=str(task_arn),
                issue_number=issue_num,
                threshold_seconds=silent_hang_seconds,
            )
            if silent_hang_result is not None:
                transitions.append(silent_hang_result)
        return len(rows), transitions

    def _resolve_stopped_task(
        self,
        *,
        agent_id: str,
        task_arn: str,
        issue_number: int,
        desc: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve a STOPPED ECS task to a terminal agent status.

        Extracts exit state from the ECS task description, marks the agent
        row terminal, and (on failure) inline-spawns the diagnoser. Returns
        the transition dict to be appended to the tick's transition list.
        """
        exit_code, exit_reason = self._extract_exit_state(desc)
        new_status = "succeeded" if exit_code == 0 else "failed"
        self._mark_agent_terminal(
            agent_id=agent_id,
            issue_number=issue_number,
            status=new_status,
            exit_code=exit_code,
            exit_reason=exit_reason,
        )
        transition: dict[str, Any] = {
            "agent_id": agent_id,
            "status": new_status,
            "exit_code": exit_code,
            "exit_reason": exit_reason,
        }
        # On failure, inline-launch the diagnoser (cap of 1 per agent
        # enforced inside _launch_diagnoser via re-read of the row).
        if new_status == "failed":
            diagnoser_arn = self._launch_diagnoser(agent_id=agent_id)
            if diagnoser_arn:
                transition["diagnoser_arn"] = diagnoser_arn
        return transition

    def _maybe_reap_silent_hang(
        self,
        *,
        agent_id: str,
        task_arn: str,
        issue_number: int,
        threshold_seconds: float,
    ) -> Optional[dict[str, Any]]:
        """Reap a RUNNING task if its CloudWatch log stream has gone silent.

        Returns ``None`` if the silent-hang detector is disabled (no log
        group configured) or the task is not yet stale. Returns the
        transition dict when the task is reaped.
        """
        if not self._task_runner_log_group:
            return None
        stale = self._task_session_is_silent(
            task_arn=task_arn,
            threshold_seconds=threshold_seconds,
        )
        if not stale:
            return None
        # Stale: stop the task and mark the agent failed.
        self._stop_task_for_silent_hang(agent_id=agent_id, task_arn=task_arn)
        self._mark_agent_terminal(
            agent_id=agent_id,
            issue_number=issue_number,
            status="failed",
            exit_code=None,
            exit_reason=EXIT_REASON_SILENT_HANG,
        )
        log.warning(
            "launcher.silent_hang_reaped",
            extra={
                "event": "silent_hang_reaped",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "task_arn": task_arn,
                "threshold_seconds": threshold_seconds,
            },
        )
        transition: dict[str, Any] = {
            "agent_id": agent_id,
            "status": "failed",
            "exit_code": None,
            "exit_reason": EXIT_REASON_SILENT_HANG,
        }
        # Silent-hang reaped agents are non-success terminals; run the
        # diagnoser (cap of 1 enforced inside _launch_diagnoser via
        # re-read of the row's diagnoser_arn).
        diagnoser_arn = self._launch_diagnoser(agent_id=agent_id)
        if diagnoser_arn:
            transition["diagnoser_arn"] = diagnoser_arn
        return transition

    def _maybe_reap_wall_clock(
        self,
        *,
        agent_id: str,
        task_arn: str,
        issue_number: int,
        started_at: Any,
        cap_seconds: int,
    ) -> Optional[dict[str, Any]]:
        """Reap a RUNNING task if its age exceeds the wall-clock cap.

        Returns ``None`` when the wall-clock check is disabled
        (``cap_seconds <= 0``), the row has no ``started_at``, or the
        task is not yet over the cap. Returns the transition dict when
        the task is reaped.

        See :func:`_watch_in_flight` for the wall-clock-cap rationale
        and #3940 for the F2 task-def stopTimeout incident that drove
        this enforcement into the launcher.
        """
        if cap_seconds <= 0 or started_at is None:
            return None
        if not self._task_age_exceeds_cap(
            started_at=started_at,
            cap_seconds=cap_seconds,
        ):
            return None
        self._stop_task_for_wall_clock(agent_id=agent_id, task_arn=task_arn)
        self._mark_agent_terminal(
            agent_id=agent_id,
            issue_number=issue_number,
            status="failed",
            exit_code=None,
            exit_reason=EXIT_REASON_WALL_CLOCK_EXCEEDED,
        )
        log.warning(
            "launcher.wall_clock_exceeded_reaped",
            extra={
                "event": "wall_clock_exceeded_reaped",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "issue_number": issue_number,
                "task_arn": task_arn,
                "cap_seconds": cap_seconds,
            },
        )
        transition: dict[str, Any] = {
            "agent_id": agent_id,
            "status": "failed",
            "exit_code": None,
            "exit_reason": EXIT_REASON_WALL_CLOCK_EXCEEDED,
        }
        # Wall-clock reaped agents are non-success terminals; run the
        # diagnoser (cap of 1 enforced inside _launch_diagnoser via
        # re-read of the row's diagnoser_arn).
        diagnoser_arn = self._launch_diagnoser(agent_id=agent_id)
        if diagnoser_arn:
            transition["diagnoser_arn"] = diagnoser_arn
        return transition

    @staticmethod
    def _extract_exit_state(desc: dict[str, Any]) -> tuple[Optional[int], str]:
        """Pull ``exitCode`` and ``stoppedReason`` from an ECS Task.

        The agent-runner container is the only one in v3's task-runner
        task-def, so we read its first ``containers[0].exitCode``. A
        ``None`` exit code (rare but possible — task killed before
        container exited) is normalized to a non-zero sentinel
        (``-1``) so the transition resolves to ``failed``.
        """
        containers = desc.get("containers") or []
        exit_code: Optional[int] = None
        if containers and isinstance(containers[0], dict):
            raw = containers[0].get("exitCode")
            if isinstance(raw, int):
                exit_code = raw
        if exit_code is None:
            exit_code = -1
        stopped_reason = str(desc.get("stoppedReason") or "")
        return exit_code, stopped_reason

    def _describe_tasks(self, arns: list[str]) -> list[dict[str, Any]]:
        """Wrap ``ecs:DescribeTasks`` (best-effort, paginated 100/req)."""
        if not arns:
            return []
        try:
            client = self._get_ecs_client()
        except Exception:
            log.exception(
                "launcher.ecs_client_init_failed",
                extra={"event": "ecs_client_init_failed", "run_id": self._run_id},
            )
            return []
        results: list[dict[str, Any]] = []
        # AWS DescribeTasks max 100 ARNs per call.
        for chunk_start in range(0, len(arns), 100):
            chunk = arns[chunk_start : chunk_start + 100]
            try:
                resp = client.describe_tasks(
                    cluster=self._ecs_cluster_arn,
                    tasks=chunk,
                )
            except Exception:
                log.exception(
                    "launcher.describe_tasks_failed",
                    extra={
                        "event": "describe_tasks_failed",
                        "run_id": self._run_id,
                        "chunk_size": len(chunk),
                    },
                )
                continue
            tasks = resp.get("tasks") or []
            for t in tasks:
                if isinstance(t, dict):
                    results.append(t)
        return results

    # -- silent-hang detection (issue #3881) ------------------------------

    def _task_session_is_silent(
        self,
        *,
        task_arn: str,
        threshold_seconds: int,
    ) -> bool:
        """True iff the task's CW log stream is older than threshold.

        Calls ``cloudwatch_logs:DescribeLogStreams`` filtered to the
        single stream named ``<prefix>/<container>/<task-id>`` and
        reads ``lastEventTimestamp`` (ms since epoch). The check is
        side-effect-free and returns False on any error path so a
        transient CW API blip never reaps a healthy agent.

        False-paths (return False, do not reap):

        - Stream not found yet (just-launched task — the awslogs driver
          creates the stream on first event, which can lag the ECS
          ``RUNNING`` transition by several seconds).
        - ``lastEventTimestamp`` missing from the response (newly
          created stream, no events yet).
        - CW API call raises (rate limit, IAM, network).

        True-path (return True, reap):

        - Stream exists, has a ``lastEventTimestamp``, and the gap
          between now and that timestamp exceeds ``threshold_seconds``.
        """
        stream_name = self._build_log_stream_name(task_arn)
        if not stream_name:
            return False
        last_event_ms = self._fetch_last_event_timestamp(stream_name)
        if last_event_ms is None:
            return False
        # ``time.time()`` is wall-clock, ``last_event_ms`` is epoch ms
        # (CloudWatch's documented unit for the field). Both come from
        # the same time domain so the subtraction is meaningful.
        now_ms = int(time.time() * 1000)
        gap_ms = now_ms - last_event_ms
        threshold_ms = threshold_seconds * 1000
        return gap_ms >= threshold_ms

    def _build_log_stream_name(self, task_arn: str) -> str:
        """Reconstruct the awslogs stream name for a task ARN.

        ECS awslogs driver names streams as
        ``<prefix>/<container-name>/<task-id>`` where ``task-id`` is
        the last URI segment of the task ARN. The container name is
        fixed to ``task-runner`` (set in
        :meth:`_run_task_for_agent`'s ``containerOverrides``). Returns
        an empty string if the ARN is malformed (no segments).
        """
        if not task_arn:
            return ""
        # Task ARN format: arn:aws:ecs:<region>:<acct>:task/<cluster>/<task-id>
        # We take the final segment regardless of cluster prefix.
        task_id = task_arn.rsplit("/", 1)[-1]
        if not task_id or task_id == task_arn:
            return ""
        return f"{self._task_runner_log_stream_prefix}/task-runner/{task_id}"

    def _fetch_last_event_timestamp(self, stream_name: str) -> Optional[int]:
        """Return the stream's ``lastEventTimestamp`` (ms) or None.

        ``DescribeLogStreams`` with ``logStreamNamePrefix`` returns at
        most ``limit`` items; we set ``limit=1`` and rely on an exact
        prefix match to be safe even if the awslogs driver ever adds
        suffixes. None on any non-happy path so the caller's "skip the
        reap on error" guard fires.
        """
        try:
            client = self._get_cloudwatch_logs_client()
        except Exception:
            log.exception(
                "launcher.cw_client_init_failed",
                extra={"event": "cw_client_init_failed", "run_id": self._run_id},
            )
            return None
        try:
            resp = client.describe_log_streams(
                logGroupName=self._task_runner_log_group,
                logStreamNamePrefix=stream_name,
                limit=1,
            )
        except Exception:
            log.exception(
                "launcher.describe_log_streams_failed",
                extra={
                    "event": "describe_log_streams_failed",
                    "run_id": self._run_id,
                    "log_stream_prefix": stream_name,
                },
            )
            return None
        streams = resp.get("logStreams") or []
        if not streams:
            return None
        first = streams[0]
        if not isinstance(first, dict):
            return None
        # Defend against a same-prefix collision: the first returned
        # stream must have the exact name we asked for, otherwise we
        # treat it as "stream not found yet" (False).
        if str(first.get("logStreamName") or "") != stream_name:
            return None
        ts = first.get("lastEventTimestamp")
        if not isinstance(ts, int):
            return None
        return ts

    def _read_silent_hang_minutes(self) -> int:
        """Read ``dispatcher.config.silent_hang_minutes``; default to 30.

        Returns the threshold in **minutes** (not seconds). Conversion
        to seconds happens at the comparison site so debugging logs
        carry the more readable minutes value. Negative or non-int
        values fall back to the default.
        """
        if self._conn is None:
            return DEFAULT_SILENT_HANG_MINUTES
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("silent_hang_minutes",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return DEFAULT_SILENT_HANG_MINUTES
        if row is None or row[0] is None:
            return DEFAULT_SILENT_HANG_MINUTES
        try:
            value = int(row[0])
        except (TypeError, ValueError):
            return DEFAULT_SILENT_HANG_MINUTES
        if value <= 0:
            return DEFAULT_SILENT_HANG_MINUTES
        return value

    def _stop_task_for_silent_hang(self, *, agent_id: str, task_arn: str) -> None:
        """Best-effort ``ecs:StopTask`` on a silent-hang reap.

        A failure here is logged but does not block the DB transition —
        the row will be marked failed regardless, and the next tick's
        watch loop will resolve the task to STOPPED via the standard
        path once ECS catches up.
        """
        try:
            client = self._get_ecs_client()
            client.stop_task(
                cluster=self._ecs_cluster_arn,
                task=task_arn,
                reason=f"silent_hang:{agent_id}",
            )
        except Exception:
            log.exception(
                "launcher.silent_hang_stop_failed",
                extra={
                    "event": "silent_hang_stop_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "task_arn": task_arn,
                },
            )

    @staticmethod
    def _task_age_exceeds_cap(*, started_at: Any, cap_seconds: int) -> bool:
        """True iff the agent's task is older than the wall-clock cap.

        ``started_at`` arrives as whatever the DB driver hands back for
        a ``timestamptz`` column — typically a ``datetime`` (psycopg
        default) but tests pass plain UTC ``datetime`` objects too. We
        compute the elapsed seconds in UTC; a naive ``datetime`` is
        treated as already-UTC (matching the v2 daemon's convention).

        Returns False on any unparseable input (defense-in-depth: a
        misshapen DB row must not flip a healthy agent to failed).
        """
        # Lazy-import datetime to keep module-import side effects minimal.
        from datetime import datetime, timezone  # noqa: PLC0415 — lazy

        if not isinstance(started_at, datetime):
            return False
        # Normalize to UTC. A naive datetime is treated as UTC (matches
        # the convention v2's _silent_hang_check uses for now() values).
        if started_at.tzinfo is None:
            started_at_utc = started_at.replace(tzinfo=timezone.utc)
        else:
            started_at_utc = started_at.astimezone(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        elapsed = (now_utc - started_at_utc).total_seconds()
        return elapsed >= cap_seconds

    def _stop_task_for_wall_clock(self, *, agent_id: str, task_arn: str) -> None:
        """Best-effort ``ecs:StopTask`` on a wall-clock-cap reap.

        Mirrors :meth:`_stop_task_for_silent_hang` -- a failure here is
        logged but does not block the DB transition. The row will be
        marked failed regardless, and the next tick's watch loop will
        resolve the task to STOPPED via the standard path once ECS
        catches up. Distinct ``reason`` string so CloudTrail records
        which kill class fired.
        """
        try:
            client = self._get_ecs_client()
            client.stop_task(
                cluster=self._ecs_cluster_arn,
                task=task_arn,
                reason=f"wall_clock_exceeded:{agent_id}",
            )
        except Exception:
            log.exception(
                "launcher.wall_clock_stop_failed",
                extra={
                    "event": "wall_clock_stop_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "task_arn": task_arn,
                },
            )

    def _mark_agent_terminal(
        self,
        *,
        agent_id: str,
        issue_number: int,
        status: str,
        exit_code: Optional[int],
        exit_reason: str,
    ) -> None:
        """Persist the terminal state for a v3 agent and release the label."""
        assert self._conn is not None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = %s, ended_at = now(), "
                    "    exit_code = %s, exit_reason = %s "
                    "WHERE agent_id = %s",
                    (status, exit_code, exit_reason, agent_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.mark_terminal_failed",
                extra={
                    "event": "mark_terminal_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "target_status": status,
                },
            )
            self._safe_rollback()
            return
        # Release the in-progress label so the issue becomes re-claimable.
        # Best-effort — a label-remove failure does not block the
        # transition (the row is already terminal in the DB).
        if issue_number:
            self._gh_remove_labels(issue_number, [LABEL_STATUS_IN_PROGRESS])
        # Feed the v3 circuit breaker (#3883). Append the outcome row
        # and evaluate the rolling window. A breaker failure here is
        # logged but does not block the terminal transition — the
        # row is already updated in the DB by this point.
        try:
            self._breaker.record_and_evaluate(agent_id=agent_id, status=status)
        except Exception:
            log.exception(
                "launcher.breaker_eval_failed",
                extra={
                    "event": "breaker_eval_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "status": status,
                },
            )

    # -- step 3b: diagnoser invocation (issue #3882, spec §4.2) -----------

    def _launch_diagnoser(self, *, agent_id: str) -> Optional[str]:
        """Launch a diagnoser ECS task for *agent_id*; return the task ARN.

        Cap of 1 enforced by an atomic re-read inside this method: if
        ``diagnoser_arn`` is already set on the row (any prior diagnoser
        attempt — running or completed), no new diagnoser is launched
        and the method returns None. Per spec §4.2: "if the diagnoser
        exits non-zero, OOMs, or is reclaimed → status='needs_review';
        the next claim of the same issue bypasses the diagnoser
        entirely on its first failure — there's no recursion of
        'diagnose the diagnoser.'"

        The diagnoser task definition family name is taken from the
        constructor's ``diagnoser_task_definition`` argument (env var
        ``DIAGNOSER_TASK_DEFINITION`` in production). The diagnoser
        container's entrypoint runs ``claude -p "/diagnose-failure
        $AGENT_ID"`` against the same image as the task-runner; the
        ``AGENT_ID`` env var is the only override the launcher passes.

        Returns None on cap-of-1 short-circuit, on RunTask failure
        (logged via :func:`log.exception`), or when no diagnoser
        task-def is configured. The caller treats None as a benign
        outcome — the row stays ``status='failed'`` without a diagnoser,
        and a future re-attempt on the same issue will not loop on
        diagnosis.
        """
        if self._conn is None:
            return None
        if not self._diagnoser_task_definition:
            # Configuration sanity: no diagnoser task-def wired up.
            # F2 (#3887) hasn't landed yet in the dev environment, or
            # the operator has explicitly unset the env var. Log + skip.
            log.warning(
                "launcher.diagnoser_task_def_unset",
                extra={
                    "event": "diagnoser_task_def_unset",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            return None

        # Cap-of-1 re-check: read the row's current diagnoser_arn and
        # bail if any prior diagnoser exists. Done as a fresh SELECT
        # rather than relying on the watch-loop's row data so a
        # concurrent diagnoser launch from a hypothetical second tick
        # cannot race past the gate.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT diagnoser_arn FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.diagnoser_cap_check_failed",
                extra={
                    "event": "diagnoser_cap_check_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._safe_rollback()
            return None
        if row is not None and row[0] is not None:
            log.info(
                "launcher.diagnoser_skipped_capped",
                extra={
                    "event": "diagnoser_skipped_capped",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "existing_diagnoser_arn": str(row[0]),
                },
            )
            return None

        # ecs:RunTask the diagnoser. Same network configuration as the
        # task-runner (private subnets, internal SG); the only env
        # override is AGENT_ID — the diagnoser SKILL reads everything
        # else from the DB row + S3 transcript.
        try:
            client = self._get_ecs_client()
        except Exception:
            log.exception(
                "launcher.diagnoser_client_init_failed",
                extra={
                    "event": "diagnoser_client_init_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            return None

        env_pairs = [
            {"name": "AGENT_ID", "value": agent_id},
        ]
        overrides = {
            "containerOverrides": [{"name": "diagnoser", "environment": env_pairs}]
        }
        network_configuration = {
            "awsvpcConfiguration": {
                "subnets": list(self._agent_runner_subnet_ids),
                "securityGroups": [self._agent_runner_security_group_id],
                "assignPublicIp": "DISABLED",
            }
        }
        tags = [
            {"key": "agent_id", "value": agent_id},
            {"key": "dispatcher_run_id", "value": self._run_id},
            {"key": "dispatcher_version", "value": "v3"},
            {"key": "kind", "value": "diagnoser"},
        ]
        try:
            response = client.run_task(
                cluster=self._ecs_cluster_arn,
                taskDefinition=self._diagnoser_task_definition,
                launchType="FARGATE",
                count=1,
                overrides=overrides,
                networkConfiguration=network_configuration,
                tags=tags,
                propagateTags="TASK_DEFINITION",
                enableExecuteCommand=True,
            )
        except Exception:
            log.exception(
                "launcher.diagnoser_run_task_raised",
                extra={
                    "event": "diagnoser_run_task_raised",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            return None
        failures = response.get("failures") or []
        if failures:
            log.warning(
                "launcher.diagnoser_run_task_failures",
                extra={
                    "event": "diagnoser_run_task_failures",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "failures": failures,
                },
            )
            return None
        tasks = response.get("tasks") or []
        if not tasks:
            return None
        arn = tasks[0].get("taskArn")
        if not arn:
            return None
        diagnoser_arn = str(arn)

        # Persist the ARN. A failure here is logged; the row keeps
        # status='failed' without diagnoser_arn — the diagnoser is
        # already running but the launcher has no way to find it. The
        # diagnoser will exit on its own (it doesn't need the launcher
        # to operate).
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET diagnoser_arn = %s "
                    "WHERE agent_id = %s",
                    (diagnoser_arn, agent_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.diagnoser_arn_update_failed",
                extra={
                    "event": "diagnoser_arn_update_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "diagnoser_arn": diagnoser_arn,
                },
            )
            self._safe_rollback()
            # Fall through — the diagnoser is launched, just not tracked.

        log.info(
            "launcher.diagnoser_launched",
            extra={
                "event": "diagnoser_launched",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "diagnoser_arn": diagnoser_arn,
            },
        )
        return diagnoser_arn

    def _watch_diagnosers(self) -> tuple[int, list[dict[str, Any]]]:
        """Resolve in-flight diagnoser tasks.

        The watch predicate is ``diagnoser_arn IS NOT NULL AND
        status = 'failed' AND outcome_summary IS NULL``:

        - ``diagnoser_arn IS NOT NULL`` — there is (or was) a diagnoser
          for this agent.
        - ``status = 'failed'`` — the agent has not yet been bumped to
          ``needs_review`` (the diagnoser-failed terminal). Once we
          observe a non-zero diagnoser exit and bump status, the row
          drops out of this scan.
        - ``outcome_summary IS NULL`` — neither the diagnoser SKILL nor
          our STOPPED-0 fallback has written a sentinel yet. Once a
          sentinel is written, the row drops out of this scan even if
          the diagnoser ECS task lingers in STOPPED state.

        Together these gate-against re-scanning a row whose diagnoser
        has already been resolved. STOPPED-0 path: the diagnoser SKILL
        wrote ``outcome_summary`` itself before exiting cleanly — the
        launcher writes a fallback sentinel iff the column is still
        null, so the next tick's predicate excludes the row. STOPPED-
        non-zero path: status flips to ``needs_review``, the predicate
        excludes the row.
        """
        if self._conn is None:
            return 0, []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT agent_id, diagnoser_arn FROM dispatcher.agents "
                    "WHERE diagnoser_arn IS NOT NULL "
                    "  AND status = 'failed' "
                    "  AND outcome_summary IS NULL "
                    f"  AND {V3_SCOPED_PARENT_RUN_FILTER}",
                )
                rows = list(cur.fetchall() or [])
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.diagnoser_watch_scan_failed",
                extra={
                    "event": "diagnoser_watch_scan_failed",
                    "run_id": self._run_id,
                },
            )
            self._safe_rollback()
            return 0, []

        if not rows:
            return 0, []

        diagnoser_arns = [str(r[1]) for r in rows]
        descriptions = self._describe_tasks(diagnoser_arns)
        by_arn: dict[str, dict[str, Any]] = {}
        for desc in descriptions:
            arn = desc.get("taskArn")
            if isinstance(arn, str):
                by_arn[arn] = desc

        transitions: list[dict[str, Any]] = []
        for agent_id, diagnoser_arn in rows:
            desc = by_arn.get(str(diagnoser_arn))
            if desc is None:
                continue
            last_status = str(desc.get("lastStatus") or "")
            if last_status != "STOPPED":
                continue
            exit_code, exit_reason = self._extract_exit_state(desc)
            if exit_code == 0:
                # Spec §4.2: "no further action — the diagnoser already
                # wrote outcome_summary." Only write a fallback sentinel
                # if the diagnoser somehow exited 0 without writing
                # outcome_summary (defensive: stops the watch query
                # from re-matching this row forever).
                self._mark_diagnoser_succeeded(agent_id=str(agent_id))
                transitions.append(
                    {
                        "agent_id": str(agent_id),
                        "diagnoser_status": "succeeded",
                    }
                )
            else:
                # STOPPED-non-zero: bump agent to needs_review. The
                # operator inspects from the cockpit; the per-issue
                # claim budget governs whether a future ready-flip
                # re-claims the issue. The Telegram alert is now
                # emitted inside ``_mark_agent_needs_review`` (#3883).
                self._mark_agent_needs_review(
                    agent_id=str(agent_id),
                    diagnoser_exit_code=exit_code,
                    diagnoser_exit_reason=exit_reason,
                )
                transitions.append(
                    {
                        "agent_id": str(agent_id),
                        "diagnoser_status": "failed",
                        "diagnoser_exit_code": exit_code,
                        "diagnoser_exit_reason": exit_reason,
                    }
                )
        return len(rows), transitions

    def _mark_diagnoser_succeeded(self, *, agent_id: str) -> None:
        """Write a fallback ``outcome_summary`` when diagnoser STOPPED-0.

        Defensive only: under spec §4.2 the diagnoser SKILL writes its
        own ``outcome_summary`` before exiting cleanly. If somehow the
        diagnoser exited 0 without writing the column, this fallback
        keeps the watch predicate (``outcome_summary IS NULL``) from
        re-matching the row forever. We use ``COALESCE`` so a real
        diagnoser write is never overwritten.
        """
        if self._conn is None:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET outcome_summary = COALESCE(outcome_summary, %s) "
                    "WHERE agent_id = %s",
                    ("diagnoser_completed", agent_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.diagnoser_success_mark_failed",
                extra={
                    "event": "diagnoser_success_mark_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._safe_rollback()

    def _mark_agent_needs_review(
        self,
        *,
        agent_id: str,
        diagnoser_exit_code: Optional[int],
        diagnoser_exit_reason: str,
    ) -> None:
        """Bump agent status to ``needs_review`` after a diagnoser failure.

        Records the diagnoser exit context in ``outcome_summary`` so the
        cockpit can render a useful one-liner without further DB joins.
        Per spec §4.2 the next claim of the same issue bypasses the
        diagnoser entirely on its first failure — there's no recursion;
        the per-issue claim budget is what governs further attempts.

        Also fires a Telegram alert (#3883) so the operator sees the
        needs_review transition without polling the cockpit. The DB
        write above is the authoritative state; the alert is the
        human notification, best-effort and logged separately on
        failure.
        """
        if self._conn is None:
            return
        # Look up the issue number FIRST so the Telegram message can
        # name the issue even if the UPDATE below fails. The lookup
        # is best-effort — fall through to a 0 sentinel.
        issue_number = self._lookup_issue_number(agent_id)
        # Build a compact summary string. Truncate the raw stoppedReason
        # so we don't blow past TEXT-column display widths in the cockpit.
        reason_tail = (diagnoser_exit_reason or "").strip()[:160]
        summary = (
            f"diagnoser_failed: exit={diagnoser_exit_code} {reason_tail}"
        ).strip()[:240]
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = 'needs_review', "
                    "    outcome_summary = COALESCE(outcome_summary, %s) "
                    "WHERE agent_id = %s",
                    (summary, agent_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.needs_review_mark_failed",
                extra={
                    "event": "needs_review_mark_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._safe_rollback()
            return
        log.info(
            "launcher.diagnoser_failed_needs_review",
            extra={
                "event": "diagnoser_failed_needs_review",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "diagnoser_exit_code": diagnoser_exit_code,
            },
        )
        # Fire the Telegram alert (#3883 fills the prior C5 TODO marker).
        # Best-effort — a Telegram outage / config gap is logged inside
        # the helper; the DB transition is the authoritative state.
        try:
            self._telegram_alerter(
                telegram.render_needs_review(
                    issue_number=issue_number,
                    agent_id=agent_id,
                    diagnoser_exit_code=diagnoser_exit_code,
                    diagnoser_exit_reason=diagnoser_exit_reason,
                ),
                trigger="needs_review",
                run_id=self._run_id,
            )
        except Exception:
            log.exception(
                "launcher.needs_review_telegram_failed",
                extra={
                    "event": "needs_review_telegram_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )

    def _lookup_issue_number(self, agent_id: str) -> int:
        """Fetch the agent row's ``issue_number`` (0 sentinel on miss/error)."""
        if self._conn is None:
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issue_number FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return 0
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    # -- step 4: watch in-flight diagnosers --------------------------------
    # (defined above as part of step 3b's diagnoser-invocation block —
    # ``_watch_diagnosers`` lives next to ``_launch_diagnoser`` because
    # they share the cap-of-1 invariant.)

    # -- step 5: recover partial claims -----------------------------------

    def _recover_partial_claims(self) -> int:
        """Mark stuck ``status='claiming' AND task_arn IS NULL`` rows failed."""
        if self._conn is None:
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = 'failed', ended_at = now(), "
                    "    exit_reason = 'claim_abandoned' "
                    "WHERE current_milestone = 'claiming' "
                    "  AND task_arn IS NULL "
                    "  AND ended_at IS NULL "
                    "  AND now() - started_at > make_interval(secs => %s) "
                    f"  AND {V3_SCOPED_PARENT_RUN_FILTER}",
                    (PARTIAL_CLAIM_RECOVERY_AGE_SECONDS,),
                )
                rowcount = cur.rowcount
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.recover_partial_failed",
                extra={"event": "recover_partial_failed", "run_id": self._run_id},
            )
            self._safe_rollback()
            return 0
        if rowcount > 0:
            log.info(
                "launcher.recovered_partial_claims",
                extra={
                    "event": "recovered_partial_claims",
                    "run_id": self._run_id,
                    "count": rowcount,
                },
            )
        return int(rowcount or 0)

    # -- step 6: claim if cap allows --------------------------------------

    def _claim_if_cap_allows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Scan the queue and claim up to ``concurrency_cap_v3 - running`` issues."""
        cap = self._read_concurrency_cap_v3()
        if cap <= 0:
            return [], []
        running = self._count_running_v3_agents()
        slots = cap - running
        if slots <= 0:
            return [], []
        try:
            issues = self._gh_list_ready_issues()
        except Exception:
            log.exception(
                "launcher.queue_scan_failed",
                extra={"event": "queue_scan_failed", "run_id": self._run_id},
            )
            return [], []

        claims: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        attempts_max = self._read_claim_attempts_max()

        for issue in issues:
            if slots <= 0:
                break
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            label_names = {
                e.get("name") for e in issue.get("labels", []) if isinstance(e, dict)
            }
            if LABEL_DISPATCHER_V2_ONLY in label_names:
                skipped.append({"number": number, "reason": "v2_only"})
                continue
            if not self._trust_checker(number):
                skipped.append({"number": number, "reason": "trust_check_failed"})
                continue
            attempts = self._count_prior_attempts(number)
            if attempts >= attempts_max:
                skipped.append(
                    {
                        "number": number,
                        "reason": "budget_exhausted",
                        "attempts": attempts,
                        "limit": attempts_max,
                    }
                )
                self._gh_add_labels(number, [LABEL_STATUS_NEEDS_HUMAN])
                continue
            agent_id = self._mint_agent_id()
            outcome = self._claim_one(agent_id=agent_id, issue_number=number)
            if outcome.get("ok"):
                claims.append(
                    {
                        "number": number,
                        "agent_id": agent_id,
                        "task_arn": outcome.get("task_arn"),
                    }
                )
                slots -= 1
            else:
                skipped.append(
                    {"number": number, "reason": outcome.get("reason", "unknown")}
                )
        return claims, skipped

    @staticmethod
    def _mint_agent_id() -> str:
        """Generate a fresh agent UUID. Lazy-imports uuid to keep imports tight."""
        import uuid  # noqa: PLC0415 — lazy

        return str(uuid.uuid4())

    def _claim_one(self, *, agent_id: str, issue_number: int) -> dict[str, Any]:
        """Run the atomic claim sequence for one issue.

        Returns a dict ``{ok: bool, reason?: str, task_arn?: str}``.
        See module docstring for the full sequence.
        """
        # (a) DB INSERT — the atomic primitive (UNIQUE INDEX migration 25).
        if not self._atomic_claim_insert(agent_id=agent_id, issue_number=issue_number):
            return {"ok": False, "reason": "claim_lost"}

        # (b) Add status/in-progress label.
        self._gh_add_labels(issue_number, [LABEL_STATUS_IN_PROGRESS])
        # (c) Remove agent/ready.
        self._gh_remove_labels(issue_number, [LABEL_AGENT_READY])

        # (d) ecs:RunTask.
        try:
            task_arn = self._run_task_for_agent(
                agent_id=agent_id, issue_number=issue_number
            )
        except Exception as exc:
            log.exception(
                "launcher.run_task_raised",
                extra={
                    "event": "run_task_raised",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "agent_id": agent_id,
                },
            )
            self._mark_launch_failed(
                agent_id=agent_id, issue_number=issue_number, reason=str(exc)
            )
            return {"ok": False, "reason": "launch_failed"}
        if not task_arn:
            self._mark_launch_failed(
                agent_id=agent_id, issue_number=issue_number, reason="no_task_arn"
            )
            return {"ok": False, "reason": "launch_failed"}

        # (e) UPDATE row with task_arn.
        self._update_agent_task_arn(agent_id=agent_id, task_arn=task_arn)
        log.info(
            "launcher.claim_succeeded",
            extra={
                "event": "claim_succeeded",
                "run_id": self._run_id,
                "issue_number": issue_number,
                "agent_id": agent_id,
                "task_arn": task_arn,
            },
        )
        return {"ok": True, "task_arn": task_arn}

    def _atomic_claim_insert(self, *, agent_id: str, issue_number: int) -> bool:
        """INSERT a new agent row; return True on success, False on race.

        Inserts with ``status='running'`` so the partial UNIQUE INDEX on
        ``dispatcher.agents (issue_number) WHERE status IN ('running',
        'retrying')`` (migration 25) is the atomic gate. ``current_milestone``
        is set to ``'claiming'`` so the cockpit displays "the launcher
        is wiring up the ECS task" until the task ARN lands.
        """
        assert self._conn is not None
        # Lazy-import psycopg so test environments without it can still
        # import this module — same pattern as v2's _atomic_claim.
        import psycopg  # noqa: PLC0415 — lazy

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.agents "
                    "    (agent_id, parent_run_id, kind, issue_number, "
                    "     status, current_milestone, current_milestone_at, "
                    "     started_at) "
                    "VALUES (%s, %s, 'task', %s, 'running', 'claiming', now(), now())",
                    (agent_id, self._run_id, issue_number),
                )
            self._conn.commit()
        except psycopg.errors.UniqueViolation:
            self._safe_rollback()
            log.info(
                "launcher.claim_lost",
                extra={
                    "event": "claim_lost",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "agent_id": agent_id,
                },
            )
            return False
        except Exception:
            self._safe_rollback()
            log.exception(
                "launcher.claim_failed",
                extra={
                    "event": "claim_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "agent_id": agent_id,
                },
            )
            return False
        return True

    def _run_task_for_agent(self, *, agent_id: str, issue_number: int) -> Optional[str]:
        """``ecs:RunTask`` and return the launched task ARN (or None)."""
        client = self._get_ecs_client()
        env_pairs = [
            {"name": "AGENT_ID", "value": agent_id},
            {"name": "TASK_ISSUE_NUMBER", "value": str(issue_number)},
            {"name": "RUNNER", "value": self._runner_name},
            {"name": "SESSIONS_BUCKET", "value": self._sessions_bucket},
        ]
        overrides = {
            "containerOverrides": [{"name": "task-runner", "environment": env_pairs}]
        }
        network_configuration = {
            "awsvpcConfiguration": {
                "subnets": list(self._agent_runner_subnet_ids),
                "securityGroups": [self._agent_runner_security_group_id],
                "assignPublicIp": "DISABLED",
            }
        }
        tags = [
            {"key": "agent_id", "value": agent_id},
            {"key": "issue_number", "value": str(issue_number)},
            {"key": "dispatcher_run_id", "value": self._run_id},
            {"key": "dispatcher_version", "value": "v3"},
        ]
        response = client.run_task(
            cluster=self._ecs_cluster_arn,
            taskDefinition=self._task_runner_task_definition,
            launchType="FARGATE",
            count=1,
            overrides=overrides,
            networkConfiguration=network_configuration,
            tags=tags,
            propagateTags="TASK_DEFINITION",
            enableExecuteCommand=True,
        )
        failures = response.get("failures") or []
        if failures:
            log.warning(
                "launcher.run_task_failures",
                extra={
                    "event": "run_task_failures",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "failures": failures,
                },
            )
            return None
        tasks = response.get("tasks") or []
        if not tasks:
            return None
        arn = tasks[0].get("taskArn")
        return str(arn) if arn else None

    def _update_agent_task_arn(self, *, agent_id: str, task_arn: str) -> None:
        assert self._conn is not None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET task_arn = %s, current_milestone = 'running', "
                    "    current_milestone_at = now() "
                    "WHERE agent_id = %s",
                    (task_arn, agent_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.update_task_arn_failed",
                extra={
                    "event": "update_task_arn_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._safe_rollback()

    def _mark_launch_failed(
        self, *, agent_id: str, issue_number: int, reason: str
    ) -> None:
        """Roll back the labels and DB state when ``ecs:RunTask`` fails."""
        assert self._conn is not None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE dispatcher.agents "
                    "SET status = 'failed', ended_at = now(), "
                    "    exit_reason = %s "
                    "WHERE agent_id = %s",
                    (f"launch_failed:{reason}"[:200], agent_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "launcher.mark_launch_failed_db",
                extra={
                    "event": "mark_launch_failed_db",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )
            self._safe_rollback()
        # Release the in-progress label and re-add agent/ready so the
        # next tick (or the diagnoser, post-C5) can decide whether to
        # retry.
        self._gh_remove_labels(issue_number, [LABEL_STATUS_IN_PROGRESS])

    # -- DB helpers --------------------------------------------------------

    def _read_concurrency_cap_v3(self) -> int:
        """Read ``dispatcher.config.concurrency_cap_v3``; default to 0."""
        if self._conn is None:
            return DEFAULT_CONCURRENCY_CAP_V3
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("concurrency_cap_v3",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return DEFAULT_CONCURRENCY_CAP_V3
        if row is None or row[0] is None:
            return DEFAULT_CONCURRENCY_CAP_V3
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return DEFAULT_CONCURRENCY_CAP_V3

    def _read_claim_attempts_max(self) -> int:
        """Read ``dispatcher.config.claim_attempts_max``; default to 3."""
        if self._conn is None:
            return DEFAULT_CLAIM_ATTEMPTS_MAX
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("claim_attempts_max",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return DEFAULT_CLAIM_ATTEMPTS_MAX
        if row is None or row[0] is None:
            return DEFAULT_CLAIM_ATTEMPTS_MAX
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return DEFAULT_CLAIM_ATTEMPTS_MAX

    def _count_running_v3_agents(self) -> int:
        if self._conn is None:
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM dispatcher.agents "
                    "WHERE status IN ('running', 'retrying') "
                    f"  AND {V3_SCOPED_PARENT_RUN_FILTER}",
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return 0
        if row is None:
            return 0
        return int(row[0] or 0)

    def _count_prior_attempts(self, issue_number: int) -> int:
        """Count v3 prior agent rows for the issue (across all states).

        Per-issue budget is intentionally NOT scoped by ``parent_run_id``
        — once an issue has chewed through the budget, restarting the
        launcher should not reset it. The count IS scoped to v3 so a
        v2 run history doesn't pre-burn the v3 budget. The acceptance
        criterion in #3880 calls this out explicitly.
        """
        if self._conn is None:
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM dispatcher.agents "
                    "WHERE issue_number = %s "
                    f"  AND {V3_SCOPED_PARENT_RUN_FILTER}",
                    (issue_number,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return 0
        if row is None:
            return 0
        return int(row[0] or 0)

    # -- gh CLI helpers (subprocess seam) ---------------------------------

    def _gh_list_ready_issues(self) -> list[dict[str, Any]]:
        """``gh issue list --label agent/ready`` → list of issue dicts.

        Defensively filters out issues that also carry blocking labels
        (``status/blocked``, ``status/in-progress``, ``status/needs-human``)
        even though ``gh`` would normally exclude them — same belt-and-
        suspenders posture as v2's queue scan (see ``daemon.py`` queue
        scan filter).
        """
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            self._github_repo,
            "--label",
            "agent/ready",
            "--state",
            "open",
            "--json",
            "number,title,labels,createdAt",
            "--limit",
            str(QUEUE_SCAN_PAGE_LIMIT),
        ]
        result = self._subprocess_runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=GH_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh issue list exit={result.returncode}: "
                f"{(result.stderr or '').splitlines()[:1]}"
            )
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh issue list returned invalid JSON: {exc}") from exc
        if not isinstance(issues, list):
            return []
        filtered: list[dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            labels = issue.get("labels") or []
            label_names = {e.get("name") for e in labels if isinstance(e, dict)}
            if LABEL_STATUS_BLOCKED in label_names:
                continue
            if LABEL_STATUS_IN_PROGRESS in label_names:
                continue
            if LABEL_STATUS_NEEDS_HUMAN in label_names:
                continue
            filtered.append(issue)
        return filtered

    def _gh_add_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        cmd = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self._github_repo,
            "--add-label",
            ",".join(labels),
        ]
        try:
            self._subprocess_runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:
            log.exception(
                "launcher.gh_add_labels_failed",
                extra={
                    "event": "gh_add_labels_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "labels": labels,
                },
            )

    def _gh_remove_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        cmd = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self._github_repo,
            "--remove-label",
            ",".join(labels),
        ]
        try:
            self._subprocess_runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=GH_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:
            log.exception(
                "launcher.gh_remove_labels_failed",
                extra={
                    "event": "gh_remove_labels_failed",
                    "run_id": self._run_id,
                    "issue_number": issue_number,
                    "labels": labels,
                },
            )

    # -- AWS client -------------------------------------------------------

    def _get_ecs_client(self) -> Any:
        """Return the ECS boto3 client (lazy-built; reused across ticks)."""
        if self._ecs_client is None:
            import boto3  # noqa: PLC0415 — lazy

            self._ecs_client = boto3.client("ecs")
        return self._ecs_client

    def _get_cloudwatch_logs_client(self) -> Any:
        """Return the CloudWatch Logs boto3 client (lazy-built; reused).

        Used by the silent-hang detector to read
        ``DescribeLogStreams.lastEventTimestamp`` for each in-flight
        task. Lazy-built so tests with the silent-hang detector
        disabled (empty log-group config) never instantiate boto3.
        """
        if self._cloudwatch_logs_client is None:
            import boto3  # noqa: PLC0415 — lazy

            self._cloudwatch_logs_client = boto3.client("logs")
        return self._cloudwatch_logs_client

    # -- internals --------------------------------------------------------

    def _safe_rollback(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.rollback()
        except Exception:  # pragma: no cover — best-effort
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the launcher CLI argument parser.

    The acceptance criterion explicitly calls for ``python -m
    dispatcher_v3.launcher --help`` to print usage; this parser is the
    surface that backs that.
    """
    parser = argparse.ArgumentParser(
        prog="dispatcher_v3.launcher",
        description=(
            "Dispatcher v3 launcher main loop — claim agent/ready issues "
            "and watch in-flight task-runner ECS tasks."
        ),
    )
    parser.add_argument(
        "--tick-interval",
        type=int,
        default=DEFAULT_TICK_INTERVAL_SECONDS,
        help=f"Seconds between ticks (default {DEFAULT_TICK_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit (used by integration tests).",
    )
    return parser


def _parse_int_env(name: str, default: int, *, fallback_on_zero: bool = False) -> int:
    """Parse an integer env var with a documented fallback.

    Used by :func:`_build_launcher_from_env` for the wall-clock-cap env
    vars (``TASK_RUNNER_WALL_CLOCK_SECONDS`` etc.). Empty / unset /
    non-int values fall back to ``default`` so a misconfigured launcher
    container does not crash at boot. ``fallback_on_zero=True`` adds
    "value <= 0" to the fallback set; the wall-clock-cap callers pass
    True so a typo of ``"0"`` doesn't silently disable the cap.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "launcher.env_int_parse_failed",
            extra={
                "event": "env_int_parse_failed",
                "env_name": name,
                "raw_value": raw,
                "fallback": default,
            },
        )
        return default
    if fallback_on_zero and value <= 0:
        log.warning(
            "launcher.env_int_non_positive",
            extra={
                "event": "env_int_non_positive",
                "env_name": name,
                "raw_value": raw,
                "fallback": default,
            },
        )
        return default
    return value


def _build_launcher_from_env() -> Launcher:
    """Construct a :class:`Launcher` using process env vars.

    Matches the env-var contract the v3 spec §10 lays out for the
    ``dispatcher`` ECS service. Missing required env vars raise
    :class:`KeyError` at boot time so the misconfiguration is loud.
    """
    return Launcher(
        run_id=os.environ.get("DISPATCHER_V3_RUN_ID", ""),
        github_repo=os.environ.get("GITHUB_REPO", "judgemind/judgemind"),
        ecs_cluster_arn=os.environ["ECS_CLUSTER_ARN"],
        task_runner_task_definition=os.environ["TASK_RUNNER_TASK_DEFINITION"],
        # DIAGNOSER_TASK_DEFINITION is optional at boot — F2 (#3887) is in
        # flight, and the cohabitation ramp tolerates a launcher that
        # boots without diagnoser invocation wired (cap=0 means no
        # failures fire either). When unset, ``_launch_diagnoser`` logs
        # and skips; agents still mark ``failed`` correctly.
        diagnoser_task_definition=os.environ.get("DIAGNOSER_TASK_DEFINITION", ""),
        agent_runner_subnet_ids=[
            s for s in os.environ.get("AGENT_RUNNER_SUBNET_IDS", "").split(",") if s
        ],
        agent_runner_security_group_id=os.environ["AGENT_RUNNER_SECURITY_GROUP_ID"],
        sessions_bucket=os.environ["SESSIONS_BUCKET"],
        runner_name=os.environ.get("RUNNER", "claude"),
        # Silent-hang detector (#3881). Empty TASK_RUNNER_LOG_GROUP
        # disables the detector — useful for early v3 ramp before F2
        # has wired the awslogs-group resource. The wall-clock cap below
        # remains the only liveness signal in that mode.
        task_runner_log_group=os.environ.get(
            "TASK_RUNNER_LOG_GROUP", DEFAULT_TASK_RUNNER_LOG_GROUP
        ),
        task_runner_log_stream_prefix=os.environ.get(
            "TASK_RUNNER_LOG_STREAM_PREFIX",
            DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX,
        ),
        # Wall-clock caps (#3940). F2's task-defs module renders these
        # into the launcher container's environment block as strings
        # via `tostring(var.<task>_stop_timeout_seconds)`; we parse
        # back into ints with a documented default fallback. A
        # non-positive override falls back to the default so a typo
        # like ``"0"`` doesn't silently disable the cap.
        task_runner_wall_clock_seconds=_parse_int_env(
            "TASK_RUNNER_WALL_CLOCK_SECONDS",
            DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS,
            fallback_on_zero=True,
        ),
        diagnoser_wall_clock_seconds=_parse_int_env(
            "DIAGNOSER_WALL_CLOCK_SECONDS",
            DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS,
            fallback_on_zero=True,
        ),
        scheduled_skill_wall_clock_seconds=_parse_int_env(
            "SCHEDULED_SKILL_WALL_CLOCK_SECONDS",
            DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS,
            fallback_on_zero=True,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns process exit code."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    launcher = _build_launcher_from_env()
    if args.once:
        launcher.tick()
        return 0
    launcher.run_forever(tick_interval=args.tick_interval)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via CLI
    sys.exit(main())
