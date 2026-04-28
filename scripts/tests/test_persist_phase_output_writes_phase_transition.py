# venv: none
"""Test that persist_phase_output writes a phase_transitions row (#3695).

The daemon's ``_check_stuck_agents`` uses ``COALESCE(pt.last_ts, a.started_at)``
to compute per-phase elapsed time. When no ``phase_transitions`` row exists
(the ECS norm before this fix) the reaper falls back to total agent runtime
and fires a false-positive ``agent_silent_hang`` on long-running but healthy
agents.

This test verifies that ``persist_phase_output`` now writes BOTH a
``phase_outputs`` row (idempotent via ON CONFLICT) AND a fresh
``phase_transitions`` row (BIGSERIAL — no ON CONFLICT) so the reaper always
has a per-phase timestamp to work with.

Test is skipped when no local docker postgres is reachable (CI runs the
schema-parity test in ``test_phase_outputs_insert_shape.py`` which is enough
to catch column-shape drift; this test catches the behavioural invariant for
the docker-compose dev environment specifically).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENTRYPOINT_SH = _REPO_ROOT / "scripts" / "dispatcher" / "agent-runner-entrypoint.sh"


def _docker_postgres_dsn() -> str | None:
    """Return a DSN if the docker-compose postgres is reachable, else None."""
    if shutil.which("psql") is None:
        return None
    dsn = os.environ.get(
        "TEST_DSN",
        "postgres://judgemind:localdev@localhost:5432/judgemind",
    )
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


_DSN = _docker_postgres_dsn()
pytestmark = pytest.mark.skipif(
    _DSN is None,
    reason="local docker postgres not reachable; behavioural test skipped "
    "(schema-parity test in test_phase_outputs_insert_shape.py still runs)",
)

_TEST_DB_NAME = "judgemind_test_persist_phase_transition"


def _bootstrap_test_database(admin_dsn: str) -> str:
    """Create a dedicated test database and return its DSN.

    Idempotent — re-runs of the test reuse the same database.  Both
    ``dispatcher.phase_outputs`` and ``dispatcher.phase_transitions`` are
    created in their minimal forms (no FK constraints, no extra indexes beyond
    what the bash function needs).
    """
    create = subprocess.run(
        [
            "psql",
            "-X",
            admin_dsn,
            "-At",
            "-c",
            f"SELECT 1 FROM pg_database WHERE datname='{_TEST_DB_NAME}';",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if create.returncode != 0:
        raise AssertionError(f"db existence probe failed: {create.stderr}")
    if create.stdout.strip() != "1":
        cr = subprocess.run(
            [
                "psql",
                "-X",
                admin_dsn,
                "-c",
                f'CREATE DATABASE "{_TEST_DB_NAME}";',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if cr.returncode != 0 and "already exists" not in cr.stderr:
            raise AssertionError(f"create db failed: {cr.stderr}")

    test_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{_TEST_DB_NAME}"

    schema_sql = textwrap.dedent("""
        CREATE SCHEMA IF NOT EXISTS dispatcher;
        CREATE TABLE IF NOT EXISTS dispatcher.phase_outputs (
            agent_id    uuid    NOT NULL,
            phase       text    NOT NULL,
            attempt     int     NOT NULL DEFAULT 1,
            output_json jsonb   NOT NULL,
            ts          timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS
            idx_phase_outputs_pt_agent_phase_attempt
            ON dispatcher.phase_outputs (agent_id, phase, attempt);
        CREATE TABLE IF NOT EXISTS dispatcher.phase_transitions (
            transition_id   bigserial   PRIMARY KEY,
            agent_id        uuid        NOT NULL,
            phase           text        NOT NULL,
            ts              timestamptz NOT NULL DEFAULT now()
        );
    """)
    r = subprocess.run(
        ["psql", "-X", test_dsn, "-v", "ON_ERROR_STOP=1", "-c", schema_sql],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"schema setup failed: {r.stderr}"
    return test_dsn


_TEST_DSN: str | None = None


def _get_test_dsn() -> str | None:
    """Return the dedicated test DSN, creating the database on first call."""
    global _TEST_DSN
    if _TEST_DSN is not None:
        return _TEST_DSN
    if _DSN is None:
        return None
    _TEST_DSN = _bootstrap_test_database(_DSN)
    return _TEST_DSN


def _invoke_persist(
    dsn: str, agent_id: str, phase: str, payload: str
) -> tuple[int, str, str]:
    """Source the entrypoint and call persist_phase_output with payload.

    Returns (returncode, stdout, stderr).

    We extract the ``persist_phase_output`` function body and run it in a
    minimal bash shim (same pattern as the sibling jsonb-safety test).
    """
    text = _ENTRYPOINT_SH.read_text(encoding="utf-8")
    fn_start = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("persist_phase_output()"):
            fn_start = i
            break
    assert fn_start is not None, "persist_phase_output not found in entrypoint"
    fn_lines = [lines[fn_start]]
    for line in lines[fn_start + 1 :]:
        fn_lines.append(line)
        if line.startswith("}"):
            break
    fn_body = "\n".join(fn_lines)

    shim = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        log() {{ printf '%s %s\\n' "$1" "${{2:-}}" >&2; }}
        export PSQLRC=/dev/null
        export DATABASE_URL='{dsn}'
        export AGENT_ID='{agent_id}'

        {fn_body}

        _payload=$(cat)
        persist_phase_output "{phase}" "$_payload"
    """)
    r = subprocess.run(
        ["bash", "-c", shim],
        input=payload.encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    return (
        r.returncode,
        r.stdout.decode("utf-8", "replace"),
        r.stderr.decode("utf-8", "replace"),
    )


def _count_phase_transitions(dsn: str, agent_id: str) -> int:
    """Return the number of phase_transitions rows for agent_id."""
    r = subprocess.run(
        [
            "psql",
            "-X",
            dsn,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"SELECT COUNT(*) FROM dispatcher.phase_transitions "
            f"WHERE agent_id = '{agent_id}';",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"COUNT query failed: {r.stderr}"
    return int(r.stdout.strip())


def _latest_phase_transition_phase(dsn: str, agent_id: str) -> str:
    """Return the phase column of the most-recently inserted phase_transitions row."""
    r = subprocess.run(
        [
            "psql",
            "-X",
            dsn,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"SELECT phase FROM dispatcher.phase_transitions "
            f"WHERE agent_id = '{agent_id}' "
            f"ORDER BY ts DESC, transition_id DESC LIMIT 1;",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"SELECT phase query failed: {r.stderr}"
    return r.stdout.strip()


def test_first_call_writes_one_phase_transition_row() -> None:
    """After one persist_phase_output call, exactly one phase_transitions row exists."""
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    agent_id = str(uuid.uuid4())
    rc, _, stderr = _invoke_persist(test_dsn, agent_id, "plan", '{"go": true}')
    assert rc == 0, f"persist_phase_output failed rc={rc}\n{stderr}"

    count = _count_phase_transitions(test_dsn, agent_id)
    assert count == 1, f"expected 1 phase_transitions row, got {count}"

    phase = _latest_phase_transition_phase(test_dsn, agent_id)
    assert phase == "plan", f"expected phase='plan', got {phase!r}"


def test_second_call_different_phase_adds_second_row() -> None:
    """A second persist_phase_output call with a different phase adds a new row.

    The daemon's ``_check_stuck_agents`` uses MAX(ts) from phase_transitions
    to see per-phase progress. A second call with phase='ralph' must create a
    second row so MAX(ts) belongs to the ralph row (most-recent phase).
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    agent_id = str(uuid.uuid4())
    rc1, _, stderr1 = _invoke_persist(test_dsn, agent_id, "plan", '{"go": true}')
    assert rc1 == 0, f"first call failed rc={rc1}\n{stderr1}"

    rc2, _, stderr2 = _invoke_persist(test_dsn, agent_id, "ralph", '{"verdict": "SHIP"}')
    assert rc2 == 0, f"second call failed rc={rc2}\n{stderr2}"

    count = _count_phase_transitions(test_dsn, agent_id)
    assert count == 2, f"expected 2 phase_transitions rows, got {count}"

    latest_phase = _latest_phase_transition_phase(test_dsn, agent_id)
    assert latest_phase == "ralph", (
        f"expected MAX(ts) row to have phase='ralph', got {latest_phase!r}"
    )


def test_reentry_to_same_phase_appends_new_row() -> None:
    """Re-entry to the same phase appends a NEW phase_transitions row (no ON CONFLICT).

    phase_outputs uses ON CONFLICT DO UPDATE (idempotent overwrite).
    phase_transitions has a BIGSERIAL PK with no unique constraint on
    (agent_id, phase), so every call appends a fresh row. This is the
    operative invariant the daemon relies on: repeated calls to
    persist_phase_output within the same phase keep updating last_ts.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    agent_id = str(uuid.uuid4())
    phase = "fix_ci"

    rc1, _, stderr1 = _invoke_persist(test_dsn, agent_id, phase, '{"attempt": 1}')
    assert rc1 == 0, f"first call failed rc={rc1}\n{stderr1}"

    rc2, _, stderr2 = _invoke_persist(test_dsn, agent_id, phase, '{"attempt": 2}')
    assert rc2 == 0, f"second call failed rc={rc2}\n{stderr2}"

    count = _count_phase_transitions(test_dsn, agent_id)
    assert count == 2, (
        f"expected 2 phase_transitions rows on re-entry (no ON CONFLICT), got {count}"
    )
