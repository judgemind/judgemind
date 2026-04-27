# venv: none
"""Schema-parity tests for dispatcher.phase_outputs INSERT sites (#3231).

Parses two independent INSERT implementations:
  1. Python: scripts/dispatcher/daemon.py  _persist_phase_output()
  2. Bash:   scripts/dispatcher/agent-runner-entrypoint.sh
             - persist_phase_output() function body
             - inline INSERT inside agent_runner_reaped_failure()

Asserts that the bash sites are structurally consistent with the daemon's
authoritative INSERT: same ON CONFLICT target, ON CONFLICT DO UPDATE present
on every site, and column sets that are a subset of the daemon's (modulo the
nullable metering columns the bash side intentionally omits).

If any of these tests fail it means the two INSERT sites have drifted --
the class of bug that caused #3115 (missing `status` column) and #3219
(missing ON CONFLICT).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TypedDict

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_DAEMON_PY = _REPO_ROOT / "scripts" / "dispatcher" / "daemon.py"
_ENTRYPOINT_SH = _REPO_ROOT / "scripts" / "dispatcher" / "agent-runner-entrypoint.sh"

# Nullable metering / log columns the bash side intentionally omits.
# The daemon always writes them (as NULL when unused); bash writes only the
# three required columns and leaves the rest as DB-side defaults.
_BASH_NULLABLE_OMIT = frozenset(
    {
        "log_text",
        "attempt",
        "tokens_input",
        "tokens_output",
        "tokens_cache_read",
        "tokens_cache_write",
        "cost_usd",
        "model_used",
    }
)

# Every INSERT site MUST contain at least these three columns.
_REQUIRED_FLOOR = frozenset({"agent_id", "phase", "output_json"})


class InsertShape(TypedDict):
    columns: frozenset
    on_conflict_target: str | None
    has_on_conflict_do_update: bool


class EntrypointInsertShape(TypedDict):
    columns: frozenset
    on_conflict_target: str | None
    has_on_conflict_do_update: bool
    site: str


# ---------------------------------------------------------------------------
# Parser: daemon.py (Python AST)
# ---------------------------------------------------------------------------


def _extract_daemon_insert() -> InsertShape:
    """Walk daemon.py's AST, find _persist_phase_output, and extract the
    INSERT INTO dispatcher.phase_outputs column list + ON CONFLICT shape.

    Returns an InsertShape dict.
    """
    source = _DAEMON_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_DAEMON_PY))

    # Find the FunctionDef named _persist_phase_output anywhere in the tree.
    target_func: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_persist_phase_output":
            target_func = node
            break

    assert target_func is not None, (
        f"Could not find _persist_phase_output in {_DAEMON_PY}"
    )

    # Collect all Constant string nodes within the function body and join
    # adjacent string constants (implicit concatenation becomes JoinedStr /
    # multiple Constants inside a Call arg).
    def _collect_call_sql(func_node: ast.FunctionDef) -> str | None:
        """Return the first cur.execute() SQL string whose text contains
        'INSERT INTO dispatcher.phase_outputs'.
        """
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue
            # Match cur.execute(...)
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            # Collect all string constants from the first argument tree.
            parts: list[str] = []
            for sub in ast.walk(first_arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    parts.append(sub.value)
            sql = "".join(parts)
            if "INSERT INTO dispatcher.phase_outputs" in sql:
                return sql
        return None

    sql = _collect_call_sql(target_func)
    assert sql is not None, (
        "Could not find 'INSERT INTO dispatcher.phase_outputs' cur.execute() "
        f"call inside _persist_phase_output in {_DAEMON_PY}"
    )

    return _parse_insert_sql(sql, source="daemon.py:_persist_phase_output")


# ---------------------------------------------------------------------------
# Parser: agent-runner-entrypoint.sh (string / regex)
# ---------------------------------------------------------------------------


def _extract_function_block(text: str, func_name: str) -> str:
    """Extract the body of a bash function named func_name.

    Uses the ``funcname() {`` ... ``^}`` pattern (function ends at a
    closing brace at column 0).  Returns the raw block text including the
    opening line.
    """
    lines = text.splitlines()
    in_fn = False
    block_lines: list[str] = []
    for line in lines:
        if re.match(rf"^{re.escape(func_name)}\s*\(\s*\)\s*{{", line):
            in_fn = True
            block_lines = [line]
            continue
        if in_fn:
            block_lines.append(line)
            # A lone "}" at column 0 ends the function.
            if re.match(r"^\}", line):
                break
    assert block_lines, (
        f"Could not find bash function '{func_name}' in {_ENTRYPOINT_SH}"
    )
    return "\n".join(block_lines)


def _extract_entrypoint_inserts() -> list[EntrypointInsertShape]:
    """Parse agent-runner-entrypoint.sh and return shapes for two INSERT sites:

    1. persist_phase_output function body.
    2. Inline INSERT inside agent_runner_reaped_failure.
    """
    text = _ENTRYPOINT_SH.read_text(encoding="utf-8")

    # --- Site 1: persist_phase_output ---
    ppo_block = _extract_function_block(text, "persist_phase_output")
    shape1 = _parse_insert_sql(
        ppo_block, source="agent-runner-entrypoint.sh:persist_phase_output"
    )
    shape1["site"] = "persist_phase_output"

    # --- Site 2: inline INSERT in agent_runner_reaped_failure ---
    arf_block = _extract_function_block(text, "agent_runner_reaped_failure")
    shape2 = _parse_insert_sql(
        arf_block, source="agent-runner-entrypoint.sh:agent_runner_reaped_failure"
    )
    shape2["site"] = "agent_runner_reaped_failure"

    return [shape1, shape2]


# ---------------------------------------------------------------------------
# Shared SQL shape extractor (regex-based, works on both Python and bash SQL)
# ---------------------------------------------------------------------------


def _parse_insert_sql(text: str, source: str = "") -> EntrypointInsertShape:
    """Extract INSERT column list and ON CONFLICT shape from an SQL fragment.

    Works on:
    - Python implicit-concatenation strings (multi-line, with extra whitespace).
    - Bash heredoc / quoted INSERT strings (may include shell quoting artefacts).

    Returns an EntrypointInsertShape dict (without the 'site' key set — callers
    set it).
    """
    # Normalise whitespace so newlines/tabs don't confuse the regex.
    flat = re.sub(r"\s+", " ", text)

    # Extract column list between the first ( ) after INSERT INTO ... table_name.
    col_match = re.search(
        r"INSERT\s+INTO\s+dispatcher\.phase_outputs\s*\(([^)]+)\)",
        flat,
        re.IGNORECASE,
    )
    assert col_match is not None, (
        f"Could not find INSERT INTO dispatcher.phase_outputs (...) in [{source}]: {flat!r}"
    )
    raw_cols = col_match.group(1)
    # Split on commas, strip whitespace and any shell/psql quoting artefacts.
    columns = frozenset(
        c.strip().strip('"').strip("'").lower()
        for c in raw_cols.split(",")
        if c.strip()
    )

    # ON CONFLICT target: extract column(s) between ON CONFLICT ( ... )
    conflict_match = re.search(
        r"ON\s+CONFLICT\s*\(([^)]+)\)",
        flat,
        re.IGNORECASE,
    )
    if conflict_match:
        on_conflict_target = re.sub(
            r"\s+", " ", conflict_match.group(1).strip().lower()
        )
    else:
        on_conflict_target = None

    has_on_conflict_do_update = bool(
        re.search(r"ON\s+CONFLICT\s*\([^)]+\)\s*DO\s+UPDATE", flat, re.IGNORECASE)
    )

    return EntrypointInsertShape(
        columns=columns,
        on_conflict_target=on_conflict_target,
        has_on_conflict_do_update=has_on_conflict_do_update,
        site="",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_entrypoint_columns_subset_of_daemon() -> None:
    """Both entrypoint INSERT sites' column sets must be a subset of the
    daemon's authoritative column set.

    The bash side intentionally omits nullable metering columns (log_text,
    attempt, tokens_*,  cost_usd, model_used) — that omission is whitelisted
    via _BASH_NULLABLE_OMIT.  The required floor {agent_id, phase, output_json}
    must appear on every site.
    """
    daemon_shape = _extract_daemon_insert()
    entrypoint_shapes = _extract_entrypoint_inserts()

    daemon_cols = daemon_shape["columns"]

    for shape in entrypoint_shapes:
        site = shape["site"]
        site_cols = shape["columns"]

        # 1. Required floor must be present on every site.
        missing_floor = _REQUIRED_FLOOR - site_cols
        assert not missing_floor, (
            f"[{site}] Missing required columns {missing_floor!r} "
            f"(site has {sorted(site_cols)!r})"
        )

        # 2. Site columns must be a subset of daemon columns (modulo whitelist).
        unknown = site_cols - daemon_cols - _BASH_NULLABLE_OMIT
        assert not unknown, (
            f"[{site}] Column(s) {unknown!r} present in entrypoint INSERT "
            f"but absent from daemon's INSERT and not in the nullable-omit "
            f"whitelist. Daemon columns: {sorted(daemon_cols)!r}"
        )

        # 3. Daemon columns minus the nullable-omit set must all be present
        #    (i.e. no extra required column was added to daemon without updating
        #    the bash side).
        required_on_bash = daemon_cols - _BASH_NULLABLE_OMIT
        missing_required = required_on_bash - site_cols
        assert not missing_required, (
            f"[{site}] Required column(s) {missing_required!r} are in the "
            f"daemon INSERT but absent from the entrypoint INSERT and not in "
            f"the nullable-omit whitelist. "
            f"Entrypoint has: {sorted(site_cols)!r}"
        )


def test_all_sites_have_on_conflict_do_update() -> None:
    """Every INSERT site must carry ON CONFLICT (...) DO UPDATE.

    Without this, a second INSERT on the same (agent_id, phase, attempt)
    three-tuple raises a unique-constraint violation and kills the ECS task
    (the bug that triggered #3219).
    """
    daemon_shape = _extract_daemon_insert()
    entrypoint_shapes = _extract_entrypoint_inserts()

    all_shapes: list[tuple[str, bool]] = [
        ("daemon.py:_persist_phase_output", daemon_shape["has_on_conflict_do_update"])
    ]
    for shape in entrypoint_shapes:
        all_shapes.append((shape["site"], shape["has_on_conflict_do_update"]))

    for site, has_upsert in all_shapes:
        assert has_upsert, (
            f"[{site}] INSERT is missing ON CONFLICT (...) DO UPDATE. "
            "A re-entrant phase write will raise a unique-constraint violation "
            "and crash the ECS task (see #3219)."
        )


def test_agent_runner_reaped_failure_no_bash_interpolation() -> None:
    """agent_runner_reaped_failure must use psql -v + jsonb_build_object, not
    bash interpolation via _escaped_reason (#3488).

    The old implementation did:
        _escaped_reason=$(printf '%s' "$_reason" | sed "s/'/''/g")
        ... '{"category": "$_category", "reason": "$_escaped_reason"}' ...

    This still allowed $() / backticks / $VAR inside $_reason to be expanded
    by bash before psql saw the SQL.  The fix lifts $_category and $_reason
    into psql -v vars and builds the JSON via jsonb_build_object so no bash
    string interpolation occurs.

    This test is purely structural (regex on source text) and always runs —
    no DB required.
    """
    text = _ENTRYPOINT_SH.read_text(encoding="utf-8")
    arf_block = _extract_function_block(text, "agent_runner_reaped_failure")

    # Must NOT contain the old escape helper.
    assert "_escaped_reason" not in arf_block, (
        "agent_runner_reaped_failure still references '_escaped_reason'. "
        "The bash-interpolation fix (#3488) should have removed it — "
        "lift $_reason into a psql -v var instead."
    )

    # Must use psql -v to pass the reason.
    assert re.search(r"-v\s+reason=", arf_block), (
        "agent_runner_reaped_failure does not pass reason via psql -v. "
        "Expected '-v reason=' to lift $_reason out of bash interpolation (#3488)."
    )

    # Must build JSON via jsonb_build_object (no bare string interpolation).
    assert re.search(r"jsonb_build_object", arf_block), (
        "agent_runner_reaped_failure does not use jsonb_build_object to build "
        "the output_json. Expected jsonb_build_object(:'category'::text, "
        ":'reason'::text) instead of bash string interpolation (#3488)."
    )

    # Must reference the psql-quoted variable :'reason' (not $-prefixed bash var).
    assert re.search(r":'reason'", arf_block), (
        "agent_runner_reaped_failure does not reference :'reason' as a psql "
        "variable. The fix must use psql variable substitution so that the "
        "reason value never passes through bash expansion (#3488)."
    )


def test_on_conflict_targets_match() -> None:
    """Every ON CONFLICT target must be exactly (agent_id, phase, attempt).

    This is the unique index added in migration 30.  Any deviation means a
    site is targeting a stale or wrong constraint.
    """
    expected_target = "agent_id, phase, attempt"

    daemon_shape = _extract_daemon_insert()
    entrypoint_shapes = _extract_entrypoint_inserts()

    all_shapes: list[tuple[str, str | None]] = [
        (
            "daemon.py:_persist_phase_output",
            daemon_shape["on_conflict_target"],
        )
    ]
    for shape in entrypoint_shapes:
        all_shapes.append((shape["site"], shape["on_conflict_target"]))

    for site, target in all_shapes:
        assert target is not None, (
            f"[{site}] ON CONFLICT target is None — no ON CONFLICT clause found."
        )
        assert target == expected_target, (
            f"[{site}] ON CONFLICT target is {target!r}, expected {expected_target!r}. "
            "Migration 30 added the (agent_id, phase, attempt) unique index; "
            "all INSERT sites must target it."
        )
