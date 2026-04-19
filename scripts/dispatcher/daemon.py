#!/usr/bin/env python3
"""Dispatcher v2 daemon entrypoint — Phase 2 shadow mode.

Long-running process that reads ``dispatcher.*`` state, observes the
GitHub ``agent/ready`` queue, writes snapshots to
``dispatcher.queue_snapshots``, and emits heartbeat metrics to
CloudWatch. **Phase 2 shadow mode: no subprocess spawning** — the
``concurrency_cap=0`` guard asserts the spawn path stays dormant.

Phase 3 will add the subprocess spawn (``/task-v2-*`` agents) once
shadow-mode observations confirm the daemon is stable.

Spec: ``docs/specs/dispatcher-v2-spec.md`` §6 (scheduler loop), §7
(supervisor loop), §14 (deployment), §15 (Phase 2 definition + gate),
§17 Risk 2 (double-daemon race), §18 (schema DDL). Issue #2768 (Phase 2
epic; builds on Phase 1 skeleton from #2729).

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
#: spawn path does not exist yet (Phase 3) but the guard prevents a
#: future Phase 3 wiring mistake from activating spawn during Phase 2.
PHASE_2_REQUIRED_CONCURRENCY_CAP = 0


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
    """Phase 2 shadow-mode dispatcher daemon.

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
            - **still does NOT spawn subprocesses** (Phase 3).
        * Run the supervisor loop every ``tick_supervisor_seconds``:
            - UPDATE ``dispatcher.runs.heartbeat_ts``;
            - count recent ``dispatcher.failures`` rows;
            - publish a ``HeartbeatAge`` CloudWatch metric under the
              configured namespace (default ``Judgemind/Dispatcher``)
              so the alarm defined in the terraform module
              (``infra/terraform/modules/dispatcher-daemon``) sees fresh
              data and does not fire.
        * On SIGTERM / SIGINT, UPDATE ``dispatcher.runs.stopped_at`` and
          exit 0.

    What this daemon still does **NOT** do (deferred to Phase 3):
        * Spawn ``/task-v2-*`` subprocesses.
        * Act on the phase state machine (§6 step 5).
        * Run the failure diagnoser (§8).
    """

    def __init__(self, cfg: DaemonConfig, logger: logging.Logger):
        self._cfg = cfg
        self._log = logger
        self._conn: Connection[Any] | None = None
        self._run_id: str | None = None
        self._stop = threading.Event()
        self._scheduler_ticks = 0
        self._supervisor_ticks = 0
        self._last_heartbeat_at: datetime | None = None
        # CloudWatch client is created lazily on first publish so tests can
        # mock it via ``_make_cloudwatch_client``. Shared across supervisor
        # ticks — boto3 clients are thread-safe and reusing one avoids
        # repeated credential lookups.
        self._cloudwatch_client: Any | None = None

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
            },
        )
        return {
            "commands_consumed": commands_consumed,
            "concurrency_cap": -1 if concurrency_cap is None else concurrency_cap,
            "queue_depth": queue_depth,
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

    # ── supervisor tick (every ``tick_supervisor_seconds``) ─────────────

    def supervisor_tick(self) -> dict[str, int]:
        """Run one supervisor tick. Writes heartbeat; emits CW metric."""
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
            },
        )
        return {
            "failures_last_hour": failures_last_hour,
            "heartbeat_metric_emitted": 1 if metric_emitted else 0,
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
        # Tick once on boot so the first scheduler/supervisor cycle is
        # observable immediately in logs + DB, rather than after the
        # first full cadence elapses.
        try:
            self.scheduler_tick()
            last_scheduler = time.monotonic()
            self.supervisor_tick()
            last_supervisor = time.monotonic()
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
