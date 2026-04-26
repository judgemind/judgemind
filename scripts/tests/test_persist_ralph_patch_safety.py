# venv: none
"""Round-trip safety test for persist_ralph_patch (#3488).

The prior implementation of ``persist_ralph_patch`` in
``scripts/dispatcher/agent-runner-entrypoint.sh`` escaped single-quotes with
``sed "s/'/''/g"`` and then interpolated the result into a SQL string via
``db_exec``. Bash expanded ``$()`` / backticks / ``$VAR`` inside the patch
content BEFORE psql ever saw it, corrupting the SQL and producing a silent
exit 126 — the same class of bug as #3413 for ``persist_phase_output``.

PR #3419 fixed ``persist_phase_output`` with the tmpfile + quoted-heredoc +
psql-var pattern. This fix (#3488) applies the same pattern to
``persist_ralph_patch`` and ``ralph_head_watcher_persist``.

This test sources the bash function from the entrypoint script, calls it with
a pathological patch payload (containing ``$()``, backticks, double quotes,
single quotes, newlines, ``$VAR`` references), and asserts the round-tripped
TEXT column is byte-identical to the input.

Test is skipped when no local postgres is reachable (CI runs the schema parity
test in ``test_phase_outputs_insert_shape.py`` which is enough to catch column-
shape drift; this test catches the content-escaping class of bug specifically
and is gated on the docker-compose stack being up).
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

# Test schema name — deliberately separate from ``dispatcher`` so the test
# never collides with the operational schema that may be loaded in the same
# docker postgres (it has FKs and constraints we can't easily seed). The bash
# function under test executes ``INSERT INTO dispatcher.ralph_patches ...`` with
# that schema name hard-coded, so we point it at a dedicated test database
# where we own the ``dispatcher`` schema entirely.
_TEST_DB_NAME = "judgemind_test_persist_ralph_patch"


def _bootstrap_test_database(admin_dsn: str) -> str:
    """Create a dedicated test database and return its DSN.

    Idempotent — re-runs of the test reuse the same database.
    """
    probe = subprocess.run(
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
    if probe.returncode != 0:
        raise AssertionError(f"db existence probe failed: {probe.stderr}")
    if probe.stdout.strip() != "1":
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

    # Minimal schema mirroring production columns. No FKs — agents table is
    # referenced by the UPDATE in ralph_head_watcher_persist; we create a stub
    # with the same column shape so the UPDATE is a no-op (WHERE clause never
    # matches a real agent_id, which is fine — we only need the INSERT to work).
    schema_sql = textwrap.dedent("""
        CREATE SCHEMA IF NOT EXISTS dispatcher;
        CREATE TABLE IF NOT EXISTS dispatcher.ralph_patches (
            patch_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id       UUID        NOT NULL,
            issue_number   INT         NOT NULL,
            patch_content  TEXT        NOT NULL,
            commit_sha     TEXT,
            iteration_n    INT,
            verdict        TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS dispatcher.agents (
            agent_id                    UUID PRIMARY KEY,
            ralph_iterations_observed   INT NOT NULL DEFAULT 0
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


def _extract_function_body(fn_name: str) -> str:
    """Extract a top-level bash function body from the entrypoint script."""
    text = _ENTRYPOINT_SH.read_text(encoding="utf-8")
    lines = text.splitlines()
    fn_start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{fn_name}()"):
            fn_start = i
            break
    assert fn_start is not None, f"{fn_name} not found in entrypoint"
    fn_lines = [lines[fn_start]]
    for line in lines[fn_start + 1 :]:
        fn_lines.append(line)
        if line.startswith("}"):
            break
    return "\n".join(fn_lines)


def _invoke_persist_ralph_patch(
    dsn: str,
    agent_id: str,
    issue_number: str,
    patch_content: str,
    workspace_dir: Path,
    repo_dir: Path,
) -> tuple[int, str, str]:
    """Source persist_ralph_patch and call it with a fake SHIP patch.

    Writes patch_content to ``$AGENT_WORKSPACE/ralph-ship.patch``, provides a
    stub ``git rev-parse HEAD`` via a minimal git repo (or a PATH shim), and
    calls the function.

    Returns (returncode, stdout, stderr).
    """
    # Write the patch file that persist_ralph_patch expects.
    patch_file = workspace_dir / "ralph-ship.patch"
    patch_file.write_text(patch_content, encoding="utf-8")

    fn_body = _extract_function_body("persist_ralph_patch")

    # Stub git so that `git -C "$REPO_ROOT" rev-parse HEAD` returns a fake SHA.
    # We write a tiny shim script into a temp bin dir on PATH.
    shim_bin = workspace_dir / "shim-bin"
    shim_bin.mkdir(exist_ok=True)
    git_shim = shim_bin / "git"
    git_shim.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            # Stub: rev-parse HEAD -> fixed SHA; format-patch -> copy existing file.
            if [[ "$*" == *"rev-parse HEAD"* ]]; then
                printf 'deadbeef1234567890abcdef1234567890abcdef'
            elif [[ "$*" == *"format-patch"* ]]; then
                # The real function writes to $_patch_file before calling us;
                # the file already exists — nothing to do.
                true
            else
                /usr/bin/git "$@"
            fi
        """),
        encoding="utf-8",
    )
    git_shim.chmod(0o755)

    shim = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        export PATH="{shim_bin}:$PATH"
        log() {{ printf '%s %s\\n' "$1" "${{2:-}}" >&2; }}
        export PSQLRC=/dev/null
        export DATABASE_URL='{dsn}'
        export AGENT_ID='{agent_id}'
        export ISSUE_NUMBER='{issue_number}'
        export AGENT_WORKSPACE='{workspace_dir}'
        export REPO_ROOT='{repo_dir}'

        {fn_body}

        persist_ralph_patch
    """)
    r = subprocess.run(
        ["bash", "-c", shim],
        capture_output=True,
        timeout=20,
    )
    return (
        r.returncode,
        r.stdout.decode("utf-8", "replace"),
        r.stderr.decode("utf-8", "replace"),
    )


def _roundtrip_patch(
    dsn: str,
    patch_content: str,
    tmp_path: Path,
) -> tuple[int, str | None, str]:
    """Insert patch_content via persist_ralph_patch, then SELECT it back."""
    agent_id = str(uuid.uuid4())
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    rc, _stdout, stderr = _invoke_persist_ralph_patch(
        dsn, agent_id, "42", patch_content, workspace_dir, repo_dir
    )
    if rc != 0:
        return rc, None, stderr

    sel = subprocess.run(
        [
            "psql",
            "-X",
            dsn,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"SELECT patch_content FROM dispatcher.ralph_patches "
            f"WHERE agent_id = '{agent_id}';",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert sel.returncode == 0, f"SELECT failed: {sel.stderr}"
    # psql -At strips the trailing newline from each row; add it back only
    # if the original content ended with a newline (psql convention).
    persisted = sel.stdout
    # psql appends a newline to the last row in unaligned mode; strip it once.
    if persisted.endswith("\n"):
        persisted = persisted[:-1]
    return rc, persisted, stderr


def test_persist_round_trips_pathological_patch(tmp_path: Path) -> None:
    """A ralph patch with shell metacharacters round-trips byte-identical.

    The patch contains every shell-interpretable character class that broke the
    prior sed-escape implementation: ``$()``, backticks, double quotes, single
    quotes, ``$VAR`` references, newlines, and SQL-injection shapes.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    patch_content = (
        "From deadbeef Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] test\n"
        "\n"
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "+x = $(whoami)\n"
        "+y = `date`\n"
        '+z = "double-quoted"\n'
        "+w = 'single-quoted'\n"
        "+v = $HOME\n"
        "+p = '; DROP TABLE ralph_patches; --\n"
        "+q = backtick `injection` here\n"
        '+r = nested $(echo "$(date)")\n'
        "\n"
    )

    rc, persisted, stderr = _roundtrip_patch(test_dsn, patch_content, tmp_path)
    assert rc == 0, (
        f"persist_ralph_patch failed with rc={rc}. stderr:\n{stderr}\n"
        f"patch (first 300): {patch_content[:300]!r}"
    )
    assert persisted is not None, "no row persisted"
    assert persisted == patch_content.rstrip("\n"), (
        f"round-trip mismatch.\n  got:  {persisted!r}\n  want: {patch_content.rstrip(chr(10))!r}"
    )


def test_persist_round_trips_dollar_paren_patch(tmp_path: Path) -> None:
    """A patch containing only ``$()`` round-trips.

    Focused on the specific class of input that triggered the production
    exit 126 — ``$()`` inside content was being expanded by bash before
    the SQL reached psql.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    patch_content = (
        "From deadbeef Mon Sep 17 00:00:00 2001\n+x = $(this should not execute)\n"
    )
    rc, persisted, stderr = _roundtrip_patch(test_dsn, patch_content, tmp_path)
    assert rc == 0, f"persist_ralph_patch failed with rc={rc}. stderr:\n{stderr}"
    assert persisted is not None
    assert persisted == patch_content.rstrip("\n")


def test_persist_round_trips_backtick_patch(tmp_path: Path) -> None:
    """A patch with backticks round-trips.

    Backticks are the older form of command substitution; bash expands them in
    unquoted contexts — so they were part of the same bug class as #3413.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    patch_content = "From deadbeef Mon Sep 17 00:00:00 2001\n+cmd = `whoami` today\n"
    rc, persisted, stderr = _roundtrip_patch(test_dsn, patch_content, tmp_path)
    assert rc == 0, f"persist failed rc={rc}\n{stderr}"
    assert persisted is not None
    assert persisted == patch_content.rstrip("\n")


def test_persist_round_trips_var_expansion_patch(tmp_path: Path) -> None:
    """A patch containing bare ``$VAR`` references round-trips.

    Unquoted ``$HOME`` / ``$PATH`` / ``$1`` inside content would expand under
    the old implementation's ``sed``-then-``db_exec`` path.
    """
    test_dsn = _get_test_dsn()
    assert test_dsn is not None

    patch_content = (
        "From deadbeef Mon Sep 17 00:00:00 2001\n+path = $HOME/bin:$PATH\n+arg = $1\n"
    )
    rc, persisted, stderr = _roundtrip_patch(test_dsn, patch_content, tmp_path)
    assert rc == 0, f"persist failed rc={rc}\n{stderr}"
    assert persisted is not None
    assert persisted == patch_content.rstrip("\n")
