# venv: none
"""Shared dispatcher-test bootstrap (#4424).

Currently the only consumer is the family of behavioural tests that
round-trip ``persist_*`` bash functions from
``scripts/dispatcher/agent-runner-entrypoint.sh`` against a local
docker-compose postgres:

  - ``test_persist_phase_output_jsonb_safety.py``
  - ``test_persist_phase_output_writes_phase_transition.py``
  - ``test_persist_ralph_patch_safety.py``

Before #4424 each of those files inlined its own copy of the
``dispatcher`` schema bootstrap. Three of the bootstraps overlapped on
``phase_outputs`` + ``phase_transitions`` + ``ralph_patches`` + the
``agents`` stub, but they drifted: when production added a second
``INSERT INTO dispatcher.phase_transitions`` to ``persist_phase_output``
(#3697), only one of the three bootstraps was updated; the other two
silently broke for ~12 days until #4418 noticed it. The CI shard that
runs these tests skips the behavioural ones (no docker postgres in CI),
so the rot was invisible until a developer ran the suite locally.

This module collapses the N copies into 1. Each test still picks its own
unique test-DB name (so cross-test leakage stays impossible), but the
schema DDL is one source of truth — the union of every column /
table the persist-* functions reference. Future production additions
break all three behavioural tests at once and are impossible to miss.

The companion structural test
``test_persist_bootstrap_mirrors_production_inserts.py`` enforces the
union-property mechanically: it greps the entrypoint for every
``INSERT INTO dispatcher.<table>`` and asserts the bootstrap creates the
table with the columns the INSERT references. That test runs in CI
without postgres and is the regression gate against the same drift
class.

The module name (``_dispatcher_test_bootstrap``) starts with an
underscore so pytest does not auto-collect it as a test file; the
prefix also signals to readers that it is a shared helper rather than a
``test_*`` test. We deliberately did NOT use ``conftest.py`` — pytest
auto-loads conftest.py for fixture / plugin discovery, but importing
fully-qualified helper symbols from it requires path acrobatics that
break when pytest's rootdir resolution changes. A plain sibling module
imported via the test directory's ``sys.path`` entry (added by pytest
during collection) is the simpler path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from typing import Final


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default DSN for the local docker-compose postgres. Mirrors the
#: per-file constants in the prior in-test bootstraps so the behaviour
#: matches exactly across the move. Override via ``TEST_DSN`` env var.
DEFAULT_DOCKER_DSN: Final[str] = (
    "postgres://judgemind:localdev@localhost:5432/judgemind"
)

#: Skip-reason used by every persist-* test that consumes the helpers
#: below. Centralised so all tests print the same message in CI logs.
DOCKER_POSTGRES_SKIP_REASON: Final[str] = (
    "local docker postgres not reachable; behavioural test skipped "
    "(schema-parity test in test_phase_outputs_insert_shape.py still runs)"
)


# ---------------------------------------------------------------------------
# Shared schema DDL — single source of truth (#4424)
# ---------------------------------------------------------------------------
#
# Union of every ``dispatcher.*`` table the persist-* test family
# touches via INSERT or UPDATE. Whenever production adds a new INSERT
# target to ``scripts/dispatcher/agent-runner-entrypoint.sh`` (or
# extends an existing INSERT's column set), this DDL is the one place
# that needs an update — every consumer test then breaks until the DDL
# catches up, instead of one consumer breaking silently while the
# others' isolated bootstraps still work.
#
# The shape is deliberately a *minimal* mirror of production migrations
# 21 (dispatcher schema base), 27 (``log_text`` column), and 38
# (``ralph_patches``):
#
#   - No FK constraints — the persist functions hard-code agent_ids
#     that won't match real seed rows.
#   - No NOT NULL on optional metering columns — the bash side omits
#     them, so production-default-NULL is the test invariant.
#   - One unique index on ``phase_outputs (agent_id, phase, attempt)``
#     so the ON CONFLICT clause inside ``persist_phase_output`` has a
#     real target to upsert against. The dedicated test DB means there
#     is no collision risk with whatever indexes the real ``dispatcher``
#     schema may also define.
#   - ``ralph_patches`` PRIMARY KEY uses ``gen_random_uuid()`` —
#     production migration 38 enables ``pgcrypto`` for the same reason;
#     docker-compose postgres ships pgcrypto by default so no extra
#     ``CREATE EXTENSION`` is needed here.
#   - ``agents`` is a STUB with only the columns the persist-*
#     UPDATEs reference (``agent_id``, ``ralph_iterations_observed``).
#     We deliberately do not mirror the full agents table — adding
#     columns there as production grows is the daemon-test family's
#     concern, not the persist-* family's.
SHARED_DISPATCHER_SCHEMA_DDL: Final[str] = textwrap.dedent(
    """
    CREATE SCHEMA IF NOT EXISTS dispatcher;

    -- phase_outputs: every persist_phase_output INSERT site writes here.
    -- Columns mirror the union of:
    --   * the no-log-text branch (agent_id, phase, output_json)
    --   * the with-log-text branch (#3694; adds log_text)
    --   * the agent_runner_reaped_failure inline INSERT (same shape)
    -- The ON CONFLICT (agent_id, phase, attempt) target requires the
    -- unique index below.
    CREATE TABLE IF NOT EXISTS dispatcher.phase_outputs (
        agent_id    uuid        NOT NULL,
        phase       text        NOT NULL,
        attempt     int         NOT NULL DEFAULT 1,
        output_json jsonb       NOT NULL,
        log_text    text,
        ts          timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX IF NOT EXISTS
        idx_phase_outputs_shared_agent_phase_attempt
        ON dispatcher.phase_outputs (agent_id, phase, attempt);

    -- phase_transitions: persist_phase_output's second INSERT (#3697 /
    -- #3695). BIGSERIAL PK with no unique on (agent_id, phase) so
    -- repeat calls within a phase append rows (the daemon's
    -- _check_stuck_agents reads MAX(ts) for per-phase progress).
    CREATE TABLE IF NOT EXISTS dispatcher.phase_transitions (
        transition_id   bigserial   PRIMARY KEY,
        agent_id        uuid        NOT NULL,
        phase           text        NOT NULL,
        ts              timestamptz NOT NULL DEFAULT now()
    );

    -- ralph_patches: persist_ralph_patch + ralph_head_watcher_persist
    -- both INSERT here. iteration_n + verdict are NULL on the
    -- persist_ralph_patch path (added by ralph_head_watcher_persist).
    CREATE TABLE IF NOT EXISTS dispatcher.ralph_patches (
        patch_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        agent_id       uuid        NOT NULL,
        issue_number   int         NOT NULL,
        patch_content  text        NOT NULL,
        commit_sha     text,
        iteration_n    int,
        verdict        text,
        created_at     timestamptz NOT NULL DEFAULT now()
    );

    -- agents stub: ralph_head_watcher_persist UPDATEs
    -- ralph_iterations_observed; persist_ralph_patch leaves it alone.
    -- This is intentionally a stub — the full table is owned by the
    -- daemon-test family.
    CREATE TABLE IF NOT EXISTS dispatcher.agents (
        agent_id                    uuid PRIMARY KEY,
        ralph_iterations_observed   int  NOT NULL DEFAULT 0
    );
    """
).strip()


# ---------------------------------------------------------------------------
# Probe + bootstrap helpers
# ---------------------------------------------------------------------------


def docker_postgres_admin_dsn() -> str | None:
    """Return a DSN if the docker-compose postgres is reachable, else None.

    Mirrors the per-test ``_docker_postgres_dsn`` helpers the persist-*
    tests previously inlined. The return value names the *admin*
    database (i.e. the docker-compose default ``judgemind`` DB) — call
    sites pass it to :func:`bootstrap_dispatcher_test_db` to derive a
    dedicated test-DB DSN.

    The probe runs ``psql -X``-with-``SELECT 1``; ``-X`` skips
    ``~/.psqlrc`` so prelude lines from a developer's local config (e.g.
    ``\\set ECHO_HIDDEN``) don't pollute the comparison against ``"1"``.

    Returns:
        DSN string when reachable, ``None`` when ``psql`` is not in
        ``PATH``, the probe times out, the connection is refused, or the
        SELECT returns anything other than ``"1"``.
    """
    if shutil.which("psql") is None:
        return None
    dsn = os.environ.get("TEST_DSN", DEFAULT_DOCKER_DSN)
    try:
        r = subprocess.run(
            ["psql", "-X", dsn, "-At", "-c", "SELECT 1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0 or r.stdout.strip() != "1":
        return None
    return dsn


def bootstrap_dispatcher_test_db(admin_dsn: str, test_db_name: str) -> str:
    """Create a dedicated test database, apply the shared schema, return its DSN.

    Idempotent — re-running against an existing test DB is a no-op (the
    schema DDL itself uses ``IF NOT EXISTS`` everywhere). Each persist-*
    test passes its own ``test_db_name`` so concurrent tests run against
    isolated DBs and share nothing.

    Args:
        admin_dsn: A working DSN against the docker-compose admin DB
            (typically the return value of
            :func:`docker_postgres_admin_dsn`).
        test_db_name: Name of the dedicated test database (e.g.
            ``"judgemind_test_persist_phase_output"``). The function
            creates it if absent.

    Returns:
        DSN pointed at ``test_db_name`` with the shared dispatcher
        schema applied.

    Raises:
        AssertionError: when the existence probe fails, the
            ``CREATE DATABASE`` returns an error other than the benign
            ``already exists`` race, or the schema DDL does not apply
            cleanly.
    """
    # Step 1: probe whether the test DB already exists. ``CREATE
    # DATABASE`` can't run inside a transaction, so we use ``-c`` which
    # auto-commits — and we only attempt the create when the probe says
    # the DB is absent (a benign race with a concurrent test creating
    # the same DB is tolerated below).
    probe = subprocess.run(
        [
            "psql",
            "-X",
            admin_dsn,
            "-At",
            "-c",
            f"SELECT 1 FROM pg_database WHERE datname='{test_db_name}';",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        raise AssertionError(f"db existence probe failed: {probe.stderr}")
    if probe.stdout.strip() != "1":
        cr = subprocess.run(
            [
                "psql",
                "-X",
                admin_dsn,
                "-c",
                f'CREATE DATABASE "{test_db_name}";',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if cr.returncode != 0 and "already exists" not in cr.stderr:
            raise AssertionError(f"create db failed: {cr.stderr}")

    # Step 2: derive the test DSN by swapping the database name on the
    # admin DSN. Both DSNs share host / port / credentials.
    test_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{test_db_name}"

    # Step 3: apply the shared DDL. ``ON_ERROR_STOP=1`` prevents psql
    # from continuing past a CREATE failure; every CREATE uses
    # ``IF NOT EXISTS`` so re-runs are no-ops.
    r = subprocess.run(
        [
            "psql",
            "-X",
            test_dsn,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            SHARED_DISPATCHER_SCHEMA_DDL,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"schema setup failed: {r.stderr}"
    return test_dsn
