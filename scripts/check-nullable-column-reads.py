#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-nullable-column-reads.py — Detect unguarded reads of columns that
became nullable in a schema migration.

Motivation (#3394, #3396): When a migration drops NOT NULL on a column,
every call site that reads that column without an ``IS NOT NULL`` guard
becomes a latent NoneType bug. This check scans migrations for
``DROP NOT NULL`` statements and then walks the Python source tree for
SELECT-shaped references to those columns that lack an inline NULL guard.

The check:
  1. Finds changed migration files in the current diff vs a base ref
     (or reads ``--migration`` paths directly for local/test use).
  2. Parses each migration for ``ALTER COLUMN <col> DROP NOT NULL``
     statements.
  3. Walks source directories looking for files that reference the
     column in a SELECT context without either:
     (a) an ``IS NOT NULL`` guard in the same string-literal block, or
     (b) a ``# nullable-ok: <reason>`` file-level acknowledgment.
  4. Exits non-zero with a clear message for every flagged file.

Usage:
    scripts/check-nullable-column-reads.py
    scripts/check-nullable-column-reads.py --base origin/main
    scripts/check-nullable-column-reads.py --migration packages/api/migrations/49_foo.sql
    scripts/check-nullable-column-reads.py --help

Exit codes:
    0 — No violations found.
    1 — One or more unguarded read sites found.
    2 — Script error (cannot run git, unreadable file, etc.).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_GLOB = "packages/api/migrations/*.sql"

# Source directories to audit (repo-relative).
_AUDIT_DIRS = [
    "scripts",
    "packages/api/src",
    "packages/scraper-framework/src",
    "packages/nlp-pipeline/src",
    "packages/web",
]

# Regex: match ALTER TABLE ... ALTER COLUMN <col> DROP NOT NULL
# Handles optional schema prefix, multi-word table names, surrounding
# whitespace, and case insensitivity. Captures (table, column).
#
# Pattern:
#   ALTER TABLE [schema.]table ... ALTER COLUMN col DROP NOT NULL
#   (the "..." between ALTER TABLE and ALTER COLUMN can span lines, but we
#   match single-line or allow one optional intermediate clause like TYPE)
_DROP_NOT_NULL_RE = re.compile(
    r"""
    ALTER \s+ TABLE \s+
    (?:[\w]+\.)? ([\w]+)   # optional schema prefix, then table name
    \s+ ALTER \s+ COLUMN \s+
    ([\w]+)                # column name
    (?:\s+\w+)*?           # optional TYPE clause words
    \s+ DROP \s+ NOT \s+ NULL
    """,
    re.VERBOSE | re.IGNORECASE,
)

# SELECT-shaped patterns: a string that looks like it reads a column.
# We look for the column name appearing in one of:
#   - a SELECT list  (SELECT col, ...)
#   - a WHERE clause
#   - a Python dict/tuple access by string key: row["col"] or row['col']
#   - a .get("col") call
# This is deliberately broad — false positives are filtered by the IS NOT NULL
# check below.
_SELECT_SHAPED_RE = re.compile(
    r"""
    (?:
        SELECT \b .* \b {col} \b   # SQL SELECT mentioning the column
        | WHERE \b .* \b {col} \b  # SQL WHERE clause
        | \[ ['"] {col} ['"] \]    # Python dict/row access: row["col"]
        | \.get\( ['"] {col} ['"] \)  # row.get("col")
    )
    """,
    re.VERBOSE | re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class NullableColumn:
    """A column that was made nullable by a migration."""

    table: str
    column: str


@dataclass
class Violation:
    """An unguarded read site for a nullable column."""

    file: Path
    column: str
    table: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers (pure — easy to unit-test)
# ---------------------------------------------------------------------------


def parse_dropped_not_null(sql_text: str) -> list[tuple[str, str]]:
    """Return a list of (table, column) pairs for every ``ALTER COLUMN col
    DROP NOT NULL`` statement in *sql_text*.

    Case-insensitive. Handles optional ``schema.`` prefix on the table name
    (the schema prefix is stripped — callers match on bare table name only,
    since columns are the key discriminator).

    Examples that match:
        ALTER TABLE derived.rulings ALTER COLUMN hearing_date DROP NOT NULL;
        ALTER TABLE dispatcher.agents ALTER COLUMN issue_number DROP NOT NULL;

    Examples that do NOT match:
        ALTER TABLE foo ADD COLUMN bar TEXT;
        ALTER TABLE foo ALTER COLUMN bar TYPE TEXT;
        ALTER TABLE foo ALTER COLUMN bar SET NOT NULL;
    """
    results: list[tuple[str, str]] = []
    for m in _DROP_NOT_NULL_RE.finditer(sql_text):
        table = m.group(1).lower()
        column = m.group(2).lower()
        results.append((table, column))
    return results


def find_changed_migrations(base_ref: str, repo_root: Path) -> list[Path]:
    """Return migration SQL files touched in the diff vs *base_ref*.

    Shells out: ``git diff --name-only <base_ref>...HEAD --
    packages/api/migrations/*.sql``.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--name-only",
            f"{base_ref}...HEAD",
            "--",
            MIGRATIONS_GLOB,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        p = repo_root / line.strip()
        if p.suffix == ".sql" and p.exists():
            paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _file_has_nullable_ok(text: str) -> bool:
    """Return True if the file carries a ``# nullable-ok: <reason>`` line."""
    return bool(re.search(r"#\s*nullable-ok\s*:", text, re.IGNORECASE))


def _text_has_null_guard(text: str, column: str) -> bool:
    """Return True if *text* contains ``<column> IS NOT NULL`` or
    ``IS NOT NULL`` adjacent to the column reference (case-insensitive).
    """
    return bool(
        re.search(
            r"\b" + re.escape(column) + r"\b\s+IS\s+NOT\s+NULL",
            text,
            re.IGNORECASE,
        )
        or re.search(
            r"IS\s+NOT\s+NULL\b.*\b" + re.escape(column) + r"\b",
            text,
            re.IGNORECASE,
        )
    )


def _file_references_column(text: str, column: str) -> bool:
    """Return True if the file text contains the bare column name at all.

    This is a fast pre-filter before the more expensive SELECT-shaped check.
    """
    return bool(re.search(r"\b" + re.escape(column) + r"\b", text, re.IGNORECASE))


def _file_has_select_shaped_reference(text: str, column: str) -> bool:
    """Return True if the text contains a SELECT-shaped reference to *column*.

    We look for any of:
    - A SQL SELECT statement that mentions the column
    - A SQL WHERE clause mentioning the column
    - A Python row/dict access: row["column"] or row.get("column")
    """
    col = re.escape(column)
    patterns = [
        # SQL SELECT mentioning the column
        r"SELECT\b[^;]*\b" + col + r"\b",
        # SQL WHERE clause
        r"WHERE\b[^;]*\b" + col + r"\b",
        # Python dict/row access: row["col"] or row['col']
        r"\[\s*['\"]" + col + r"['\"]\s*\]",
        # .get("col") call
        r"\.get\(\s*['\"]" + col + r"['\"]\s*\)",
    ]
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE | re.DOTALL):
            return True
    return False


def audit_column_reads(
    repo_root: Path,
    columns: list[tuple[str, str]],
) -> list[Violation]:
    """Walk source directories and return violations for unguarded reads of
    *columns* (list of (table, column) pairs).

    A file is flagged when ALL of:
    1. It references the column name (fast pre-filter).
    2. It has a SELECT-shaped reference to the column.
    3. It does NOT have ``<column> IS NOT NULL`` anywhere in the file.
    4. It does NOT carry a ``# nullable-ok: <reason>`` file-level ack.

    Only `.py` files are scanned (SQL templates and TypeScript resolvers are
    out of scope for v1).
    """
    if not columns:
        return []

    violations: list[Violation] = []

    for audit_dir_rel in _AUDIT_DIRS:
        audit_dir = repo_root / audit_dir_rel
        if not audit_dir.is_dir():
            continue
        for py_file in sorted(audit_dir.rglob("*.py")):
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for table, column in columns:
                # Fast pre-filter: does the file mention the column at all?
                if not _file_references_column(text, column):
                    continue

                # SELECT-shaped reference check.
                if not _file_has_select_shaped_reference(text, column):
                    continue

                # File-level ack — skip if present.
                if _file_has_nullable_ok(text):
                    continue

                # NULL guard check — skip if present.
                if _text_has_null_guard(text, column):
                    continue

                violations.append(
                    Violation(
                        file=py_file,
                        column=column,
                        table=table,
                        reason=(
                            f"File references '{column}' in a SELECT context "
                            f"but has no IS NOT NULL guard and no "
                            f"# nullable-ok: annotation."
                        ),
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Detect unguarded reads of columns made nullable by schema migrations."
        )
    )
    ap.add_argument(
        "--base",
        default="origin/main",
        help="Base ref to diff against (default: origin/main).",
    )
    ap.add_argument(
        "--migration",
        dest="migrations",
        action="append",
        default=None,
        metavar="FILE",
        help=(
            "Read this migration file directly (repeatable). When given, git diff "
            "is skipped. Useful for local testing and CI fixtures."
        ),
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (default: auto-detect from git rev-parse --show-toplevel).",
    )
    args = ap.parse_args()

    # -------- Resolve repo root --------
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            repo_root = Path(r.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"ERROR: cannot determine repo root: {exc}", file=sys.stderr)
            return 2

    # -------- Collect migration files --------
    if args.migrations:
        migration_files = [Path(p).resolve() for p in args.migrations]
        for mf in migration_files:
            if not mf.exists():
                print(f"ERROR: migration file not found: {mf}", file=sys.stderr)
                return 2
    else:
        try:
            migration_files = find_changed_migrations(args.base, repo_root)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if not migration_files:
        print("check-nullable-column-reads: no migration files touched in diff — OK.")
        return 0

    # -------- Parse DROP NOT NULL statements --------
    all_columns: list[tuple[str, str]] = []
    for mf in migration_files:
        try:
            sql_text = mf.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"ERROR: cannot read {mf}: {exc}", file=sys.stderr)
            return 2
        dropped = parse_dropped_not_null(sql_text)
        all_columns.extend(dropped)

    if not all_columns:
        print(
            "check-nullable-column-reads: no DROP NOT NULL statements in changed "
            "migrations — OK."
        )
        return 0

    print(
        "check-nullable-column-reads: found DROP NOT NULL for columns: "
        + ", ".join(f"{t}.{c}" for t, c in all_columns)
    )

    # -------- Audit source directories --------
    violations = audit_column_reads(repo_root, all_columns)

    if not violations:
        print("check-nullable-column-reads: no unguarded read sites found — OK.")
        return 0

    print("", file=sys.stderr)
    print(
        "check-nullable-column-reads: FAIL — unguarded reads of nullable columns:",
        file=sys.stderr,
    )
    for v in violations:
        rel = (
            v.file.relative_to(repo_root)
            if v.file.is_relative_to(repo_root)
            else v.file
        )
        print(f"  {rel}: {v.reason}", file=sys.stderr)

    print("", file=sys.stderr)
    print("Remediation options:", file=sys.stderr)
    print(
        "  1. Add an IS NOT NULL filter at the SELECT layer:\n"
        "       WHERE <col> IS NOT NULL\n"
        "     or guard the Python read site:\n"
        "       if row['<col>'] is not None: ...",
        file=sys.stderr,
    )
    print(
        "  2. Add a file-level acknowledgment if NULL is handled elsewhere:\n"
        "       # nullable-ok: <reason explaining how NULL is handled>",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "See https://github.com/judgemind/judgemind/issues/3396 for background.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
