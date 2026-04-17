#!/usr/bin/env python3
"""check-sql-columns.py -- Validate SQL column references in Python scripts.

Scans Python files in scripts/ and packages/ for SQL string literals,
extracts table.column and alias.column references, and validates each
column exists in the schema definition (schema.sql).

This catches bugs like referencing ``documents.updated_at`` when the
``documents`` table has no ``updated_at`` column -- the kind of bug that
caused the runtime failure in #1929.

Usage:
    scripts/check-sql-columns.py                 # scan all Python files
    scripts/check-sql-columns.py --verbose        # show every reference found
    scripts/check-sql-columns.py FILE [FILE ...]  # scan specific files only

Exit codes:
    0 -- No invalid column references found.
    1 -- One or more invalid column references detected.
"""
# permanent: true

from __future__ import annotations

import re
import sys
import tokenize
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema parsing: extract table -> set of column names
# ---------------------------------------------------------------------------


def _up_migration_portion(sql: str) -> str:
    """Return only the up-migration portion of a SQL file.

    Strips everything after ``-- Down Migration`` (case-insensitive).
    """
    for i, line in enumerate(sql.splitlines()):
        if line.strip().lower().startswith("-- down migration"):
            return "\n".join(sql.splitlines()[:i])
    return sql


def _extract_table_columns_from_sql(
    sql: str,
) -> dict[str, set[str]]:
    """Parse SQL text and extract all table -> column mappings.

    Handles:
      1. ``CREATE TABLE [IF NOT EXISTS] [schema.]name (col defs)``
      2. ``ALTER TABLE name ADD [COLUMN] col_name TYPE``

    Only processes the up-migration portion (before ``-- Down Migration``).

    Returns a dict of table -> set of column names.
    """
    sql = _up_migration_portion(sql)
    lower = sql.lower()
    columns: dict[str, set[str]] = {}

    # --- Pass 1: CREATE TABLE blocks ---
    _create_table_re = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?"
        r"([a-z_]+(?:\.[a-z_]+)?)",
    )

    # SQL keywords that should NOT be treated as column names.
    _sql_keywords = frozenset(
        {
            "constraint",
            "check",
            "foreign",
            "primary",
            "unique",
            "references",
            "create",
            "alter",
            "not",
            "null",
            "default",
            "comment",
            "index",
            "on",
            "if",
            "exists",
            "table",
            "type",
            "enum",
            "as",
            "or",
            "and",
            "in",
            "is",
            "set",
            "with",
            "returns",
            "trigger",
            "begin",
            "end",
            "function",
            "language",
            "where",
        }
    )

    # Column definition regex: starts with column_name followed by a type.
    # We match any identifier as the type (including custom enum types like
    # ruling_outcome, document_format, etc.) and filter via _sql_keywords.
    _col_def_re = re.compile(
        r"^\s*([a-z_][a-z0-9_]*)\s+([a-z_][a-z0-9_]*)"
    )

    current_table: str | None = None
    paren_depth = 0

    for line in lower.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue

        # Detect entering a CREATE TABLE block
        m = _create_table_re.search(stripped)
        if m:
            # Strip schema prefix (public., derived., telemetry.) so
            # references like "d.column" resolve against "documents".
            # Preserve "staging." for proper separation.
            raw_name = m.group(1)
            for prefix in ("public.", "derived.", "telemetry."):
                if raw_name.startswith(prefix):
                    raw_name = raw_name[len(prefix):]
                    break
            current_table = raw_name
            columns.setdefault(current_table, set())
            paren_depth = stripped.count("(") - stripped.count(")")
            continue

        if current_table is not None:
            paren_depth += stripped.count("(") - stripped.count(")")

        # Inside a CREATE TABLE block
        if current_table is not None and paren_depth >= 1:
            cm = _col_def_re.match(stripped)
            if cm:
                col_name = cm.group(1)
                type_name = cm.group(2)
                # Both the column name and type must not be SQL keywords.
                # This handles standard types (uuid, text) and custom enum
                # types (ruling_outcome, document_format, etc.).
                if (
                    col_name not in _sql_keywords
                    and type_name not in _sql_keywords
                ):
                    columns[current_table].add(col_name)

        # Detect leaving the CREATE TABLE block
        if current_table is not None and paren_depth <= 0:
            current_table = None

    # --- Pass 2: ALTER TABLE ... ADD [COLUMN] col_name ---
    _alter_add_col_re = re.compile(
        r"alter\s+table\s+([a-z_]+(?:\.[a-z_]+)?)\s+"
        r"add\s+(?:column\s+)?([a-z_][a-z0-9_]*)\s+"
    )

    for line in lower.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        m = _alter_add_col_re.search(stripped)
        if m:
            table = m.group(1)
            col = m.group(2)
            if col not in _sql_keywords and col != "constraint":
                columns.setdefault(table, set())
                columns[table].add(col)

    return columns


def build_table_columns(
    repo_root: Path,
) -> dict[str, set[str]]:
    """Build the table -> columns map from schema.sql and migration files.

    Parses schema.sql first (the primary source), then merges any columns
    added via ALTER TABLE in migration files.

    Returns the merged map of table -> set[str].
    """
    schema_path = repo_root / "packages" / "api" / "src" / "data-access" / "schema.sql"
    migrations_dir = repo_root / "packages" / "api" / "migrations"

    merged: dict[str, set[str]] = {}

    def _merge(source: dict[str, set[str]]) -> None:
        for table, col_set in source.items():
            merged.setdefault(table, set()).update(col_set)

    if schema_path.is_file():
        _merge(
            _extract_table_columns_from_sql(
                schema_path.read_text(encoding="utf-8")
            )
        )

    if migrations_dir.is_dir():
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            _merge(
                _extract_table_columns_from_sql(
                    migration_file.read_text(encoding="utf-8")
                )
            )

    return merged


# Build the map once at import time.
TABLE_COLUMNS: dict[str, set[str]] = build_table_columns(
    Path(__file__).resolve().parent.parent
)

# ---------------------------------------------------------------------------
# SQL string extraction (reuses pattern from check-sql-conflicts.py)
# ---------------------------------------------------------------------------


def _extract_string_tokens(
    content: str,
) -> list[tuple[int, str]]:
    """Extract (start_line, string_value) for all string tokens in Python source.

    Uses the tokenize module so that comments and non-string code are skipped.
    Skips strings that have a ``# sql-check:ignore`` comment on the same line
    or on the immediately preceding line.
    """
    lines = content.splitlines()
    results: list[tuple[int, str]] = []
    try:
        tokens = tokenize.generate_tokens(StringIO(content).readline)
        for tok_type, tok_string, (srow, _scol), (erow, _ecol), _line in tokens:
            if tok_type == tokenize.STRING:
                # Check for suppression comment on start line, end line,
                # preceding line, or line after end
                if _has_ignore_comment(lines, srow) or _has_ignore_comment(
                    lines, erow
                ):
                    continue

                raw = tok_string
                while raw and raw[0] in "fFrRbBuU":
                    raw = raw[1:]
                if raw.startswith('"""') or raw.startswith("'''"):
                    val = raw[3:-3]
                elif raw.startswith('"') or raw.startswith("'"):
                    val = raw[1:-1]
                else:
                    continue
                results.append((srow, val))
    except tokenize.TokenError:
        pass
    return results


def _has_ignore_comment(lines: list[str], line_num: int) -> bool:
    """Check if a line or its predecessor has a ``# sql-check:ignore`` comment.

    Parameters
    ----------
    lines : list[str]
        All lines of the file (0-indexed).
    line_num : int
        1-indexed line number of the token.
    """
    idx = line_num - 1  # Convert to 0-indexed
    for check_idx in (idx, idx - 1):
        if 0 <= check_idx < len(lines):
            if "sql-check:ignore" in lines[check_idx]:
                return True
    return False


# ---------------------------------------------------------------------------
# SQL detection and alias resolution
# ---------------------------------------------------------------------------

# A string is considered SQL only if it contains a complete SQL clause pattern.
# Single SQL keywords in docstrings or URLs are NOT sufficient.
_SQL_CLAUSE_RE = re.compile(
    r"\b("
    r"SELECT\s+\S+.*?\bFROM\b"
    r"|UPDATE\s+\S+.*?\bSET\b"
    r"|INSERT\s+INTO\b"
    r"|DELETE\s+FROM\b"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_sql_string(val: str) -> bool:
    """Return True if the string value looks like it contains SQL.

    Requires a complete SQL clause pattern (SELECT...FROM, UPDATE...SET,
    INSERT INTO, DELETE FROM), not just individual SQL keywords.
    This avoids false positives from docstrings, URLs, and comments.
    """
    return bool(_SQL_CLAUSE_RE.search(val))


# Pattern: FROM/JOIN table_name [AS] alias
_TABLE_ALIAS_RE = re.compile(
    r"(?:FROM|JOIN)\s+"
    r"(?:LATERAL\s*\(.*?\)\s*)?"
    r"([a-z_]+(?:\.[a-z_]+)?)"
    r"(?:\s+AS)?\s+"
    r"([a-z][a-z0-9_]*)"
    r"(?:\s|$|,)",
    re.IGNORECASE | re.DOTALL,
)

# Pattern: UPDATE table_name [alias] SET ...
_UPDATE_TABLE_RE = re.compile(
    r"UPDATE\s+([a-z_]+(?:\.[a-z_]+)?)\s+",
    re.IGNORECASE,
)
_UPDATE_ALIAS_RE = re.compile(
    r"UPDATE\s+([a-z_]+(?:\.[a-z_]+)?)\s+([a-z][a-z0-9_]*)\s+SET\b",
    re.IGNORECASE,
)

# Pattern: INSERT INTO table_name
_INSERT_TABLE_RE = re.compile(
    r"INSERT\s+INTO\s+([a-z_]+(?:\.[a-z_]+)?)",
    re.IGNORECASE,
)

# Pattern: DELETE FROM table_name [alias]
_DELETE_TABLE_RE = re.compile(
    r"DELETE\s+FROM\s+([a-z_]+(?:\.[a-z_]+)?)"
    r"(?:\s+AS\s+|\s+)?"
    r"([a-z][a-z0-9_]*)?",
    re.IGNORECASE,
)

# Pattern: qualified column reference: alias.column or table.column
_QUALIFIED_COL_RE = re.compile(
    r"\b([a-z][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)

# SQL keywords that should not be resolved as alias keywords
_ALIAS_KEYWORD_EXCLUDES = frozenset(
    {
        "on", "where", "set", "and", "or", "not", "as", "in", "is",
        "left", "right", "inner", "outer", "cross", "full", "join",
        "lateral", "true", "false", "null", "limit", "offset", "order",
        "group", "having", "union", "except", "intersect", "values",
        "returning", "using", "natural", "between", "like", "ilike",
        "exists", "case", "when", "then", "else", "end", "do", "nothing",
        "update", "cascade", "restrict", "default", "select", "from",
        "into", "insert", "delete", "create", "alter", "drop",
        "index", "table", "if", "with", "recursive",
    }
)

# Names that look like table aliases but are actually SQL keywords,
# functions, or special qualifiers
_QUALIFIER_EXCLUDES = frozenset(
    {
        "excluded", "new", "old",
        "pg_catalog", "information_schema", "public",
        "staging",  # handled as schema prefix
        # PostgreSQL functions (common ones that could appear as qualifier.arg)
        "gen_random_uuid", "now", "coalesce", "lower", "upper", "trim",
        "length", "count", "sum", "avg", "min", "max", "row_number",
        "rank", "dense_rank", "lag", "lead", "first_value", "last_value",
        "array_agg", "string_agg", "jsonb_build_object", "jsonb_agg",
        "json_agg", "extract", "date_trunc", "to_char", "to_date",
        "to_timestamp", "regexp_replace", "regexp_match", "regexp_matches",
        "substring", "position", "replace", "concat", "format",
        "split_part", "btrim", "ltrim", "rtrim", "chr", "ascii",
        "encode", "decode", "md5", "sha256", "gen_random_bytes",
        "greatest", "least", "abs", "ceil", "floor", "round", "trunc",
        "random", "generate_series", "unnest", "exists", "any", "all",
        "some", "every", "bool_and", "bool_or",
    }
)


def _resolve_aliases(sql_text: str) -> dict[str, str]:
    """Extract alias -> table_name mappings from SQL text.

    Handles FROM, JOIN, UPDATE, INSERT INTO, and DELETE FROM clauses.
    Returns a dict mapping lowercase alias to lowercase table name.
    """
    aliases: dict[str, str] = {}

    # FROM/JOIN aliases
    for m in _TABLE_ALIAS_RE.finditer(sql_text):
        table = m.group(1).lower()
        alias = m.group(2).lower()
        if alias not in _ALIAS_KEYWORD_EXCLUDES:
            aliases[alias] = table

    # UPDATE table [alias] SET -- the table itself and optional alias
    for m in _UPDATE_TABLE_RE.finditer(sql_text):
        table = m.group(1).lower()
        aliases[table] = table
    for m in _UPDATE_ALIAS_RE.finditer(sql_text):
        table = m.group(1).lower()
        alias = m.group(2).lower()
        if alias not in _ALIAS_KEYWORD_EXCLUDES:
            aliases[alias] = table

    # INSERT INTO table
    for m in _INSERT_TABLE_RE.finditer(sql_text):
        table = m.group(1).lower()
        aliases[table] = table

    # DELETE FROM table [alias]
    for m in _DELETE_TABLE_RE.finditer(sql_text):
        table = m.group(1).lower()
        aliases[table] = table
        if m.group(2):
            alias = m.group(2).lower()
            if alias not in _ALIAS_KEYWORD_EXCLUDES:
                aliases[alias] = table

    return aliases


def _extract_column_references(
    sql_text: str,
) -> list[tuple[str, str]]:
    """Extract (qualifier, column) pairs from SQL text.

    Only returns qualified references (alias.column or table.column).
    Filters out function calls and known non-table qualifiers.
    """
    results: list[tuple[str, str]] = []

    for m in _QUALIFIED_COL_RE.finditer(sql_text):
        qualifier = m.group(1).lower()
        column = m.group(2).lower()

        # Skip known non-table qualifiers
        if qualifier in _QUALIFIER_EXCLUDES:
            continue

        # Skip if it looks like a URL (preceded by :// or / or followed by .com/org/gov etc)
        start = m.start()
        if start >= 3 and sql_text[start - 3 : start] == "://":
            continue
        if start >= 1 and sql_text[start - 1] == "/":
            continue
        # Check for domain-like patterns: qualifier.tld (gov, com, org, net, io)
        if column in ("gov", "com", "org", "net", "io", "edu"):
            continue

        # Skip if column looks like a function call (followed by parenthesis)
        end_pos = m.end()
        if end_pos < len(sql_text) and sql_text[end_pos] == "(":
            continue

        # Skip file-extension-like patterns: something.pdf, something.html, etc
        if column in ("pdf", "html", "txt", "csv", "json", "xml", "docx", "py",
                       "sql", "sh", "md", "yml", "yaml", "toml", "cfg", "ini",
                       "log", "tmp", "bak"):
            continue

        results.append((qualifier, column))

    return results


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_column_ref(
    qualifier: str,
    column: str,
    aliases: dict[str, str],
    table_columns: dict[str, set[str]],
) -> str | None:
    """Return an error message if the column reference is invalid, else None.

    Resolves the qualifier via aliases to find the actual table name,
    then checks if the column exists in that table's definition.
    """
    # Resolve alias to table name
    table = aliases.get(qualifier)
    if table is None:
        # Try the qualifier as a direct table name
        if qualifier in table_columns:
            table = qualifier
        else:
            # Unknown qualifier -- could be a CTE, subquery alias, or
            # something we can't resolve. Skip without error to minimize
            # false positives.
            return None

    # Check if the table is known
    cols = table_columns.get(table)
    if cols is None:
        # Unknown table -- skip. Could be a CTE or temp table.
        return None

    # Check if the column exists
    if column in cols:
        return None

    return (
        f"column '{column}' does not exist on table '{table}' "
        f"(referenced as '{qualifier}.{column}'). "
        f"Available columns: {', '.join(sorted(cols))}"
    )


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def scan_file(
    filepath: Path,
    *,
    verbose: bool = False,
    table_columns: dict[str, set[str]] | None = None,
) -> list[str]:
    """Scan a single file for invalid SQL column references.

    Returns a list of error messages (empty if all valid).
    """
    if table_columns is None:
        table_columns = TABLE_COLUMNS

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # File-level suppression: skip entire file if it contains sql-check:skip-file
    if "sql-check:skip-file" in content:
        return []

    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()  # Dedup: (table, column, qualifier)

    for start_line, string_val in _extract_string_tokens(content):
        if not _is_sql_string(string_val):
            continue

        aliases = _resolve_aliases(string_val)
        col_refs = _extract_column_references(string_val)

        for qualifier, column in col_refs:
            err = _validate_column_ref(qualifier, column, aliases, table_columns)

            if verbose and err is None:
                table = aliases.get(qualifier, qualifier)
                print(
                    f"  {filepath.name}:{start_line}: "
                    f"{qualifier}.{column} -> {table}.{column} -- OK"
                )

            if err:
                # Resolve table for dedup key
                table = aliases.get(qualifier, qualifier)
                dedup_key = (table, column, qualifier)
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    # Calculate approximate line number
                    search_str = f"{qualifier}.{column}".lower()
                    idx = string_val.lower().find(search_str)
                    prefix = string_val[:idx] if idx >= 0 else ""
                    line_offset = prefix.count("\n") if prefix else 0
                    errors.append(
                        f"  {filepath.name}:{start_line + line_offset}: {err}"
                    )

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point."""
    verbose = "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--verbose"]

    repo_root = Path(__file__).resolve().parent.parent

    if args:
        files = [Path(a) for a in args]
    else:
        # Scan scripts/*.py and packages/**/*.py
        files = sorted(repo_root.glob("scripts/*.py"))
        files += sorted(repo_root.glob("packages/*/src/**/*.py"))
        files += sorted(repo_root.glob("packages/*/tests/**/*.py"))

    all_errors: list[str] = []

    for filepath in files:
        errors = scan_file(filepath, verbose=verbose)
        all_errors.extend(errors)

    if all_errors:
        print("ERROR: Invalid SQL column references found:\n")
        for err in all_errors:
            print(err)
        print(
            f"\nFound {len(all_errors)} invalid column reference(s).\n"
            "Fix: check the table's column definitions in schema.sql.\n"
            "If the column was added in a migration, ensure it also appears\n"
            "in schema.sql.\n"
            "\nSee https://github.com/judgemind/judgemind/issues/1954 for context."
        )
        return 1

    print(
        f"All clean -- scanned {len(files)} files, "
        f"no invalid column references found."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
