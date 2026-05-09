# venv: none
"""Regression: shared persist-test bootstrap must mirror production INSERTs (#4424).

This test is the structural backstop that prevents the failure mode #4418
caught manually: production added a second ``INSERT INTO
dispatcher.phase_transitions`` to ``persist_phase_output`` (#3697) and only
one of the three persist-* tests had its bootstrap updated; the other two
silently broke for ~12 days because the CI shard skips the behavioural
tests (no docker postgres in CI).

The structural property we enforce: every ``INSERT INTO dispatcher.<table>``
target referenced by the persist-* / ralph_head_watcher functions in
``scripts/dispatcher/agent-runner-entrypoint.sh`` exists as a
``CREATE TABLE`` in the shared bootstrap. Every column listed inside an
INSERT also exists on the corresponding bootstrap table. New INSERT
targets break this test at parse time — there is no way for a future
production change to add a third INSERT target without forcing a one-line
update to ``_dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL``.

Runs in CI without postgres — the assertions are pure string parsing
against the entrypoint script and the bootstrap module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENTRYPOINT_SH = _REPO_ROOT / "scripts" / "dispatcher" / "agent-runner-entrypoint.sh"

# Functions whose INSERTs the shared bootstrap must mirror. Other
# functions in the entrypoint also INSERT into ``dispatcher.*``
# (handlers, the agent_runner_reaped_failure inline INSERT) — we cover
# the agent_runner_reaped_failure INSERT explicitly because it shares
# the ``phase_outputs`` shape with persist_phase_output. We do not try
# to cover every UPDATE site (the agents table is used as a stub that
# only needs ``ralph_iterations_observed`` for the watcher's UPDATE).
_TARGET_FUNCTIONS = (
    "persist_phase_output",
    "persist_ralph_patch",
    "ralph_head_watcher_persist",
    "agent_runner_reaped_failure",
)


def _read_function_body(text: str, fn_name: str) -> str | None:
    """Return the body of a top-level bash function, or None if absent.

    Mirrors the extraction logic each persist-* test inlines: scan for a
    line starting with ``<fn_name>()`` and slice through to the first
    line beginning with ``}``. Returns the body inclusive of the opening
    declaration and closing brace.
    """
    lines = text.splitlines()
    fn_start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{fn_name}()"):
            fn_start = i
            break
    if fn_start is None:
        return None
    fn_lines = [lines[fn_start]]
    for line in lines[fn_start + 1 :]:
        fn_lines.append(line)
        if line.startswith("}"):
            break
    return "\n".join(fn_lines)


# Regex that matches ``INSERT INTO dispatcher.<table> (col1, col2, ...)``
# allowing arbitrary whitespace + newlines between the table name and
# the column list. The bash sources lay these out across multiple lines
# inside heredocs, so DOTALL is required.
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+dispatcher\.([a-z_]+)\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_dispatcher_inserts(text: str) -> list[tuple[str, frozenset[str]]]:
    """Return ``(table, columns)`` tuples for every ``INSERT INTO dispatcher.*``.

    Columns are normalised — whitespace and newlines stripped, lowercase.
    Duplicates are kept (the same INSERT may appear in both branches of
    an ``\\if`` block) — caller deduplicates if needed.
    """
    out: list[tuple[str, frozenset[str]]] = []
    for m in _INSERT_RE.finditer(text):
        table = m.group(1).strip().lower()
        col_blob = m.group(2)
        cols = frozenset(
            c.strip().lower()
            for c in col_blob.replace("\n", ",").split(",")
            if c.strip()
        )
        out.append((table, cols))
    return out


# Tables the bootstrap MUST create — the union across every INSERT
# site under _TARGET_FUNCTIONS. Computed once at import time so a
# missing table fails at collection rather than during the test body.
_ENTRYPOINT_TEXT = _ENTRYPOINT_SH.read_text(encoding="utf-8")


def _expected_inserts() -> list[tuple[str, frozenset[str]]]:
    """Walk every target function and collect (table, columns) tuples."""
    found: list[tuple[str, frozenset[str]]] = []
    for fn in _TARGET_FUNCTIONS:
        body = _read_function_body(_ENTRYPOINT_TEXT, fn)
        assert body is not None, (
            f"function {fn!r} not found in {_ENTRYPOINT_SH} — "
            "either renamed or removed; update _TARGET_FUNCTIONS."
        )
        for table, cols in _extract_dispatcher_inserts(body):
            found.append((table, cols))
    return found


# ---------------------------------------------------------------------------
# Bootstrap-side parser: read the shared DDL from _dispatcher_test_bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_tables() -> dict[str, frozenset[str]]:
    """Parse ``_dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL`` into ``{table: columns}``.

    Returns columns lowercased and stripped of type / NOT NULL /
    DEFAULT clauses. Imports the helper module rather than re-reading
    the file so a future shape change (e.g. moving the DDL into a
    Python list literal) still works as long as the constant name is
    preserved.
    """
    # _dispatcher_test_bootstrap.py lives in the same directory as this
    # test. ``scripts/tests/__init__.py`` exists, so pytest treats
    # ``tests`` as a package — combined with ``PYTHONPATH=scripts`` (set
    # by the CI shard, defined in .github/workflows/ci.yml's
    # scripts-tests job) we import via the fully-qualified package path.
    from tests import _dispatcher_test_bootstrap

    ddl: str = _dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL
    out: dict[str, frozenset[str]] = {}

    # Match `CREATE TABLE [IF NOT EXISTS] dispatcher.<table> ( <body> );`
    table_re = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?dispatcher\.([a-z_]+)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    for m in table_re.finditer(ddl):
        table = m.group(1).strip().lower()
        body = m.group(2)
        cols: set[str] = set()
        # Each column declaration occupies one comma-separated chunk.
        # Take the first whitespace-separated token of each chunk as the
        # column name. Skip table-level constraints and unique indexes
        # that don't begin with a bare identifier (e.g. ``CREATE
        # UNIQUE INDEX ...`` lives outside the parenthesised body so it
        # never appears here, but we defensively skip ``PRIMARY KEY
        # (...)`` / ``UNIQUE (...)`` / ``FOREIGN KEY ...`` / ``CHECK
        # ...`` shapes anyway).
        for chunk in body.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            head = chunk.split()[0].strip().lower()
            if head in {"primary", "unique", "foreign", "check", "constraint"}:
                continue
            # ``--`` line comments inside the DDL surface as a head
            # token of ``--``; skip them too.
            if head.startswith("--"):
                continue
            cols.add(head)
        out[table] = frozenset(cols)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_insert_target_has_a_bootstrap_table() -> None:
    """Each ``INSERT INTO dispatcher.<table>`` target has a CREATE TABLE.

    A future production change that adds a third INSERT target (e.g.
    ``INSERT INTO dispatcher.cost_breakdown``) without updating the
    shared bootstrap fails this test at collection time.
    """
    inserts = _expected_inserts()
    bootstrap = _bootstrap_tables()
    targets = {table for table, _ in inserts}
    missing = targets - set(bootstrap)
    assert not missing, (
        f"INSERT targets present in {_ENTRYPOINT_SH.name} but missing from "
        f"_dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL: {sorted(missing)}.\n"
        f"Add a CREATE TABLE for each missing target in conftest.py."
    )


def test_every_insert_column_exists_in_bootstrap() -> None:
    """Each column listed in an INSERT exists in the bootstrap CREATE TABLE.

    Catches the narrower drift where a new column is added to an
    existing INSERT without being added to the bootstrap. Without this,
    a future production change that adds (e.g.) ``output_json,
    log_text, cost_usd`` to ``persist_phase_output`` would only fail
    the behavioural tests on first round-trip — and the behavioural
    tests skip in CI.
    """
    inserts = _expected_inserts()
    bootstrap = _bootstrap_tables()
    failures: list[str] = []
    for table, cols in inserts:
        if table not in bootstrap:
            # Already covered by the test above; skip here so the
            # failure message stays readable.
            continue
        missing = cols - bootstrap[table]
        if missing:
            failures.append(
                f"  dispatcher.{table}: INSERT columns {sorted(missing)} "
                f"are not present on the bootstrap table "
                f"(bootstrap has {sorted(bootstrap[table])})"
            )
    assert not failures, (
        "INSERT columns present in the entrypoint but missing from the "
        "shared bootstrap — add them to "
        "_dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL:\n"
        + "\n".join(failures)
    )


@pytest.mark.parametrize(
    "table",
    ("phase_outputs", "phase_transitions", "ralph_patches", "agents"),
)
def test_known_target_tables_are_present(table: str) -> None:
    """Pin the four known target tables exist in the bootstrap.

    Belt-and-suspenders against the broader regex above silently
    matching zero INSERTs (e.g. if the entrypoint script is renamed and
    the open + close logic above stops finding any function bodies).
    """
    bootstrap = _bootstrap_tables()
    assert table in bootstrap, (
        f"shared bootstrap is missing dispatcher.{table} — every "
        "persist-* test relies on this table existing. Update "
        "_dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL."
    )


def test_phase_outputs_required_floor_present() -> None:
    """phase_outputs has the three columns every INSERT site requires.

    Mirrors the floor enforced by
    ``test_phase_outputs_insert_shape.py::_REQUIRED_FLOOR`` for the
    bootstrap side: even if a future change drops the optional
    metering columns, ``agent_id`` + ``phase`` + ``output_json`` are
    structurally required.
    """
    bootstrap = _bootstrap_tables()
    required = frozenset({"agent_id", "phase", "output_json"})
    actual = bootstrap.get("phase_outputs", frozenset())
    missing = required - actual
    assert not missing, (
        f"shared bootstrap dispatcher.phase_outputs is missing "
        f"required columns: {sorted(missing)}. The required floor matches "
        f"the schema-parity test in test_phase_outputs_insert_shape.py."
    )


def test_no_inline_create_table_in_persist_test_files() -> None:
    """No persist-* test file inlines its own ``CREATE TABLE`` DDL.

    Direct enforcement of the AC: ``grep -n 'CREATE TABLE.*dispatcher'
    scripts/tests/test_persist_*`` must show zero matches. The shared
    bootstrap in conftest.py is the single source of truth.
    """
    persist_tests = sorted((_REPO_ROOT / "scripts" / "tests").glob("test_persist_*.py"))
    assert persist_tests, (
        "no persist-* test files found — glob pattern is wrong or the "
        "files were renamed."
    )
    inline = re.compile(r"CREATE\s+TABLE.*dispatcher\.", re.IGNORECASE)
    failures: list[str] = []
    for p in persist_tests:
        # Skip the structural test itself — its bootstrap-parser regex
        # contains the pattern as a string literal we want to find.
        if p.name == Path(__file__).name:
            continue
        if inline.search(p.read_text(encoding="utf-8")):
            failures.append(str(p.relative_to(_REPO_ROOT)))
    assert not failures, (
        "persist-* test files still inline ``CREATE TABLE dispatcher.*`` "
        "DDL — move it to _dispatcher_test_bootstrap.SHARED_DISPATCHER_SCHEMA_DDL. "
        f"Offenders: {failures}"
    )
