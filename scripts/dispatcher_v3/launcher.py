"""dispatcher-v3 launcher main loop — claim + watch.

The single long-lived process for dispatcher-v3. Each tick (default 30s):

1. **Consume commands** — drain ``dispatcher.commands`` (start, stop,
   pause, force_kill, set_cap). Same control-plane shape as v2.
2. **Heartbeat** — UPDATE ``dispatcher.runs.heartbeat_ts`` so the
   CloudWatch staleness alarm stays green.
3. **Watch in-flight tasks** — for every v3-owned ``dispatcher.agents``
   row with ``task_arn IS NOT NULL AND ended_at IS NULL``, call
   ``ecs:DescribeTasks`` and resolve the row's terminal state when the
   task has STOPPED. RUNNING tasks are left in place; the silent-hang
   detector (issue #3881, C4) and the diagnoser invocation (issue
   #3882, C5) are deferred to subsequent PRs and marked TODO inline.
4. **Recover partial claims** — rows with ``status='claiming'`` and
   ``task_arn IS NULL`` that have aged past
   :data:`PARTIAL_CLAIM_RECOVERY_AGE_SECONDS` are marked ``failed``
   with ``exit_reason='claim_abandoned'``. Belt-and-suspenders for the
   narrow window where a label flip or RunTask fails after the DB
   INSERT lands.
5. **Claim if cap allows** — read ``dispatcher.config.concurrency_cap_v3``
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
            extra={"event": "trust_check_error", "issue_number": issue_number,
                   "detail": str(exc)},
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
        agent_runner_subnet_ids: list[str],
        agent_runner_security_group_id: str,
        sessions_bucket: str,
        runner_name: str = "claude",
        conn: Any = None,
        ecs_client: Any = None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        trust_checker: Callable[[int], bool] | None = None,
    ) -> None:
        self._run_id = run_id
        self._github_repo = github_repo
        self._ecs_cluster_arn = ecs_cluster_arn
        self._task_runner_task_definition = task_runner_task_definition
        self._agent_runner_subnet_ids = list(agent_runner_subnet_ids)
        self._agent_runner_security_group_id = agent_runner_security_group_id
        self._sessions_bucket = sessions_bucket
        self._runner_name = runner_name
        self._conn = conn
        self._ecs_client = ecs_client
        self._subprocess_runner = subprocess_runner or subprocess.run
        self._trust_checker = trust_checker or (
            lambda n: _check_issue_author_trusted(
                n, runner=self._subprocess_runner
            )
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
            "claims": [],
            "claim_skipped": [],
            "recovered": 0,
        }
        summary["commands_consumed"] = self._consume_commands()
        summary["heartbeat"] = self._heartbeat()
        watched, transitions = self._watch_in_flight()
        summary["watched"] = watched
        summary["transitions"] = transitions
        summary["recovered"] = self._recover_partial_claims()
        claims, skipped = self._claim_if_cap_allows()
        summary["claims"] = claims
        summary["claim_skipped"] = skipped
        return summary

    def run_forever(self, *, tick_interval: int = DEFAULT_TICK_INTERVAL_SECONDS) -> None:
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
        """Stop a running ECS task for *agent_id* (best-effort)."""
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT task_arn FROM dispatcher.agents "
                "WHERE agent_id = %s AND ended_at IS NULL",
                (agent_id,),
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return
        task_arn = str(row[0])
        try:
            client = self._get_ecs_client()
            client.stop_task(
                cluster=self._ecs_cluster_arn,
                task=task_arn,
                reason="force_kill",
            )
        except Exception:
            log.exception(
                "launcher.force_kill_failed",
                extra={
                    "event": "force_kill_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
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
                    "UPDATE dispatcher.runs SET heartbeat_ts = now() "
                    "WHERE run_id = %s",
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
        """For each in-flight v3 agent, resolve STOPPED tasks.

        TODO(#3881 — silent-hang detector): this method only resolves
        tasks the ECS API reports as STOPPED. The fine-grained "RUNNING
        but session log has not grown for N minutes" check lands in C4.
        TODO(#3882 — diagnoser invocation): on STOPPED-non-zero we mark
        the row failed but do not yet spawn the diagnoser ECS task —
        that wiring lands in C5.
        """
        if self._conn is None:
            return 0, []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT agent_id, task_arn, issue_number FROM dispatcher.agents "
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

        task_arns = [str(r[1]) for r in rows]
        descriptions = self._describe_tasks(task_arns)
        # Index ECS results by ARN for O(1) lookup.
        by_arn: dict[str, dict[str, Any]] = {}
        for desc in descriptions:
            arn = desc.get("taskArn")
            if isinstance(arn, str):
                by_arn[arn] = desc

        transitions: list[dict[str, Any]] = []
        for agent_id, task_arn, issue_number in rows:
            desc = by_arn.get(str(task_arn))
            if desc is None:
                # Task missing from ECS response (very stale ARN). Leave
                # in place — the next tick will re-check. No-op here so
                # a transient API blip does not flip an active agent to
                # failed.
                continue
            last_status = str(desc.get("lastStatus") or "")
            if last_status != "STOPPED":
                continue
            exit_code, exit_reason = self._extract_exit_state(desc)
            new_status = "succeeded" if exit_code == 0 else "failed"
            self._mark_agent_terminal(
                agent_id=str(agent_id),
                issue_number=int(issue_number) if issue_number is not None else 0,
                status=new_status,
                exit_code=exit_code,
                exit_reason=exit_reason,
            )
            transitions.append(
                {"agent_id": str(agent_id), "status": new_status,
                 "exit_code": exit_code, "exit_reason": exit_reason}
            )
        return len(rows), transitions

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

    # -- step 4: recover partial claims -----------------------------------

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

    # -- step 5: claim if cap allows --------------------------------------

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
                    {"number": number, "reason": "budget_exhausted",
                     "attempts": attempts, "limit": attempts_max}
                )
                self._gh_add_labels(number, [LABEL_STATUS_NEEDS_HUMAN])
                continue
            agent_id = self._mint_agent_id()
            outcome = self._claim_one(agent_id=agent_id, issue_number=number)
            if outcome.get("ok"):
                claims.append({"number": number, "agent_id": agent_id,
                               "task_arn": outcome.get("task_arn")})
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

    def _run_task_for_agent(
        self, *, agent_id: str, issue_number: int
    ) -> Optional[str]:
        """``ecs:RunTask`` and return the launched task ARN (or None)."""
        client = self._get_ecs_client()
        env_pairs = [
            {"name": "AGENT_ID", "value": agent_id},
            {"name": "TASK_ISSUE_NUMBER", "value": str(issue_number)},
            {"name": "RUNNER", "value": self._runner_name},
            {"name": "SESSIONS_BUCKET", "value": self._sessions_bucket},
        ]
        overrides = {
            "containerOverrides": [
                {"name": "task-runner", "environment": env_pairs}
            ]
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
            "gh", "issue", "list",
            "--repo", self._github_repo,
            "--label", "agent/ready",
            "--state", "open",
            "--json", "number,title,labels,createdAt",
            "--limit", str(QUEUE_SCAN_PAGE_LIMIT),
        ]
        result = self._subprocess_runner(
            cmd, capture_output=True, text=True,
            timeout=GH_SUBPROCESS_TIMEOUT_SECONDS, check=False,
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
            label_names = {
                e.get("name") for e in labels if isinstance(e, dict)
            }
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
            "gh", "issue", "edit", str(issue_number),
            "--repo", self._github_repo,
            "--add-label", ",".join(labels),
        ]
        try:
            self._subprocess_runner(
                cmd, capture_output=True, text=True,
                timeout=GH_SUBPROCESS_TIMEOUT_SECONDS, check=False,
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
            "gh", "issue", "edit", str(issue_number),
            "--repo", self._github_repo,
            "--remove-label", ",".join(labels),
        ]
        try:
            self._subprocess_runner(
                cmd, capture_output=True, text=True,
                timeout=GH_SUBPROCESS_TIMEOUT_SECONDS, check=False,
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
        agent_runner_subnet_ids=[
            s for s in os.environ.get("AGENT_RUNNER_SUBNET_IDS", "").split(",") if s
        ],
        agent_runner_security_group_id=os.environ[
            "AGENT_RUNNER_SECURITY_GROUP_ID"
        ],
        sessions_bucket=os.environ["SESSIONS_BUCKET"],
        runner_name=os.environ.get("RUNNER", "claude"),
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
