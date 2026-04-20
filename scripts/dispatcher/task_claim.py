#!/usr/bin/env python3
"""Shared ``dispatcher.agents`` claim ledger — write path for the ``/task`` skill.

The dispatcher v2 daemon writes to ``dispatcher.agents`` on every claim via
:meth:`scripts.dispatcher.daemon.DispatcherDaemon._atomic_claim`. That write
is what the partial UNIQUE INDEX
``idx_dispatcher_agents_active_issue`` (migration 25) protects — at most one
``status IN ('running', 'retrying')`` row per ``issue_number``.

Historically the ``/task`` skill (operator-spawned subagents) did **not**
write to that table. The result was a race: operator spawns ``/task #N`` on
their laptop; seconds later the Fargate daemon's queue scan independently
claims the same issue and runs its own plan → ralph → summary pipeline.
Observed 2026-04-19 with issue #2856. Issue #2866 closes that gap.

This module is the write-side helper the ``/task`` skill calls to:

1. ``claim`` — INSERT a row with ``kind='task-skill'`` + ``status='running'``.
   The same partial UNIQUE INDEX that catches daemon↔daemon races also
   catches daemon↔/task (and /task↔/task) races in either direction — if
   the INSERT fails with a UniqueViolation, the helper looks up the row
   that owns the index slot and surfaces the conflicting owner's
   ``kind`` + ``agent_id`` so the skill can emit a clear
   "already being worked on by <kind> agent <uuid>" error.
2. ``terminal`` — UPDATE the row to ``status='succeeded'`` or
   ``status='failed'`` + ``ended_at=now()`` on completion. Releases the
   index slot so a retry of the same issue can claim again.

``kind='task-skill'`` distinguishes operator-spawned /task rows from
daemon-spawned ``kind='task'`` rows so the daemon can log
``candidate_skipped reason='already_claimed_by_task'`` when it observes one
in its queue scan, and the admin cockpit can render a distinguishing chip.

Database access
---------------
The /task skill can run in two environments:

* **Operator laptop** — no direct network path to the dev VPC's RDS. The
  helper shells out to ``scripts/dev-db-query.sh --rw "<SQL>"`` which runs
  the SQL inside the already-running ingestion-worker container via ECS
  Exec. Handles both SELECT and non-SELECT statements. Latency is the SSM
  session cost (~2-4s).
* **Fargate /task** — ``DATABASE_URL`` is present in the environment.
  The helper imports ``psycopg`` and talks to RDS directly. This is
  the same path :class:`emit_failure.py` uses.

Which path runs is chosen by a single env-var check: if ``DATABASE_URL`` is
set, use psycopg; otherwise use the ``dev-db-query.sh`` wrapper. Callers
can force the wrapper path by unsetting ``DATABASE_URL``.

Usage
-----

::

    # Claim — the helper generates a uuid4() and writes it to
    # ``<worktree>/.task-agent-id`` so ``terminal`` can recover it
    # without the caller threading state. Exit 0 on success, 2 on
    # duplicate, 3 on configuration error, 1 on other error.
    scripts/dispatcher/task_claim.py claim \\
        --issue 2866 \\
        --worktree-path /Users/you/.../.claude/worktrees/agent-aabbccdd

    # Terminal update — reads agent_id from the sidecar. Exit 0 always
    # (best-effort) unless the sidecar is missing and --agent-id is not
    # supplied (exit 3).
    scripts/dispatcher/task_claim.py terminal \\
        --worktree-path /Users/you/.../.claude/worktrees/agent-aabbccdd \\
        --status succeeded

    # You may still pass ``--agent-id`` explicitly (must be a UUID):
    scripts/dispatcher/task_claim.py terminal \\
        --agent-id aabbccdd-eeff-0011-2233-445566778899 \\
        --status succeeded

Exit codes
----------
* ``0`` — claim succeeded / terminal update succeeded (or silently
  swallowed on DB failure — never block the /task skill's flow).
* ``1`` — unexpected error (subprocess missing, DB error on claim).
  stderr contains the detail.
* ``2`` — claim lost to a prior owner (UniqueViolation). stderr and stdout
  contain a JSON object with ``owner_agent_id``, ``owner_kind``,
  ``owner_status``, ``issue_number``.
* ``3`` — configuration error: caller passed a non-UUID ``--agent-id``,
  missing sidecar on ``terminal``, or the DB returned
  ``InvalidTextRepresentation`` on a column-type mismatch. Distinct
  from ``1`` so the /task skill doesn't treat a caller bug as a
  transient DB error (issue #2892).
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

#: Dedicated CLI identifier that tags /task-skill-originated claim rows.
#: Rows the daemon writes use ``kind='task'`` (or future ``'v2-per-phase'``);
#: /task writes use this value so the daemon's collision-detection path
#: can emit a distinguishing ``already_claimed_by_task`` structured log.
TASK_SKILL_KIND = "task-skill"

#: Valid terminal statuses the /task skill can report. Matches the set
#: the daemon's :meth:`_mark_agent_terminal` accepts (minus the
#: dispatcher-specific ``crashed`` and ``plan_blocked``, which are daemon-
#: side concepts). Any value outside this set is rejected with exit 1.
VALID_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})

#: Subprocess timeout for the ``scripts/dev-db-query.sh`` wrapper. ECS
#: Exec SSM sessions typically take 2-4s; 60s gives generous headroom
#: for a slow rollover.
DEV_DB_QUERY_TIMEOUT_SECONDS = 60

#: Relative path to the dev-db-query wrapper, resolved from the repo root.
DEV_DB_QUERY_SCRIPT = Path("scripts/dev-db-query.sh")

#: Sidecar filename written inside the worktree after a successful claim.
#: Stores the UUID agent_id so the ``terminal`` subcommand can recover
#: it without the caller needing to thread state across invocations.
#: The worktree path is always available to the /task skill (it's the
#: agent's cwd), so the sidecar + ``--worktree-path`` is a clean
#: handshake.
AGENT_ID_SIDECAR_NAME = ".task-agent-id"

#: Substrings in a subprocess stderr that indicate a UniqueViolation
#: against the partial unique index protecting ``dispatcher.agents``.
#: Match on the index name first (least fragile across psycopg versions),
#: fall back to the generic SQLSTATE 23505 names.
_UNIQUE_VIOLATION_MARKERS = (
    "idx_dispatcher_agents_active_issue",
    "duplicate key",
    "uniqueviolation",
)

#: Substrings in a subprocess stderr that indicate the caller handed us
#: a malformed ``agent_id`` — i.e. a configuration bug in the /task
#: skill or its caller, not a race and not a DB outage. Surfacing these
#: as a distinct exit code stops them from being silently treated as
#: "generic DB error" (which was the pre-fix failure mode — see #2892).
_CONFIGURATION_ERROR_MARKERS = (
    "invalid input syntax for type uuid",
    "invalidtextrepresentation",
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments for the two subcommands."""
    parser = argparse.ArgumentParser(
        description="Write /task skill claim + terminal rows to dispatcher.agents.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    claim = sub.add_parser("claim", help="INSERT a claim row.")
    claim.add_argument("--issue", required=True, type=int, help="Issue number.")
    claim.add_argument(
        "--agent-id",
        default=None,
        help=(
            "UUID of the /task agent. If omitted, the helper generates a "
            "fresh uuid4() and writes it to "
            "``<worktree>/" + AGENT_ID_SIDECAR_NAME + "`` for the terminal "
            "call to recover. Must be a valid UUID string when supplied; "
            "otherwise the DB INSERT fails with "
            "``invalid input syntax for type uuid`` (issue #2892)."
        ),
    )
    claim.add_argument(
        "--worktree-path",
        required=True,
        help="Absolute path to the worktree the /task skill is running in.",
    )
    claim.add_argument(
        "--issue-title",
        default=None,
        help="Optional issue title to store for admin-page rendering.",
    )

    term = sub.add_parser("terminal", help="UPDATE the row to a terminal status.")
    term.add_argument(
        "--agent-id",
        default=None,
        help=(
            "UUID of the /task agent that previously claimed. If omitted, "
            "the helper recovers it from ``<worktree>/"
            + AGENT_ID_SIDECAR_NAME
            + "`` (written by ``claim``). Either --agent-id or "
            "--worktree-path must be supplied."
        ),
    )
    term.add_argument(
        "--worktree-path",
        default=None,
        help=(
            "Absolute path to the worktree. Used to recover the agent_id "
            "from the sidecar file when --agent-id is omitted."
        ),
    )
    term.add_argument(
        "--status",
        required=True,
        choices=sorted(VALID_TERMINAL_STATUSES),
        help="Terminal status to record.",
    )
    term.add_argument(
        "--pr-number",
        default=None,
        type=int,
        help="Optional PR number to write back.",
    )

    return parser.parse_args(argv)


def _resolve_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until ``.git`` is found.

    Works from either the main repo checkout or a worktree (``.git`` is a
    file in worktrees, a directory in the main checkout — either way
    :meth:`Path.exists` returns True).
    """
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return start.resolve()


def _dev_db_query_path() -> Path:
    """Absolute path to ``scripts/dev-db-query.sh`` in the current repo."""
    here = Path(__file__).resolve()
    repo_root = _resolve_repo_root(here)
    return repo_root / DEV_DB_QUERY_SCRIPT


def _run_sql_via_db_query_sh(sql: str) -> dict[str, Any]:
    """Execute SQL via the ``dev-db-query.sh --rw`` wrapper.

    Returns the parsed JSON from stdout on success. For non-SELECT
    statements the wrapper emits ``{"rowcount": N}``; for SELECT it emits
    a list of row dicts.

    Raises :class:`RuntimeError` on subprocess failure. UniqueViolation
    and other DB errors bubble up as non-zero exit codes with the DB
    error message on stderr — the caller classifies them.
    """
    script = _dev_db_query_path()
    if not script.exists():
        raise RuntimeError(f"dev-db-query.sh not found at {script}")

    cmd = ["bash", str(script), "--rw", sql]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DEV_DB_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"bash not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"dev-db-query.sh timed out after {DEV_DB_QUERY_TIMEOUT_SECONDS}s"
        ) from exc

    if result.returncode != 0:
        # Propagate stderr — the caller classifies UniqueViolation vs
        # genuine errors by string-matching it.
        raise RuntimeError(
            f"dev-db-query.sh exit={result.returncode}: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )

    stdout = (result.stdout or "").strip()
    # The wrapper prints informational lines to stderr; stdout is the
    # JSON payload. An empty payload indicates a non-SELECT with 0
    # rowcount which we treat as "nothing happened".
    if not stdout:
        return {"rowcount": 0}

    # The wrapper streams SSM output around the JSON payload. Typical
    # stdout looks like::
    #
    #     The Session Manager plugin was installed successfully. ...
    #
    #
    #     Starting session with SessionId: ecs-execute-command-...
    #     {"rowcount": 1}
    #     Cannot perform start session: EOF
    #
    # ``json.loads(stdout)`` chokes on the preamble; the old fallback
    # (``stdout[stdout.find("{"):]``) then chokes on the trailer because
    # it's not strict JSON. Use :meth:`JSONDecoder.raw_decode` starting
    # at the first ``[`` or ``{`` to consume exactly one JSON value and
    # ignore everything after it. See issue #2892.
    return _extract_first_json_value(stdout)


class ClaimLost(Exception):
    """Raised by :func:`_claim_via_psycopg` when the partial UNIQUE INDEX rejects the INSERT."""


def _claim_via_psycopg(
    database_url: str,
    issue_number: int,
    agent_id: str,
    worktree_path: str,
    issue_title: str | None,
) -> None:
    """INSERT a claim row via psycopg. Raises :class:`ClaimLost` on UniqueViolation."""
    import psycopg  # noqa: PLC0415 — lazy import

    with psycopg.connect(database_url, connect_timeout=10) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.agents "
                    "    (agent_id, kind, issue_number, "
                    "     issue_title, worktree_path, phase, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'claiming', 'running')",
                    (
                        agent_id,
                        TASK_SKILL_KIND,
                        issue_number,
                        issue_title,
                        worktree_path,
                    ),
                )
            conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise ClaimLost(str(exc)) from exc
        except psycopg.errors.InvalidTextRepresentation as exc:
            # Caller passed a non-UUID ``agent_id`` (or a malformed
            # ``issue_number`` etc.). Surface as a configuration error
            # so ``do_claim`` can emit a distinct exit code. See #2892.
            conn.rollback()
            raise ClaimConfigurationError(str(exc)) from exc


def _lookup_owner_via_psycopg(
    database_url: str, issue_number: int
) -> dict[str, Any] | None:
    """SELECT the current ``running``/``retrying`` row for ``issue_number``.

    Returns the owner metadata dict or None when the row is no longer
    active (edge case — the partial index released between our failed
    INSERT and this lookup).
    """
    import psycopg  # noqa: PLC0415 — lazy import

    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id, kind, status, started_at "
                "FROM dispatcher.agents "
                "WHERE issue_number = %s "
                "  AND status IN ('running', 'retrying') "
                "LIMIT 1",
                (issue_number,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "owner_agent_id": str(row[0]),
        "owner_kind": row[1],
        "owner_status": row[2],
        "owner_started_at": str(row[3]),
        "issue_number": issue_number,
    }


def _claim_via_db_query_sh(
    issue_number: int,
    agent_id: str,
    worktree_path: str,
    issue_title: str | None,
) -> None:
    """INSERT a claim row via the ``dev-db-query.sh --rw`` wrapper.

    Raises :class:`ClaimLost` when the wrapper's stderr contains a
    UniqueViolation signature. Any other subprocess error bubbles up as
    :class:`RuntimeError`.
    """
    # Build a parameter-inlined INSERT. We can't pass parameters through
    # the wrapper, so values are inlined — safe because agent_id/kind are
    # constrained identifiers and worktree_path/issue_title come from
    # the /task skill (trusted caller). The wrapper's own --rw flag
    # already SETs the session to read-write; INSERT returns no rows so
    # the wrapper prints ``{"rowcount": 1}`` on success.
    title_sql = _sql_literal(issue_title) if issue_title else "NULL"
    sql = (
        "INSERT INTO dispatcher.agents "
        "(agent_id, kind, issue_number, issue_title, worktree_path, phase, status) "
        f"VALUES ({_sql_literal(agent_id)}, {_sql_literal(TASK_SKILL_KIND)}, "
        f"{int(issue_number)}, {title_sql}, {_sql_literal(worktree_path)}, "
        "'claiming', 'running')"
    )
    try:
        _run_sql_via_db_query_sh(sql)
    except RuntimeError as exc:
        msg = str(exc)
        # UniqueViolation — the partial unique index rejected the row.
        # Match on constraint name first (less fragile than the
        # human-readable prefix across psycopg versions).
        if _stderr_matches_any(msg, _UNIQUE_VIOLATION_MARKERS):
            raise ClaimLost(msg) from exc
        # Caller configuration error — e.g. passed ``agent-a5d7e546``
        # (not a UUID) for ``--agent-id``. Surface distinctly from a
        # generic DB outage so the /task skill can report "fix the
        # caller" vs. "wait for RDS to come back". See #2892.
        if _stderr_matches_any(msg, _CONFIGURATION_ERROR_MARKERS):
            raise ClaimConfigurationError(msg) from exc
        raise


def _lookup_owner_via_db_query_sh(issue_number: int) -> dict[str, Any] | None:
    """SELECT current owner via the wrapper; None if no active row."""
    sql = (
        "SELECT agent_id::text AS owner_agent_id, "
        "       kind AS owner_kind, "
        "       status AS owner_status, "
        "       started_at::text AS owner_started_at "
        "FROM dispatcher.agents "
        f"WHERE issue_number = {int(issue_number)} "
        "  AND status IN ('running', 'retrying') "
        "LIMIT 1"
    )
    try:
        rows = _run_sql_via_db_query_sh(sql)
    except RuntimeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    owner = rows[0]
    owner["issue_number"] = issue_number
    return owner


def _sql_literal(value: str) -> str:
    """Quote a string as a Postgres SQL literal.

    Only single-quotes are doubled — PostgreSQL accepts ``'it''s'`` as
    ``it's``. Backslashes are passed through because the session is not
    in ``standard_conforming_strings=off`` mode (default is on in
    Postgres ≥ 9.1). Good enough for the trusted-caller inputs the
    /task skill hands us.
    """
    if "\x00" in value:
        raise ValueError("value contains NUL byte; cannot be a SQL literal")
    return "'" + value.replace("'", "''") + "'"


def _extract_first_json_value(payload: str) -> Any:
    """Consume exactly one JSON value from the front of ``payload``.

    The ``dev-db-query.sh`` wrapper streams its output through ECS Exec,
    which adds a session preamble before the command output and a
    ``Cannot perform start session: EOF`` trailer after. We locate the
    first ``[`` or ``{`` and use :meth:`json.JSONDecoder.raw_decode` to
    read one value, ignoring anything that follows.

    Falls back to a line-by-line scan if ``raw_decode`` can't parse at
    the first candidate (handles a pathological case where a character
    before the JSON looks JSON-ish, e.g. a stray ``[`` in the preamble).

    Raises :class:`RuntimeError` when no JSON value is recoverable —
    indicates the wrapper failed to produce output, not a caller bug.
    """
    # Fast path: the whole string is valid JSON.
    stripped = payload.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()

    # Try raw_decode starting at every candidate position where a JSON
    # value could start.
    candidates: list[int] = []
    for token in ("[", "{"):
        idx = 0
        while True:
            idx = stripped.find(token, idx)
            if idx < 0:
                break
            candidates.append(idx)
            idx += 1
    candidates.sort()

    for start in candidates:
        try:
            value, _end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError:
            continue
        return value

    # Line-based fallback — a single line that is pure JSON.
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    raise RuntimeError(f"dev-db-query.sh returned non-JSON stdout: {payload[:400]}")


def _is_uuid(value: str) -> bool:
    """Return True when ``value`` parses as a valid UUID.

    ``dispatcher.agents.agent_id`` is ``uuid NOT NULL`` — any non-UUID
    input fails the INSERT with ``InvalidTextRepresentation``. We
    validate client-side so the error message is clear (caller supplied
    a bad UUID) rather than surfacing as a generic DB error (issue
    #2892).
    """
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _sidecar_path(worktree_path: str) -> Path:
    """Return the absolute path to the agent_id sidecar file.

    Kept private so the sidecar location can be centralized here — both
    ``claim`` and ``terminal`` go through this function so they always
    agree on where the file lives.
    """
    return Path(worktree_path).resolve() / AGENT_ID_SIDECAR_NAME


def _write_agent_id_sidecar(worktree_path: str, agent_id: str) -> None:
    """Write ``agent_id`` to ``<worktree>/.task-agent-id``.

    Best-effort — if the write fails (disk full, permission issue, the
    worktree directory is missing for some reason), we log to stderr
    and continue. The DB row still carries the canonical ``agent_id``;
    the sidecar is only a convenience for the ``terminal`` caller.
    Without it, the caller would have to remember and pass
    ``--agent-id`` explicitly on the terminal invocation.
    """
    path = _sidecar_path(worktree_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(agent_id + "\n", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"task_claim: failed to write agent_id sidecar at {path}: {exc}\n"
        )


def _read_agent_id_sidecar(worktree_path: str) -> str | None:
    """Read the agent_id from ``<worktree>/.task-agent-id`` if it exists.

    Returns the UUID string or None if the file is missing / unreadable
    / contents aren't a UUID. Callers treat a None return as "no
    sidecar" and fall back to raising a usage error.
    """
    path = _sidecar_path(worktree_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        sys.stderr.write(
            f"task_claim: failed to read agent_id sidecar at {path}: {exc}\n"
        )
        return None
    if not _is_uuid(content):
        sys.stderr.write(
            f"task_claim: agent_id sidecar at {path} does not contain a UUID: "
            f"{content[:80]!r}\n"
        )
        return None
    return content


def _stderr_matches_any(text: str, markers: tuple[str, ...]) -> bool:
    """Lowercase-substring match helper for subprocess error classification."""
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


class ClaimConfigurationError(Exception):
    """Raised when the caller hands the helper malformed args.

    Distinct from :class:`ClaimLost` (benign race — exit 2) and generic
    ``RuntimeError`` (DB down / subprocess broken — exit 1) so the
    /task skill surfaces a clear "skill bug, fix the agent_id" message
    rather than a silent retry.
    """


def _terminal_via_psycopg(
    database_url: str,
    agent_id: str,
    status: str,
    pr_number: int | None,
) -> int:
    """UPDATE the row to the terminal status. Returns rowcount."""
    import psycopg  # noqa: PLC0415 — lazy import

    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dispatcher.agents "
                "SET status = %s, ended_at = now(), "
                "    pr_number = COALESCE(%s, pr_number) "
                "WHERE agent_id = %s",
                (status, pr_number, agent_id),
            )
            rowcount = cur.rowcount
        conn.commit()
    return rowcount


def _terminal_via_db_query_sh(agent_id: str, status: str, pr_number: int | None) -> int:
    """UPDATE the row to the terminal status via ``dev-db-query.sh --rw``."""
    pr_clause = str(int(pr_number)) if pr_number is not None else "NULL"
    sql = (
        "UPDATE dispatcher.agents "
        f"SET status = {_sql_literal(status)}, "
        "    ended_at = now(), "
        f"    pr_number = COALESCE({pr_clause}, pr_number) "
        f"WHERE agent_id = {_sql_literal(agent_id)}"
    )
    result = _run_sql_via_db_query_sh(sql)
    if isinstance(result, dict):
        return int(result.get("rowcount", 0))
    return 0


def do_claim(
    issue_number: int,
    agent_id: str | None,
    worktree_path: str,
    issue_title: str | None,
) -> int:
    """Orchestrate the claim INSERT. Returns the process exit code.

    When ``agent_id`` is None, we generate a fresh uuid4() and write it
    to ``<worktree>/.task-agent-id`` so the later ``terminal`` call can
    recover it without the caller threading state. When explicitly
    supplied, we validate it's a UUID before issuing the INSERT so a
    bad caller argument produces a clear error rather than surfacing
    as ``invalid input syntax for type uuid`` (issue #2892).
    """
    # Generate or validate agent_id up front so an obviously-broken
    # caller gets a clear error before we round-trip to the DB.
    if agent_id is None:
        agent_id = str(uuid.uuid4())
    elif not _is_uuid(agent_id):
        sys.stderr.write(
            f"task_claim: --agent-id {agent_id!r} is not a valid UUID. "
            "Omit --agent-id to have the helper generate one, or pass a "
            "uuid4() value. The daemon's claim path uses "
            "``str(uuid.uuid4())`` — match that shape. See #2892.\n"
        )
        return 3

    database_url = os.environ.get("DATABASE_URL", "").strip()
    use_psycopg = bool(database_url)

    try:
        if use_psycopg:
            _claim_via_psycopg(
                database_url, issue_number, agent_id, worktree_path, issue_title
            )
        else:
            _claim_via_db_query_sh(issue_number, agent_id, worktree_path, issue_title)
    except ClaimLost:
        # Look up the current owner so the /task skill can tell the operator
        # who owns the issue right now.
        if use_psycopg:
            owner = _lookup_owner_via_psycopg(database_url, issue_number)
        else:
            owner = _lookup_owner_via_db_query_sh(issue_number)
        payload = owner or {"issue_number": issue_number}
        payload.setdefault("reason", "claim_lost")
        print(json.dumps(payload), file=sys.stdout)
        sys.stderr.write(
            f"claim_lost: issue #{issue_number} is already being worked on "
            f"by {payload.get('owner_kind', 'unknown')} agent "
            f"{payload.get('owner_agent_id', 'unknown')} "
            f"(status={payload.get('owner_status', 'unknown')}).\n"
        )
        return 2
    except ClaimConfigurationError as exc:
        sys.stderr.write(
            f"task_claim: configuration error — the /task skill or its "
            f"caller passed something the DB rejected at the column-type "
            f"level (likely a non-UUID ``agent_id``). Fix the caller and "
            f"retry. Underlying error: {exc}\n"
        )
        return 3
    except Exception as exc:  # noqa: BLE001 — surface any DB / subprocess error
        sys.stderr.write(f"task_claim: claim failed: {exc}\n")
        return 1

    # Claim landed — persist the agent_id for ``terminal`` to recover.
    _write_agent_id_sidecar(worktree_path, agent_id)

    print(
        json.dumps(
            {
                "status": "claimed",
                "issue_number": issue_number,
                "agent_id": agent_id,
            }
        )
    )
    return 0


def do_terminal(
    agent_id: str | None,
    status: str,
    pr_number: int | None,
    worktree_path: str | None,
) -> int:
    """Orchestrate the terminal UPDATE. Returns the process exit code.

    Terminal updates are best-effort — a failure here does not block the
    /task skill's flow (the PR has already landed; the DB row just stays
    ``running`` until the daemon's supervisor tick reconciles it). We
    still return a non-zero exit on catastrophic failure so a caller
    that WANTS to know can act on it, but the skill ignores it.

    When ``agent_id`` is None, we read ``<worktree>/.task-agent-id`` to
    recover it — written by a prior ``claim`` call. Either ``agent_id``
    or ``worktree_path`` must be supplied.
    """
    # Resolve agent_id — either from the CLI or from the sidecar.
    if agent_id is None:
        if worktree_path is None:
            sys.stderr.write(
                "task_claim: terminal requires either --agent-id or "
                "--worktree-path (to recover agent_id from the sidecar).\n"
            )
            return 3
        agent_id = _read_agent_id_sidecar(worktree_path)
        if agent_id is None:
            sys.stderr.write(
                f"task_claim: no agent_id sidecar found at "
                f"{_sidecar_path(worktree_path)} — either the claim never "
                "ran in this worktree or the file was removed. Pass "
                "--agent-id explicitly.\n"
            )
            return 3
    elif not _is_uuid(agent_id):
        sys.stderr.write(f"task_claim: --agent-id {agent_id!r} is not a valid UUID.\n")
        return 3

    database_url = os.environ.get("DATABASE_URL", "").strip()
    use_psycopg = bool(database_url)

    try:
        if use_psycopg:
            rowcount = _terminal_via_psycopg(database_url, agent_id, status, pr_number)
        else:
            rowcount = _terminal_via_db_query_sh(agent_id, status, pr_number)
    except Exception as exc:  # noqa: BLE001 — DB/subprocess surface
        sys.stderr.write(f"task_claim: terminal update failed: {exc}\n")
        return 1

    if rowcount == 0:
        sys.stderr.write(
            f"task_claim: terminal update matched 0 rows for agent_id={agent_id}\n"
        )
    print(
        json.dumps(
            {
                "status": "terminal_recorded",
                "agent_id": agent_id,
                "terminal_status": status,
                "rowcount": rowcount,
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.subcommand == "claim":
        return do_claim(
            args.issue,
            args.agent_id,
            args.worktree_path,
            args.issue_title,
        )
    if args.subcommand == "terminal":
        return do_terminal(
            args.agent_id,
            args.status,
            args.pr_number,
            args.worktree_path,
        )
    sys.stderr.write(f"Unknown subcommand: {args.subcommand}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
