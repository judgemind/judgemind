#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-issue-verify-sql.py — Validate SQL columns referenced in
``Verify:`` lines of a GitHub issue body against the columns defined
in ``packages/api/src/data-access/schema.sql``.

Why this check exists
---------------------
Issue ACs frequently include verification queries of the form::

    Verify: ``SELECT name, schedule, enabled FROM dispatcher.scheduled_skills``

If a column referenced there does not exist on the named table, the
``Verify:`` line errors out at column-not-found when an agent picks the
issue up — and the agent then has to debug whether the implementation
or the AC is wrong before they can begin. That's expensive. The lint
rule documented in ``docs/agent/issue-authoring.md`` §"Verify lines on
SQL queries" (added by #4319) tells authors to grep ``schema.sql``
before filing — but it relies on author discipline. This script is the
automated half: parse ``schema.sql`` into a ``{schema.table: [columns]}``
map, walk the issue body's ``Verify:`` SQL fragments, and report any
column-not-found mismatches.

CLI
---
::

    scripts/check-issue-verify-sql.py --issue 4319            # fetch via gh, validate
    scripts/check-issue-verify-sql.py --body-file body.txt    # validate a local body
    scripts/check-issue-verify-sql.py --schema-sql custom.sql ...  # override schema path

Exit codes
----------
* ``0`` — all SQL fragments validated cleanly.
* ``1`` — at least one column-not-found mismatch.
* ``2`` — parse error (gh fetch failure, schema.sql unreadable, no
  Verify lines couldn't be parsed at all).

Out-of-scope (Phase 2)
----------------------
* Full SQL grammar — table.column extraction uses regex + a simple
  tokenizer.
* Cross-schema-qualified column inference (``r.case_id`` where ``r``
  was aliased earlier as ``derived.rulings`` is supported; deeper
  alias resolution like CTEs is not).
* Validation of ``Verify:`` lines that reference functions or
  schema-rebuild artifacts.

Tracking: issue #4358 (parent: #4319, surfaced via #4309).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SCHEMA_PATH = (
    _REPO_ROOT / "packages" / "api" / "src" / "data-access" / "schema.sql"
)

# Schemas the lint applies to. Extended carefully — every schema added
# here must have a CREATE SCHEMA stanza in schema.sql.
_KNOWN_SCHEMAS = {"derived", "dispatcher", "staging", "telemetry", "public"}

# SQL keywords used in alias / column tokenizing. Lowercased for matching.
_SQL_KEYWORDS = {
    "select",
    "from",
    "where",
    "join",
    "inner",
    "left",
    "right",
    "outer",
    "full",
    "cross",
    "lateral",
    "on",
    "group",
    "order",
    "by",
    "having",
    "limit",
    "offset",
    "as",
    "and",
    "or",
    "not",
    "in",
    "is",
    "null",
    "asc",
    "desc",
    "with",
    "union",
    "all",
    "distinct",
    "case",
    "when",
    "then",
    "else",
    "end",
    "into",
    "values",
    "set",
    "update",
    "delete",
    "insert",
    "explain",
    "between",
    "like",
    "ilike",
    "exists",
    "any",
    "true",
    "false",
    "fetch",
    "first",
    "next",
    "rows",
    "only",
    "returning",
    "using",
    "natural",
}

# Regex helpers.
_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE (\w+)\.(\w+) \($")
_TABLE_END_RE = re.compile(r"^\);")
_COLUMN_LINE_RE = re.compile(r"^\s+(\w+)\b")
_VERIFY_LINE_RE = re.compile(r"^\s*-?\s*Verify:", re.IGNORECASE)

# FROM / JOIN <schema>.<table> [[AS] alias]. Schema is required so we
# don't trip on FROM <cte_name> / FROM (subquery).
# Group 1: schema, Group 2: table, Group 3: alias (optional).
_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(\w+)\.(\w+)(?:\s+(?:AS\s+)?([a-zA-Z_]\w*))?",
    re.IGNORECASE,
)

# <alias>.<column> or <schema>.<table>.<column>. We pre-strip schema.table
# matches before running this so the only multi-segment refs left are
# alias.column.
_ALIAS_COLUMN_RE = re.compile(r"\b([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\b")

# A token that looks like a SQL fragment: SELECT/UPDATE/DELETE/INSERT/EXPLAIN.
_SQL_OPENER_RE = re.compile(
    r"^\s*(SELECT|UPDATE|DELETE|INSERT|EXPLAIN)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# schema.sql parser
# ---------------------------------------------------------------------------


def parse_schema(schema_path: Path) -> dict[str, set[str]]:
    """Return ``{schema.table: {col1, col2, ...}}`` from ``schema.sql``.

    Walk lines: when we see ``CREATE TABLE <schema>.<table> (``,
    accumulate column names from following lines (column name is the
    first identifier on the line, skipping lines starting with
    ``CONSTRAINT``) until we see ``);``.
    """
    text = schema_path.read_text()
    columns: dict[str, set[str]] = defaultdict(set)
    current: str | None = None
    for line in text.splitlines():
        if current is None:
            m = _CREATE_TABLE_RE.match(line)
            if m:
                schema, table = m.group(1), m.group(2)
                current = f"{schema}.{table}"
            continue

        if _TABLE_END_RE.match(line):
            current = None
            continue

        # Skip CONSTRAINT lines.
        stripped = line.lstrip()
        if stripped.upper().startswith("CONSTRAINT"):
            continue

        m = _COLUMN_LINE_RE.match(line)
        if m:
            col = m.group(1)
            # Skip false-positive matches on standalone keywords (no
            # schema.sql column today is named with a SQL keyword, but
            # be defensive).
            if col.lower() not in _SQL_KEYWORDS:
                columns[current].add(col)
    return dict(columns)


# ---------------------------------------------------------------------------
# Issue body extraction
# ---------------------------------------------------------------------------


def fetch_issue_body(issue_number: int) -> str:
    """Return the body text of the GitHub issue via ``gh issue view``."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                "judgemind/judgemind",
                "--json",
                "body",
                "-q",
                ".body",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not installed") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise RuntimeError(
            f"gh issue view failed (exit {exc.returncode}): {stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("gh issue view timed out after 30s") from exc
    return result.stdout


def extract_sql_fragments(body: str) -> list[str]:
    """Return SQL fragments mentioned on ``Verify:`` lines in ``body``.

    A fragment is the content between backticks (single ``...`` or
    fenced ``` ... ```) on or near a ``Verify:`` line where the content
    starts with SELECT/UPDATE/DELETE/INSERT/EXPLAIN.

    The recognizer is intentionally generous: ``Verify:`` lines that
    don't contain SQL (``Verify: pytest -k ...``, ``Verify: ./scripts/foo.sh``,
    ``Verify: grep -n ...``) are silently dropped.
    """
    lines = body.splitlines()
    fragments: list[str] = []

    in_fenced = False
    fenced_buf: list[str] = []
    fenced_after_verify = False

    pending_verify_lines: list[str] = []

    def _flush_pending(verify_lines: list[str]) -> None:
        """Pull ``...`` (single backtick) SQL fragments from a sequence
        of accumulated ``Verify:`` (and continuation) lines."""
        joined = " ".join(verify_lines)
        # Find every backtick-bounded fragment.
        for m in re.finditer(r"`([^`]+)`", joined):
            candidate = m.group(1).strip()
            if _SQL_OPENER_RE.match(candidate):
                fragments.append(candidate)

    for line in lines:
        stripped = line.strip()

        # Fenced code-block boundaries.
        if stripped.startswith("```"):
            if in_fenced:
                # End of fence.
                if fenced_after_verify and fenced_buf:
                    candidate = "\n".join(fenced_buf).strip()
                    if _SQL_OPENER_RE.match(candidate):
                        fragments.append(candidate)
                in_fenced = False
                fenced_buf = []
                fenced_after_verify = False
            else:
                # Start of fence. If we have a pending Verify, the
                # fenced block is its body.
                in_fenced = True
                fenced_buf = []
                fenced_after_verify = bool(pending_verify_lines)
                if pending_verify_lines:
                    _flush_pending(pending_verify_lines)
                    pending_verify_lines = []
            continue

        if in_fenced:
            fenced_buf.append(line)
            continue

        if _VERIFY_LINE_RE.match(line):
            if pending_verify_lines:
                _flush_pending(pending_verify_lines)
            pending_verify_lines = [line]
            continue

        # Continuation of a Verify? Indented continuation lines are
        # rare in our issue templates, but support them: a non-blank
        # line that starts with whitespace and we have pending verify.
        if pending_verify_lines and line and line[0].isspace():
            pending_verify_lines.append(line)
            continue

        if pending_verify_lines:
            _flush_pending(pending_verify_lines)
            pending_verify_lines = []

    # End of body — flush any remaining pending verify.
    if pending_verify_lines:
        _flush_pending(pending_verify_lines)

    return fragments


# ---------------------------------------------------------------------------
# SQL fragment validation
# ---------------------------------------------------------------------------


def _strip_string_literals(sql: str) -> str:
    """Replace single-quoted string literals with ``''`` so the
    tokenizer doesn't pull column-looking tokens out of literal
    content (e.g. ``WHERE name = 'audit-llm-carry-forward'``).

    Doubled single quotes inside a literal (PostgreSQL's escape) are
    handled by greedy/non-greedy regex.
    """
    # Match: '...' where '' is the escape for a single quote. Repeat
    # the inner pattern: chars that aren't ' OR doubled ''.
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def _build_alias_map(sql: str) -> dict[str, str]:
    """Return ``{alias_or_table_name: schema.table}`` for all
    schema-qualified FROM/JOIN references in ``sql``.

    The full table name (``rulings``) also maps to ``derived.rulings``
    so unaliased qualified column refs like ``rulings.case_id`` resolve.
    """
    alias_map: dict[str, str] = {}
    for m in _FROM_JOIN_RE.finditer(sql):
        schema, table, alias = m.group(1), m.group(2), m.group(3)
        if schema.lower() not in _KNOWN_SCHEMAS:
            # Don't validate references against schemas we don't
            # know about (e.g. ``information_schema``,
            # ``pg_catalog``).
            continue
        full = f"{schema}.{table}"
        # The bare table name resolves too unless an alias shadows it.
        alias_map.setdefault(table, full)
        if alias:
            # Alias could collide with a SQL keyword if the parser is
            # too generous (e.g. AS ON). Skip those.
            if alias.lower() in _SQL_KEYWORDS:
                continue
            alias_map[alias] = full
    return alias_map


def _enumerated_tables(sql: str) -> list[str]:
    """Return the list of ``schema.table`` references appearing in
    FROM/JOIN clauses (in source order, deduplicated)."""
    seen: list[str] = []
    for m in _FROM_JOIN_RE.finditer(sql):
        schema, table = m.group(1), m.group(2)
        if schema.lower() not in _KNOWN_SCHEMAS:
            continue
        full = f"{schema}.{table}"
        if full not in seen:
            seen.append(full)
    return seen


def validate_fragment(sql: str, columns: dict[str, set[str]]) -> list[str]:
    """Return a list of human-readable error strings for column
    mismatches in ``sql``. An empty list means the fragment is clean.

    The validation walks two reference shapes:

    1. Qualified ``alias.column`` or ``table.column`` — looked up via
       the alias map built from the fragment's FROM/JOIN clauses.
    2. Unqualified column tokens after SELECT/WHERE/SET — only
       validated when the fragment names exactly one table, since
       multi-table joins make unqualified resolution ambiguous (out
       of scope per #4358 AC).
    """
    sql_clean = _strip_string_literals(sql)
    alias_map = _build_alias_map(sql_clean)
    if not alias_map:
        # No known-schema-qualified FROM/JOIN — nothing to validate.
        return []

    errors: list[str] = []

    # ---- 1. Qualified alias.column references. ----
    # Strip schema.table mentions in FROM/JOIN before scanning so
    # they aren't re-detected as alias.column refs.
    sql_for_alias_scan = _FROM_JOIN_RE.sub(" ", sql_clean)
    for m in _ALIAS_COLUMN_RE.finditer(sql_for_alias_scan):
        prefix, col = m.group(1), m.group(2)
        # Skip schema.table refs that survived (shouldn't happen, but
        # defense-in-depth).
        if prefix.lower() in _KNOWN_SCHEMAS:
            continue
        # Skip prefixes that aren't in our alias map — those are CTE
        # aliases, JSON paths, function calls, etc.
        if prefix not in alias_map:
            continue
        # Skip column tokens that are SQL keywords.
        if col.lower() in _SQL_KEYWORDS:
            continue
        full = alias_map[prefix]
        cols = columns.get(full)
        if cols is None:
            errors.append(
                f"unknown table {full} (referenced via {prefix}.{col} — "
                f"not in schema.sql)"
            )
            continue
        if col not in cols:
            errors.append(
                f"column {full}.{col} does not exist (referenced as {prefix}.{col})"
            )

    # ---- 2. Unqualified column references (single-table FROM only). ----
    tables = _enumerated_tables(sql_clean)
    if len(tables) == 1:
        single_table = tables[0]
        cols = columns.get(single_table)
        if cols is None:
            errors.append(f"unknown table {single_table} (not in schema.sql)")
        else:
            unqual_errors = _validate_unqualified_columns(sql_clean, single_table, cols)
            errors.extend(unqual_errors)

    return errors


def _validate_unqualified_columns(
    sql: str, table_full: str, cols: set[str]
) -> list[str]:
    """Validate unqualified column tokens in SELECT, WHERE, and SET
    clauses against the single-table ``cols`` set."""
    errors: list[str] = []

    # Helper: extract the substring between a starting keyword and any
    # of a set of stopping keywords (case-insensitive). Returns "" if
    # the start keyword isn't present.
    def _slice(text: str, start: str, stops: list[str]) -> str:
        m = re.search(rf"\b{start}\b", text, re.IGNORECASE)
        if not m:
            return ""
        rest = text[m.end() :]
        if not stops:
            return rest
        stop_pat = "|".join(stops)
        stop_match = re.search(rf"\b(?:{stop_pat})\b", rest, re.IGNORECASE)
        if stop_match:
            return rest[: stop_match.start()]
        return rest

    select_clause = _slice(sql, "SELECT", ["FROM", "INTO"])
    where_clause = _slice(
        sql,
        "WHERE",
        ["GROUP", "ORDER", "LIMIT", "HAVING", "RETURNING", "FETCH"],
    )
    set_clause = _slice(
        sql,
        "SET",
        ["WHERE", "RETURNING", "FROM"],
    )

    candidates: list[tuple[str, str]] = []  # (clause_label, identifier)
    candidates.extend(("SELECT", c) for c in _identifiers_in_select(select_clause))
    candidates.extend(("WHERE", c) for c in _identifiers_in_predicate(where_clause))
    candidates.extend(("SET", c) for c in _identifiers_in_set(set_clause))

    seen: set[tuple[str, str]] = set()
    for clause, ident in candidates:
        if ident in seen:
            continue
        seen.add((clause, ident))
        if ident.lower() in _SQL_KEYWORDS:
            continue
        # Skip numeric literals.
        if ident.isdigit():
            continue
        if ident in cols:
            continue
        errors.append(
            f"column {table_full}.{ident} does not exist "
            f"(unqualified reference in {clause} clause)"
        )
    return errors


def _identifiers_in_select(clause: str) -> list[str]:
    """Return the identifiers in a SELECT clause, ignoring qualified
    refs (``table.col``), function calls, and stars."""
    if not clause:
        return []
    # Drop * and parentheses — we don't validate function args.
    parts = [p.strip() for p in clause.split(",")]
    out: list[str] = []
    for part in parts:
        # Strip "AS alias" tail.
        m = re.match(r"^([^\s]+)", part)
        if not m:
            continue
        token = m.group(1)
        # Skip qualified refs (alias.col handled separately).
        if "." in token:
            continue
        # Skip wildcards / function calls.
        if "*" in token or "(" in token:
            continue
        # Plain identifier?
        if re.match(r"^[a-zA-Z_]\w*$", token):
            out.append(token)
    return out


def _identifiers_in_predicate(clause: str) -> list[str]:
    """Return left-hand-side identifiers of equality / IN / IS NULL
    predicates in a WHERE clause. We only check LHS to avoid string-
    literal RHS false-positives.

    Examples:
        WHERE name = 'foo'    -> ['name']
        WHERE col1 = col2     -> ['col1']  (col2 likely from another table or literal)
        WHERE x IS NULL       -> ['x']
        WHERE x IN ('a','b')  -> ['x']
    """
    if not clause:
        return []
    out: list[str] = []
    # Match: identifier followed by = / != / <> / IS / IN / </> /
    # >=/<=. Ignore qualified table.column refs (handled separately).
    for m in re.finditer(
        r"\b([a-zA-Z_]\w*)\s*(?:=|!=|<>|<=?|>=?|\b(?:IS|IN|LIKE|ILIKE)\b)",
        clause,
        re.IGNORECASE,
    ):
        ident = m.group(1)
        out.append(ident)
    # Strip qualified-LHS: a token that immediately follows a "." is
    # actually the second half of alias.col (handled separately).
    # Re-scan to drop those.
    filtered: list[str] = []
    for m in re.finditer(
        r"(\.?)\s*([a-zA-Z_]\w*)\s*(?:=|!=|<>|<=?|>=?|\b(?:IS|IN|LIKE|ILIKE)\b)",
        clause,
        re.IGNORECASE,
    ):
        if m.group(1) == ".":
            continue
        filtered.append(m.group(2))
    return filtered


def _identifiers_in_set(clause: str) -> list[str]:
    """Return left-hand-side identifiers of ``col = expr`` pairs in a
    SET clause."""
    if not clause:
        return []
    out: list[str] = []
    parts = [p.strip() for p in clause.split(",")]
    for part in parts:
        m = re.match(r"^([a-zA-Z_]\w*)\s*=", part)
        if m:
            out.append(m.group(1))
    return out


# ---------------------------------------------------------------------------
# Top-level: tie it together
# ---------------------------------------------------------------------------


def check_body(body: str, columns: dict[str, set[str]]) -> list[str]:
    """Return a flat list of error messages for every SQL fragment in
    ``body`` that has a column-not-found mismatch."""
    fragments = extract_sql_fragments(body)
    errors: list[str] = []
    for i, frag in enumerate(fragments, start=1):
        for err in validate_fragment(frag, columns):
            errors.append(f"  fragment #{i}: {err}\n    SQL: {frag}")
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate SQL columns referenced in Verify: lines of a "
            "GitHub issue body against schema.sql."
        )
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--issue",
        type=int,
        help="GitHub issue number (fetched via gh issue view).",
    )
    src.add_argument(
        "--body-file",
        type=Path,
        help="Path to a local file containing the issue body.",
    )
    parser.add_argument(
        "--schema-sql",
        type=Path,
        default=_DEFAULT_SCHEMA_PATH,
        help=f"Path to schema.sql (default: {_DEFAULT_SCHEMA_PATH}).",
    )
    args = parser.parse_args(argv)

    # Parse schema.sql.
    if not args.schema_sql.exists():
        sys.stderr.write(f"ERROR: schema.sql not found at {args.schema_sql}\n")
        return 2
    try:
        columns = parse_schema(args.schema_sql)
    except OSError as exc:
        sys.stderr.write(f"ERROR: failed to read schema.sql: {exc}\n")
        return 2

    # Load issue body.
    if args.issue is not None:
        try:
            body = fetch_issue_body(args.issue)
        except RuntimeError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2
        source_label = f"issue #{args.issue}"
    else:
        try:
            body = args.body_file.read_text()
        except OSError as exc:
            sys.stderr.write(
                f"ERROR: failed to read body file {args.body_file}: {exc}\n"
            )
            return 2
        source_label = str(args.body_file)

    errors = check_body(body, columns)
    if errors:
        sys.stderr.write(f"ERROR: SQL column mismatches in {source_label}:\n\n")
        for err in errors:
            sys.stderr.write(err + "\n")
        sys.stderr.write(
            "\nFix: update the Verify: line(s) above to use real columns. "
            "See packages/api/src/data-access/schema.sql for the canonical "
            "column list, or run scripts/dev-db-query.sh against the dev DB. "
            "Tracking: issue #4358.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
