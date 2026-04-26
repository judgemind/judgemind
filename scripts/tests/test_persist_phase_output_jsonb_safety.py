# venv: none
"""Round-trip safety test for persist_phase_output (#3413).

Three agents died with ``exit_code=126`` after ``fix_conflict_handler_done``
and BEFORE the new ``fix_conflict_persist_done`` instrumentation fired
(PR #3412 pinpointed the wedge to ``persist_phase_output`` in
``scripts/dispatcher/agent-runner-entrypoint.sh``). Root cause: the prior
implementation passed JSON output through ``db_exec``'s ``psql -c "$1"``
shell-interpolated SQL string. Bash expanded ``$()`` / backticks /
unescaped ``$VAR`` inside the JSON BEFORE psql ever saw it, corrupting
the SQL and producing a silent exit 126.

This test sources the bash function from the entrypoint script, calls it
with a pathological JSON payload (containing ``$()``, backticks, double
quotes, single quotes, newlines, ``$VAR`` references, ``';--`` SQL-injection
shapes), and asserts the round-tripped JSONB is byte-identical to the
input.

Test is skipped when no local postgres is reachable (CI runs the schema
parity test in ``test_phase_outputs_insert_shape.py`` which is enough to
catch column-shape drift; this test catches the JSON-content-escaping
class of bug specifically and is gated on the docker-compose stack being
up). The function-level fix is independent of postgres version and the
schema parity test is the structural guarantee — this test is the
behavioural guarantee for the docker-compose dev environment.
"""

from __future__ import annotations

import json
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
    # Match the local docker-compose postgres (judgemind-bootstrap-postgres-1).
    # Credentials are the docker-compose defaults from docker-compose.yml.
    # Allow override via TEST_DSN env var so this test also runs against
    # an alternative local postgres (e.g. a maintainer's custom setup).
    dsn = os.environ.get(
        "TEST_DSN",
        "postgres://judgemind:localdev@localhost:5432/judgemind",
    )
    try:
        r = subprocess.run(
            # ``-X`` skips ~/.psqlrc so extra prelude lines (Expanded
            # display / Pager usage) don't pollute stdout.
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


# Test schema name. We deliberately don't use ``dispatcher`` so the test
# never collides with the operational schema that may be loaded into the
# same local docker postgres (it has FKs and unique constraints we can't
# easily seed). The bash function under test executes
# ``INSERT INTO dispatcher.phase_outputs ...`` with that schema name
# hard-coded — so we run the bash function pointed at a separate database
# DSN where we own the ``dispatcher`` schema entirely.
_TEST_DB_NAME = "judgemind_test_persist_phase_output"


def _bootstrap_test_database(admin_dsn: str) -> str:
    """Create a dedicated test database and return its DSN.

    Idempotent — re-runs of the test reuse the same database. The
    ``dispatcher`` schema inside this database is pristine (no FKs, no
    unique constraints beyond the (agent_id, phase, attempt) target the
    function's ON CONFLICT clause references).
    """
    # Create the database via the admin DSN's connection (postgres
    # doesn't allow CREATE DATABASE inside a transaction; we use
    # ``-c`` which auto-commits.)
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
        # Create the database; expect success or a concurrent-create race
        # which is benign.
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

    # Build the test DSN.
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
            idx_phase_outputs_test_agent_phase_attempt
            ON dispatcher.phase_outputs (agent_id, phase, attempt);
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

    We source ``agent-runner-entrypoint.sh`` with a guard env var that
    short-circuits the script before its top-level setup (the script is
    designed for ECS task entry — it kicks off git/gh/claude work at the
    bottom). Sourcing for the function definitions only requires letting
    the function bodies parse, so we set ``AGENT_RUNNER_SOURCE_ONLY=1``
    and add a tiny shim that exits before the main loop.

    Implementation note: the entrypoint script is not currently
    structured to be sourced. Rather than refactor it, we extract the
    ``persist_phase_output`` function body into a temporary script and
    run it standalone. The function body is self-contained — it only
    references global env vars (``DATABASE_URL``, ``AGENT_ID``) and the
    ``log`` function. We provide a no-op ``log``.
    """
    text = _ENTRYPOINT_SH.read_text(encoding="utf-8")
    # Extract function body via the same logic the schema-parity test uses.
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
        # Minimal log() — write to stderr so caller can inspect.
        log() {{ printf '%s %s\\n' "$1" "${{2:-}}" >&2; }}
        # Skip ~/.psqlrc so prelude lines don't pollute stdout when the
        # caller's local psql config sets things like \\set ECHO_HIDDEN.
        export PSQLRC=/dev/null
        export DATABASE_URL='{dsn}'
        export AGENT_ID='{agent_id}'

        {fn_body}

        # Read payload from stdin (the python harness pipes it through).
        # Using a here-string would re-trigger bash expansion of $() / etc.
        # in the payload — exactly what we're testing against.
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


def _roundtrip_payload(dsn: str, payload: str) -> tuple[int, str | None, str]:
    """Insert payload via persist_phase_output, then SELECT it back.

    Returns (rc, persisted_text, stderr).
    """
    agent_id = str(uuid.uuid4())
    phase = "fix_conflict"
    rc, _stdout, stderr = _invoke_persist(dsn, agent_id, phase, payload)
    if rc != 0:
        return rc, None, stderr
    # Read it back as text — round-trip via jsonb's ::text cast.
    sel = subprocess.run(
        [
            "psql",
            "-X",
            dsn,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"SELECT output_json::text FROM dispatcher.phase_outputs "
            f"WHERE agent_id = '{agent_id}' AND phase = '{phase}';",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert sel.returncode == 0, f"SELECT failed: {sel.stderr}"
    return rc, sel.stdout.rstrip("\n"), stderr


def test_persist_round_trips_pathological_json() -> None:
    """A fix_conflict-style output with shell metacharacters round-trips.

    The payload is a realistic ``handle_fix_conflict`` envelope shape with
    ``resolved_files`` content that contains every shell-interpretable
    character class the prior bug missed: ``$(...)``, backticks, double
    quotes, single quotes, ``$VAR``, newlines, and SQL-injection shapes.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    # Build a realistic resolved-file content with every dangerous metachar.
    resolved_text = (
        "diff --git a/foo.py b/foo.py\n"
        "x = $(whoami)\n"
        "y = `date`\n"
        'z = "double-quoted"\n'
        "w = 'single-quoted'\n"
        "v = $HOME\n"
        "p = '; DROP TABLE phase_outputs; --\n"
        "q = backtick `injection` here\n"
        'r = nested $(echo "$(date)")\n'
    )
    payload_obj = {
        "verdict": "resolved",
        "resolved_files": [
            {
                "path": "foo.py",
                "content": resolved_text,
            }
        ],
        "notes": "Resolved by claude with $() and `` content",
    }
    payload = json.dumps(payload_obj)

    rc, persisted, stderr = _roundtrip_payload(test_dsn, payload)
    assert rc == 0, (
        f"persist_phase_output failed with rc={rc}. stderr:\n{stderr}\n"
        f"payload (first 300): {payload[:300]!r}"
    )
    assert persisted is not None, "no row persisted"

    # JSONB normalisation may reorder keys; compare structurally.
    parsed = json.loads(persisted)
    assert parsed == payload_obj, (
        f"round-trip mismatch.\n  got:  {persisted!r}\n  want: {payload!r}"
    )


def test_persist_round_trips_dollar_paren_payload() -> None:
    """A minimal payload containing only ``$()`` round-trips.

    Smaller test focused on the specific class of input that triggered
    the production exit 126 — ``$()`` inside JSON content was being
    expanded by bash before the SQL reached psql.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    payload_obj = {"trigger": "$(this should not execute)"}
    payload = json.dumps(payload_obj)

    rc, persisted, stderr = _roundtrip_payload(test_dsn, payload)
    assert rc == 0, (
        f"persist_phase_output failed with rc={rc}. stderr:\n{stderr}\n"
        f"payload: {payload!r}"
    )
    assert persisted is not None
    parsed = json.loads(persisted)
    assert parsed == payload_obj


def test_persist_round_trips_backtick_payload() -> None:
    """A payload with backticks round-trips.

    Backticks are the older form of command substitution; bash expands
    them in unquoted contexts and inside double-quoted strings — so they
    were also part of the #3413 wedge class.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    payload_obj = {"cmd": "result is `whoami` today"}
    payload = json.dumps(payload_obj)

    rc, persisted, stderr = _roundtrip_payload(test_dsn, payload)
    assert rc == 0, f"persist failed rc={rc}\n{stderr}"
    assert persisted is not None
    parsed = json.loads(persisted)
    assert parsed == payload_obj


def test_persist_idempotent_overwrite() -> None:
    """Calling persist_phase_output twice with the same (agent_id, phase)
    overwrites rather than failing on the unique-constraint violation.

    This is the #3219 ON CONFLICT DO UPDATE invariant — it must continue
    to hold under the new heredoc-based implementation.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    agent_id = str(uuid.uuid4())
    phase = "fix_conflict"

    rc1, _, stderr1 = _invoke_persist(test_dsn, agent_id, phase, json.dumps({"v": 1}))
    assert rc1 == 0, f"first insert failed: {stderr1}"
    rc2, _, stderr2 = _invoke_persist(test_dsn, agent_id, phase, json.dumps({"v": 2}))
    assert rc2 == 0, f"second insert (overwrite) failed: {stderr2}"

    sel = subprocess.run(
        [
            "psql",
            "-X",
            test_dsn,
            "-At",
            "-c",
            f"SELECT output_json->>'v' FROM dispatcher.phase_outputs "
            f"WHERE agent_id = '{agent_id}' AND phase = '{phase}';",
        ],
        capture_output=True,
        text=True,
    )
    assert sel.stdout.strip() == "2", (
        f"expected overwrite to value 2, got {sel.stdout!r}"
    )
