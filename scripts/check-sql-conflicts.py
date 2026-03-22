#!/usr/bin/env python3
"""check-sql-conflicts.py -- Validate ON CONFLICT targets in Python scripts.

Scans Python files in scripts/ and packages/ for SQL ON CONFLICT clauses
inside string literals and cross-references each conflict target against the
schema definition to verify a matching UNIQUE constraint exists.

This catches a class of bugs where agents write ON CONFLICT (col1, col2) but
the target table has no matching UNIQUE constraint -- the script compiles fine
but fails at runtime on first execution.  See #1524 for the motivating example.

Usage:
    scripts/check-sql-conflicts.py                 # scan all Python files
    scripts/check-sql-conflicts.py --verbose        # show every ON CONFLICT found
    scripts/check-sql-conflicts.py FILE [FILE ...]  # scan specific files only

Exit codes:
    0 -- No invalid ON CONFLICT targets found.
    1 -- One or more invalid ON CONFLICT targets detected.
"""

from __future__ import annotations

import re
import sys
import tokenize
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema: auto-generated UNIQUE constraints per table
# ---------------------------------------------------------------------------
# This map is auto-generated from schema.sql + migration files at startup.
# It is never manually maintained.  See _extract_unique_constraints_from_sql()
# and build_unique_constraints() for the parsing logic.
#
# Format:  table_name -> list of frozenset(column_names)
#   - Each frozenset represents one UNIQUE constraint.
#   - Primary keys are included (they are implicitly UNIQUE).
#   - "ON CONFLICT DO NOTHING" (no target) is always valid if the table has
#     at least one unique constraint -- Postgres will use any constraint.


def _up_migration_portion(sql: str) -> str:
    """Return only the up-migration portion of a SQL file.

    Strips everything after ``-- Down Migration`` (case-insensitive) so
    that DROP and rollback statements are not parsed.  If the marker is
    absent the full text is returned.
    """
    for i, line in enumerate(sql.splitlines()):
        if line.strip().lower().startswith("-- down migration"):
            return "\n".join(sql.splitlines()[:i])
    return sql


def _normalize_sql_statements(sql: str) -> list[str]:
    """Split SQL into logical statements with continuation lines joined.

    Each returned string is a single logical SQL statement (lowercased) with
    multi-line continuations collapsed into one line.  Comments and blank
    lines are removed.  This allows regexes to match patterns that span
    multiple physical lines in the source.
    """
    lower = sql.lower()
    statements: list[str] = []
    current: list[str] = []

    # SQL keywords that start a new top-level statement
    _stmt_start_re = re.compile(
        r"^\s*(create|alter|insert|delete|update|drop|comment|grant|revoke)\b"
    )

    for line in lower.splitlines():
        stripped = line.strip()
        # Skip comments and blank lines
        if not stripped or stripped.startswith("--"):
            if current:
                statements.append(" ".join(current))
                current = []
            continue

        if _stmt_start_re.match(stripped) and current:
            # New statement starting -- flush the previous one
            statements.append(" ".join(current))
            current = [stripped]
        elif current:
            # Continuation line
            current.append(stripped)
        else:
            # First line of a new statement
            current.append(stripped)

    if current:
        statements.append(" ".join(current))

    return statements


def _extract_unique_constraints_from_sql(
    sql: str,
) -> dict[str, list[frozenset[str]]]:
    """Parse SQL text and extract all UNIQUE constraints.

    Handles:
      1. ``col TYPE PRIMARY KEY``           -- single-column inline PK
      2. ``PRIMARY KEY (col1, col2)``        -- composite PK
      3. ``col TYPE UNIQUE``                 -- single-column inline UNIQUE
      4. ``UNIQUE (col1, col2)``             -- anonymous multi-column UNIQUE
      5. ``CONSTRAINT name UNIQUE (cols)``   -- named UNIQUE constraint
      6. ``ALTER TABLE t ADD CONSTRAINT name UNIQUE (cols)``
      7. ``CREATE UNIQUE INDEX ... ON t (cols)``

    Only processes the up-migration portion (before ``-- Down Migration``).

    Returns a dict of table -> list[frozenset[str]].  Each frozenset is one
    constraint's column set.  Duplicate constraints are de-duplicated.
    """
    sql = _up_migration_portion(sql)
    constraints: dict[str, list[frozenset[str]]] = {}

    def _add(table: str, cols: frozenset[str]) -> None:
        table = table.lower().strip()
        lst = constraints.setdefault(table, [])
        if cols not in lst:
            lst.append(cols)

    # Pre-compile regexes used below
    # Regex: CREATE TABLE [IF NOT EXISTS] [schema.]name
    _create_table_re = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?"
        r"([a-z_]+(?:\.[a-z_]+)?)",
    )
    # Regex: ALTER TABLE t ADD CONSTRAINT name UNIQUE (cols)
    _alter_unique_re = re.compile(
        r"alter\s+table\s+([a-z_]+(?:\.[a-z_]+)?)\s+"
        r"add\s+constraint\s+\S+\s+unique\s*\(\s*([^)]+)\)",
    )
    # Regex: CREATE UNIQUE INDEX ... ON t (cols)
    _create_unique_idx_re = re.compile(
        r"create\s+unique\s+index\s+(?:if\s+not\s+exists\s+)?\S+\s+"
        r"on\s+([a-z_]+(?:\.[a-z_]+)?)\s*\(\s*([^)]+)\)",
    )

    # --- Pass 1: Handle ALTER TABLE and CREATE UNIQUE INDEX ------------------
    # These are top-level statements that may span multiple lines.
    # We use _normalize_sql_statements to join continuation lines.
    for stmt in _normalize_sql_statements(sql):
        # ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (cols)
        m = _alter_unique_re.search(stmt)
        if m:
            table = m.group(1)
            cols = frozenset(c.strip() for c in m.group(2).split(",") if c.strip())
            _add(table, cols)
            continue

        # CREATE UNIQUE INDEX ... ON table (cols)
        m = _create_unique_idx_re.search(stmt)
        if m:
            table = m.group(1)
            cols = frozenset(c.strip() for c in m.group(2).split(",") if c.strip())
            _add(table, cols)
            continue

    # --- Pass 2: Handle CREATE TABLE blocks ----------------------------------
    # Process line-by-line to track paren depth and attribute inline
    # constraints to the correct table.
    lower = sql.lower()
    current_table: str | None = None
    paren_depth = 0

    # Regex: column definition with inline PRIMARY KEY
    _col_pk_re = re.compile(
        r"^\s*([a-z_]+)\s+\S+.*\bprimary\s+key\b",
    )
    # Regex: column definition with inline UNIQUE
    _col_unique_re = re.compile(
        r"^\s*([a-z_]+)\s+\S+.*\bunique\b",
    )
    # Regex: table-level PRIMARY KEY (col1, col2, ...)
    _table_pk_re = re.compile(
        r"\bprimary\s+key\s*\(\s*([^)]+)\)",
    )
    # Regex: table-level UNIQUE (col1, col2) or CONSTRAINT name UNIQUE (cols)
    _table_unique_re = re.compile(
        r"(?:constraint\s+\S+\s+)?unique\s*\(\s*([^)]+)\)",
    )

    # SQL keywords that should not be mistaken for column names
    _sql_keywords = frozenset(
        {
            "primary",
            "unique",
            "constraint",
            "check",
            "foreign",
            "references",
            "create",
            "alter",
            "not",
            "null",
            "default",
        }
    )

    for line in lower.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue

        # Detect entering a CREATE TABLE block
        m = _create_table_re.search(stripped)
        if m:
            current_table = m.group(1)
            paren_depth = stripped.count("(") - stripped.count(")")
        else:
            if current_table is not None:
                paren_depth += stripped.count("(") - stripped.count(")")

        # Inside a CREATE TABLE block
        if current_table is not None and paren_depth >= 1:
            is_col_def = False

            # Inline PRIMARY KEY on a column
            m = _col_pk_re.match(stripped)
            if m:
                col = m.group(1)
                if col not in _sql_keywords:
                    _add(current_table, frozenset({col}))
                    is_col_def = True

            # Inline UNIQUE on a column
            m = _col_unique_re.match(stripped)
            if m:
                col = m.group(1)
                if col not in _sql_keywords:
                    _add(current_table, frozenset({col}))
                    is_col_def = True

            # Table-level PRIMARY KEY (col1, col2)
            m = _table_pk_re.search(stripped)
            if m:
                cols = frozenset(c.strip() for c in m.group(1).split(",") if c.strip())
                _add(current_table, cols)

            # Table-level UNIQUE or CONSTRAINT name UNIQUE
            # Only if this is NOT a column definition (to avoid
            # double-counting "email TEXT UNIQUE" as a table-level UNIQUE).
            if not is_col_def:
                for m in _table_unique_re.finditer(stripped):
                    cols = frozenset(
                        c.strip() for c in m.group(1).split(",") if c.strip()
                    )
                    _add(current_table, cols)

        # Detect leaving the CREATE TABLE block
        if current_table is not None and paren_depth <= 0:
            current_table = None

    return constraints


def build_unique_constraints(
    repo_root: Path,
) -> dict[str, list[frozenset[str]]]:
    """Build the UNIQUE_CONSTRAINTS map from schema.sql and migration files.

    Parses all SQL sources and merges the results.  The schema.sql file is
    the primary source; migration files contribute constraints added via
    ALTER TABLE or CREATE UNIQUE INDEX that might not appear in schema.sql.

    Returns the merged map of table -> list[frozenset[str]].
    """
    schema_path = repo_root / "packages" / "api" / "src" / "data-access" / "schema.sql"
    migrations_dir = repo_root / "packages" / "api" / "migrations"

    merged: dict[str, list[frozenset[str]]] = {}

    def _merge(source: dict[str, list[frozenset[str]]]) -> None:
        for table, constraint_list in source.items():
            lst = merged.setdefault(table, [])
            for cols in constraint_list:
                if cols not in lst:
                    lst.append(cols)

    # Parse schema.sql (always present for local dev)
    if schema_path.is_file():
        _merge(
            _extract_unique_constraints_from_sql(
                schema_path.read_text(encoding="utf-8")
            )
        )

    # Parse migration files
    if migrations_dir.is_dir():
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            _merge(
                _extract_unique_constraints_from_sql(
                    migration_file.read_text(encoding="utf-8")
                )
            )

    return merged


# Build the map once at import time.  This replaces the old hardcoded dict.
# Uses repo_root derived from this script's location.
UNIQUE_CONSTRAINTS: dict[str, list[frozenset[str]]] = build_unique_constraints(
    Path(__file__).resolve().parent.parent
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Match "INSERT INTO <table>" -- captures the table name.
# Handles optional schema prefix like staging.captures.
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+"
    r"(?P<table>[a-z_]+(?:\.[a-z_]+)?)",
    re.IGNORECASE,
)

# Match "ON CONFLICT (<columns>)" -- captures the comma-separated column list.
_ON_CONFLICT_COLS_RE = re.compile(
    r"ON\s+CONFLICT\s*\(\s*(?P<cols>[a-z_]+(?:\s*,\s*[a-z_]+)*)\s*\)",
    re.IGNORECASE,
)

# Match bare "ON CONFLICT DO NOTHING" or "ON CONFLICT DO UPDATE" (no target).
_ON_CONFLICT_BARE_RE = re.compile(
    r"ON\s+CONFLICT\s+DO\s+(?:NOTHING|UPDATE)",
    re.IGNORECASE,
)


def _extract_string_tokens(
    content: str,
) -> list[tuple[int, str]]:
    """Extract (start_line, string_value) for all string tokens in Python source.

    Uses the tokenize module so that comments and non-string code are skipped.
    Only STRING tokens are returned -- these are the places where SQL lives.
    """
    results: list[tuple[int, str]] = []
    try:
        tokens = tokenize.generate_tokens(StringIO(content).readline)
        for tok_type, tok_string, (srow, _scol), _end, _line in tokens:
            if tok_type == tokenize.STRING:
                # Strip the string delimiters and any prefix (f, r, b, u).
                # We don't need to fully evaluate -- just get the raw text
                # content to scan for SQL patterns.
                raw = tok_string
                # Remove string prefixes
                while raw and raw[0] in "fFrRbBuU":
                    raw = raw[1:]
                # Remove triple-quote or single-quote delimiters
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


def _extract_conflicts_from_strings(
    content: str,
) -> list[tuple[int, str, frozenset[str] | None]]:
    """Extract (line_number, table_name, conflict_columns) from string literals.

    Only scans inside Python string tokens (SQL queries), not comments or
    docstrings used as documentation.  A string is considered a SQL string if
    it contains both INSERT INTO and ON CONFLICT.
    """
    results: list[tuple[int, str, frozenset[str] | None]] = []

    for start_line, string_val in _extract_string_tokens(content):
        # Skip strings that do not contain ON CONFLICT at all
        if "ON CONFLICT" not in string_val.upper():
            continue

        # Only process strings that contain INSERT INTO ... ON CONFLICT
        # (i.e. actual SQL statements, not doc references)
        insert_match = _INSERT_RE.search(string_val)
        if not insert_match:
            continue

        table = insert_match.group("table").lower()

        # Look for ON CONFLICT with column targets
        for m in _ON_CONFLICT_COLS_RE.finditer(string_val):
            cols_str = m.group("cols")
            cols = frozenset(c.strip().lower() for c in cols_str.split(","))
            # Calculate line number within the string
            prefix = string_val[: m.start()]
            line_offset = prefix.count("\n")
            results.append((start_line + line_offset, table, cols))

        # Look for bare ON CONFLICT DO NOTHING/UPDATE
        for m in _ON_CONFLICT_BARE_RE.finditer(string_val):
            # Skip if this is already covered by a columns match
            if _ON_CONFLICT_COLS_RE.match(string_val, m.start()):
                continue
            prefix = string_val[: m.start()]
            line_offset = prefix.count("\n")
            results.append((start_line + line_offset, table, None))

    return results


def _validate_conflict(
    table: str,
    cols: frozenset[str] | None,
) -> str | None:
    """Return an error message if the ON CONFLICT target is invalid, else None."""
    constraints = UNIQUE_CONSTRAINTS.get(table)
    if constraints is None:
        return f"unknown table '{table}' (not in schema reference)"

    if cols is None:
        # Bare "ON CONFLICT DO NOTHING" -- valid if table has any unique constraint
        return None

    if cols in constraints:
        return None

    # Build a helpful message listing valid targets
    valid = ", ".join("(" + ", ".join(sorted(c)) + ")" for c in constraints)
    return (
        f"no UNIQUE constraint on ({', '.join(sorted(cols))}) "
        f"-- valid targets for '{table}': {valid}"
    )


def scan_file(filepath: Path, *, verbose: bool = False) -> list[str]:
    """Scan a single file for invalid ON CONFLICT targets.

    Returns a list of error messages (empty if all valid).
    """
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    conflicts = _extract_conflicts_from_strings(content)
    errors: list[str] = []

    for line_num, table, cols in conflicts:
        err = _validate_conflict(table, cols)
        if verbose and err is None:
            if cols is not None:
                cols_str = ", ".join(sorted(cols))
                print(
                    f"  {filepath.name}:{line_num}: "
                    f"ON CONFLICT ({cols_str}) on {table} -- OK"
                )
            else:
                print(
                    f"  {filepath.name}:{line_num}: "
                    f"ON CONFLICT DO NOTHING on {table} -- OK"
                )

        if err:
            errors.append(f"  {filepath.name}:{line_num}: {err}")

    return errors


def main() -> int:
    """Entry point."""
    verbose = "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--verbose"]

    repo_root = Path(__file__).resolve().parent.parent

    if args:
        # Scan specific files
        files = [Path(a) for a in args]
    else:
        # Scan scripts/*.py and packages/**/*.py
        files = sorted(repo_root.glob("scripts/*.py"))
        files += sorted(repo_root.glob("packages/*/src/**/*.py"))
        files += sorted(repo_root.glob("packages/*/tests/**/*.py"))

    all_errors: list[str] = []
    total_valid = 0

    for filepath in files:
        errors = scan_file(filepath, verbose=verbose)
        all_errors.extend(errors)
        # Count valid conflicts for summary
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
            conflicts = _extract_conflicts_from_strings(content)
            total_valid += sum(
                1
                for _, table, cols in conflicts
                if _validate_conflict(table, cols) is None
            )
        except OSError:
            pass

    if all_errors:
        print("ERROR: Invalid ON CONFLICT targets found:\n")
        for err in all_errors:
            print(err)
        print(
            f"\nFound {len(all_errors)} invalid ON CONFLICT target(s).\n"
            "Fix: check the table's UNIQUE constraints in schema.sql and\n"
            "docs/agent/db-schema-reference.md before writing ON CONFLICT clauses.\n"
            "The UNIQUE_CONSTRAINTS map is auto-generated from schema.sql and\n"
            "migration files -- if a new constraint was added, ensure it appears\n"
            "in schema.sql or a migration file.\n"
            "\nSee https://github.com/judgemind/judgemind/issues/1566 for context."
        )
        return 1

    total_checked = total_valid + len(all_errors)
    print(
        f"All clean -- scanned {len(files)} files, "
        f"validated {total_checked} ON CONFLICT clause(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
