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
# Schema: known UNIQUE constraints per table
# ---------------------------------------------------------------------------
# This map is the source of truth for what ON CONFLICT targets are valid.
# It is derived from schema.sql + migration files.  When a new migration adds
# or removes a UNIQUE constraint, update this map.
#
# Format:  table_name -> list of frozenset(column_names)
#   - Each frozenset represents one UNIQUE constraint.
#   - Primary keys are included (they are implicitly UNIQUE).
#   - "ON CONFLICT DO NOTHING" (no target) is always valid if the table has
#     at least one unique constraint -- Postgres will use any constraint.

UNIQUE_CONSTRAINTS: dict[str, list[frozenset[str]]] = {
    "courts": [
        frozenset({"id"}),
        frozenset({"court_code"}),
    ],
    "judges": [
        frozenset({"id"}),
        frozenset({"canonical_name", "court_id"}),
    ],
    "judge_aliases": [
        frozenset({"id"}),
    ],
    "attorneys": [
        frozenset({"id"}),
    ],
    "attorney_aliases": [
        frozenset({"id"}),
    ],
    "parties": [
        frozenset({"id"}),
    ],
    "party_aliases": [
        frozenset({"id"}),
    ],
    "cases": [
        frozenset({"id"}),
        frozenset({"court_id", "case_number"}),
    ],
    "case_judges": [
        frozenset({"case_id", "judge_id"}),
    ],
    "case_attorneys": [
        frozenset({"id"}),
        frozenset({"case_id", "attorney_id", "role"}),
    ],
    "case_parties": [
        frozenset({"id"}),
        frozenset({"case_id", "party_id", "role"}),
    ],
    "documents": [
        frozenset({"id"}),
    ],
    "rulings": [
        frozenset({"id"}),
        frozenset({"document_id"}),
        # Partial unique index: (case_id, ruling_text_hash) WHERE ruling_text_hash IS NOT NULL
        frozenset({"case_id", "ruling_text_hash"}),
    ],
    "staging.captures": [
        frozenset({"id"}),
    ],
    "staging.ruled_items": [
        frozenset({"id"}),
    ],
    "court_directory_snapshots": [
        frozenset({"id"}),
    ],
    "scraper_runs": [
        frozenset({"id"}),
    ],
    "data_quality_metrics": [
        frozenset({"id"}),
    ],
    "users": [
        frozenset({"id"}),
        frozenset({"email"}),
        frozenset({"google_id"}),
        frozenset({"api_key"}),
    ],
    "refresh_tokens": [
        frozenset({"id"}),
        frozenset({"token_hash"}),
    ],
    "alert_subscriptions": [
        frozenset({"id"}),
    ],
    "alert_events": [
        frozenset({"id"}),
    ],
}

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
            "If a new constraint was added via migration, update the\n"
            "UNIQUE_CONSTRAINTS map in scripts/check-sql-conflicts.py.\n"
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
