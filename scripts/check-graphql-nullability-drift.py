#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-graphql-nullability-drift.py — Detect GraphQL fields that are still
declared non-null after the backing DB column had NOT NULL dropped.

Motivation (#3441): When a migration drops NOT NULL on a column that backs a
GraphQL field, the field declaration must be updated from ``Type!`` to
``Type`` in the same PR. If it is not, the GraphQL serializer throws at
runtime when the column returns NULL for the first time — crashing the entire
query, not just the one field.

The check:
  1. Finds changed migration files in the current diff vs a base ref
     (or reads ``--migration`` paths directly for local/test use).
  2. Parses each migration for ``ALTER COLUMN <col> DROP NOT NULL``
     statements.
  3. Looks up each (table, column) pair in ``KNOWN_MAPPINGS`` to find the
     GraphQL type(s) and field(s) that read from that column.
  4. Scans ``packages/api/src/graphql/dispatcher/schema.ts`` (and the
     root schema.ts) for each field declaration and checks whether it
     still ends with ``!`` (non-null marker).
  5. Exits non-zero with a clear message naming the migration file, the
     column, and the field declaration line if drift is found.

Usage:
    scripts/check-graphql-nullability-drift.py
    scripts/check-graphql-nullability-drift.py --base origin/main
    scripts/check-graphql-nullability-drift.py --migration packages/api/migrations/49_foo.sql
    scripts/check-graphql-nullability-drift.py --help

Exit codes:
    0 — No violations found.
    1 — One or more GraphQL fields still declare non-null for a dropped-NOT-NULL column.
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

# GraphQL schema files to scan (repo-relative), in priority order.
_SCHEMA_FILES = [
    "packages/api/src/graphql/dispatcher/schema.ts",
    "packages/api/src/graphql/schema.ts",
]

# Allowlist: maps (table, column) → list of (GraphQLType, fieldName) that
# read from that column.  Schema prefix is stripped from the table name
# (matches bare table name only).
#
# Seeded from issue #3441: the three dispatcher columns that had NOT NULL
# dropped in migrations 49–51.  Grow this list as new column→field
# mappings are established.
KNOWN_MAPPINGS: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("agents", "issue_number"): [
        ("DispatcherAgent", "issueNumber"),
        ("RecentCompletion", "issueNumber"),
    ],
    ("agents", "priority"): [
        ("DispatcherAgent", "priority"),
        ("RecentCompletion", "priority"),
        ("QueueItem", "priority"),
    ],
    ("failures", "issue_number"): [
        ("DispatcherFailure", "issueNumber"),
    ],
}

# Regex: match ALTER TABLE ... ALTER COLUMN <col> DROP NOT NULL
# Handles optional schema prefix, multi-word table names, surrounding
# whitespace, and case insensitivity. Captures (table, column).
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

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GraphQLDriftViolation:
    """A GraphQL field that is still non-null after the backing column had
    NOT NULL dropped."""

    migration_file: Path
    table: str
    column: str
    graphql_type: str
    field_name: str
    schema_file: Path
    line_number: int
    line_text: str


# ---------------------------------------------------------------------------
# Parsing helpers (pure — easy to unit-test)
# ---------------------------------------------------------------------------


def parse_dropped_not_null(sql_text: str) -> list[tuple[str, str]]:
    """Return a list of (table, column) pairs for every ``ALTER COLUMN col
    DROP NOT NULL`` statement in *sql_text*.

    Case-insensitive. Handles optional ``schema.`` prefix on the table name
    (the schema prefix is stripped — callers match on bare table name only).

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


def check_field_nonnull(
    schema_text: str,
    schema_file: Path,
    graphql_type: str,
    field_name: str,
) -> tuple[bool, int, str]:
    """Check whether *field_name* on *graphql_type* is still declared non-null
    (ends with ``!``) in *schema_text*.

    Returns a 3-tuple:
      - is_nonnull: True if the field is still declared non-null.
      - line_number: 1-based line number of the field declaration (0 if not found).
      - line_text: the raw text of the matching line (empty if not found).

    Strategy: scan within the type block for *graphql_type*, then look for
    a field declaration line matching *field_name*.  We use a simple
    line-by-line scan rather than a full GraphQL parser — the schema files
    are well-structured and this is more robust to whitespace variation.
    """
    lines = schema_text.splitlines()

    # Find the type block for graphql_type.  We look for a line that opens
    # the type block: ``type TypeName {`` or ``type TypeName\n{``.
    type_start = -1
    type_block_re = re.compile(r"^\s*type\s+" + re.escape(graphql_type) + r"\s*\{?\s*$")
    for i, line in enumerate(lines):
        if type_block_re.match(line):
            type_start = i
            break

    if type_start == -1:
        # Type not found in this schema file — not a violation here.
        return False, 0, ""

    # Scan lines after the type block opening until the closing ``}``.
    depth = 0
    in_block = False
    field_re = re.compile(
        r"^\s*" + re.escape(field_name) + r"\s*(?:\([^)]*\))?\s*:\s*([\w\[\]!]+)"
    )
    for i in range(type_start, len(lines)):
        line = lines[i]
        # Track brace depth.
        depth += line.count("{") - line.count("}")
        if not in_block and "{" in line:
            in_block = True
        if in_block and depth <= 0:
            break  # exited the type block

        m = field_re.match(line)
        if m:
            type_expr = m.group(1).strip()
            is_nonnull = type_expr.endswith("!")
            return is_nonnull, i + 1, line.rstrip()

    return False, 0, ""


def find_graphql_drift(
    migration_file: Path,
    dropped_columns: list[tuple[str, str]],
    schema_files: list[Path],
) -> list[GraphQLDriftViolation]:
    """For each (table, column) in *dropped_columns* that is in KNOWN_MAPPINGS,
    check every mapped (GraphQLType, field) pair across *schema_files*.

    Returns a list of violations where the GraphQL field is still non-null.
    """
    violations: list[GraphQLDriftViolation] = []

    # Cache schema file contents.
    schema_texts: dict[Path, str] = {}
    for sf in schema_files:
        if sf.exists():
            try:
                schema_texts[sf] = sf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    for table, column in dropped_columns:
        mappings = KNOWN_MAPPINGS.get((table, column))
        if mappings is None:
            # Unmapped column — allowlist-driven check; silently skip.
            continue

        for graphql_type, field_name in mappings:
            # Check each schema file until we find the field.
            for sf, schema_text in schema_texts.items():
                is_nonnull, line_no, line_text = check_field_nonnull(
                    schema_text, sf, graphql_type, field_name
                )
                if line_no == 0:
                    # Field not declared in this file — try next.
                    continue
                if is_nonnull:
                    violations.append(
                        GraphQLDriftViolation(
                            migration_file=migration_file,
                            table=table,
                            column=column,
                            graphql_type=graphql_type,
                            field_name=field_name,
                            schema_file=sf,
                            line_number=line_no,
                            line_text=line_text,
                        )
                    )
                # Field found (either null or non-null) — no need to check further files.
                break

    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Detect GraphQL fields still declared non-null after the backing DB "
            "column had NOT NULL dropped in a migration."
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
        print(
            "check-graphql-nullability-drift: no migration files touched in diff — OK."
        )
        return 0

    # -------- Resolve schema files --------
    schema_files = [repo_root / sf for sf in _SCHEMA_FILES]

    # -------- Parse DROP NOT NULL statements and check GraphQL fields --------
    all_violations: list[GraphQLDriftViolation] = []
    any_dropped: bool = False

    for mf in migration_files:
        try:
            sql_text = mf.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"ERROR: cannot read {mf}: {exc}", file=sys.stderr)
            return 2

        dropped = parse_dropped_not_null(sql_text)
        if not dropped:
            continue

        any_dropped = True
        print(
            "check-graphql-nullability-drift: found DROP NOT NULL for columns in "
            + mf.name
            + ": "
            + ", ".join(f"{t}.{c}" for t, c in dropped)
        )

        violations = find_graphql_drift(mf, dropped, schema_files)
        all_violations.extend(violations)

    if not any_dropped:
        print(
            "check-graphql-nullability-drift: no DROP NOT NULL statements in changed "
            "migrations — OK."
        )
        return 0

    if not all_violations:
        print(
            "check-graphql-nullability-drift: all mapped GraphQL fields are already "
            "nullable — OK."
        )
        return 0

    print("", file=sys.stderr)
    print(
        "check-graphql-nullability-drift: FAIL — GraphQL fields still declared "
        "non-null after backing column had NOT NULL dropped:",
        file=sys.stderr,
    )
    for v in all_violations:
        rel_migration = (
            v.migration_file.relative_to(repo_root)
            if v.migration_file.is_relative_to(repo_root)
            else v.migration_file
        )
        rel_schema = (
            v.schema_file.relative_to(repo_root)
            if v.schema_file.is_relative_to(repo_root)
            else v.schema_file
        )
        print(
            f"  Migration: {rel_migration}\n"
            f"    Column:  {v.table}.{v.column}\n"
            f"    GraphQL: {v.graphql_type}.{v.field_name} is still non-null\n"
            f"    At:      {rel_schema}:{v.line_number}: {v.line_text.strip()}",
            file=sys.stderr,
        )

    print("", file=sys.stderr)
    print("Remediation:", file=sys.stderr)
    print(
        "  Flip the GraphQL field from non-null to nullable in the same PR as the\n"
        "  migration. For example, change:\n"
        "    issueNumber: Int!\n"
        "  to:\n"
        "    issueNumber: Int",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "See https://github.com/judgemind/judgemind/issues/3441 for background.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
