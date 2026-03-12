"""Shared schema validation helpers for SQL query testing.

These utilities parse the database schema DDL and cross-reference column
references in SQL query constants to catch typos (e.g. ``d.doc_type``
instead of ``d.document_type``) at test time.

Extracted from ``test_data_quality_check.py`` (PR #862) so multiple test
files can reuse the same validation logic.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType

# Path to the canonical schema DDL used for validation.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCHEMA_SQL_PATH = _REPO_ROOT / "packages" / "api" / "src" / "data-access" / "schema.sql"


def parse_schema_columns(schema_path: Path | None = None) -> dict[str, set[str]]:
    """Parse CREATE TABLE statements from schema.sql to extract table->columns.

    Returns a dict mapping table name (lowercase) to a set of column names.
    Handles both ``CREATE TABLE tablename`` and ``CREATE TABLE schema.tablename``.
    """
    if schema_path is None:
        schema_path = SCHEMA_SQL_PATH

    text = schema_path.read_text()
    table_columns: dict[str, set[str]] = {}

    # Match CREATE TABLE [schema.]tablename ( ... ) with content between parens.
    create_re = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:\w+\.)?(\w+)\s*\((.*?)\)\s*;",
        re.DOTALL | re.IGNORECASE,
    )

    for match in create_re.finditer(text):
        table_name = match.group(1).lower()
        body = match.group(2)
        columns: set[str] = set()

        for line in body.split("\n"):
            line = line.strip()
            # Skip empty lines, comments, constraints, and PRIMARY KEY lines
            if not line or line.startswith("--"):
                continue
            # Skip constraint lines
            upper = line.upper().lstrip()
            if any(
                upper.startswith(kw)
                for kw in ("CHECK", "UNIQUE", "PRIMARY", "FOREIGN", "CONSTRAINT")
            ):
                continue
            # A column definition starts with an identifier
            col_match = re.match(r"(\w+)\s+", line)
            if col_match:
                col_name = col_match.group(1).lower()
                if col_name.upper() not in (
                    "CHECK",
                    "UNIQUE",
                    "PRIMARY",
                    "FOREIGN",
                    "CONSTRAINT",
                    "CREATE",
                    "INDEX",
                    "COMMENT",
                    "ON",
                ):
                    columns.add(col_name)

        if columns:
            table_columns[table_name] = columns

    return table_columns


def extract_alias_to_table(query: str) -> dict[str, str]:
    """Extract table alias mappings from FROM and JOIN clauses in a SQL query.

    Handles patterns like:
    - ``FROM documents d``
    - ``JOIN courts ct ON ...``
    - ``LEFT JOIN rulings r ON ...``

    Returns a dict mapping alias (lowercase) to table name (lowercase).
    """
    alias_map: dict[str, str] = {}

    pattern = re.compile(
        r"(?:FROM|(?:LEFT|RIGHT|INNER|OUTER|CROSS|FULL)?\s*JOIN)\s+"
        r"(?:\w+\.)?(\w+)\s+(\w+)",
        re.IGNORECASE,
    )

    for match in pattern.finditer(query):
        table = match.group(1).lower()
        alias = match.group(2).lower()
        if alias.upper() not in ("ON", "WHERE", "AND", "OR", "SET", "AS", "USING"):
            alias_map[alias] = table

    return alias_map


def extract_column_references(query: str) -> list[tuple[str, str]]:
    """Extract alias.column references from a SQL query.

    Returns a list of (alias, column) tuples, both lowercase.
    Skips references inside string literals and format placeholders.
    """
    # Remove string literals (single-quoted) to avoid false matches
    cleaned = re.sub(r"'[^']*'", "''", query)
    # Remove format placeholders like {county_filter}
    cleaned = re.sub(r"\{[^}]+\}", "", cleaned)
    # Remove %s parameter placeholders
    cleaned = cleaned.replace("%s", "")

    refs: list[tuple[str, str]] = []
    for match in re.finditer(r"\b(\w+)\.(\w+)\b", cleaned):
        alias = match.group(1).lower()
        column = match.group(2).lower()
        # Skip non-alias patterns (e.g., schema.table in CREATE/FROM)
        if alias in ("staging", "json_build_object"):
            continue
        refs.append((alias, column))

    return refs


def get_query_constants(module: ModuleType) -> dict[str, str]:
    """Dynamically discover all SQL query constants from a module.

    Returns a dict mapping constant name to query string.
    Identifies query constants by the naming convention: uppercase name ending
    in ``_QUERY``.
    """
    queries: dict[str, str] = {}
    for attr_name in dir(module):
        if attr_name.endswith("_QUERY") and attr_name.isupper():
            value = getattr(module, attr_name)
            if isinstance(value, str):
                queries[attr_name] = value
    return queries


def validate_queries_against_schema(
    queries: dict[str, str],
    schema: dict[str, set[str]] | None = None,
) -> list[str]:
    """Validate that all alias.column references in SQL queries exist in the schema.

    Parameters
    ----------
    queries : dict[str, str]
        Mapping of query name to SQL string.
    schema : dict[str, set[str]] | None
        Pre-parsed schema. If None, parses ``SCHEMA_SQL_PATH``.

    Returns
    -------
    list[str]
        Error messages for each invalid column reference. Empty if all valid.
    """
    if schema is None:
        schema = parse_schema_columns()

    errors: list[str] = []
    for query_name, query_sql in queries.items():
        alias_map = extract_alias_to_table(query_sql)
        col_refs = extract_column_references(query_sql)

        for alias, column in col_refs:
            if alias not in alias_map:
                # Alias might be a CTE or subquery — skip
                continue
            table = alias_map[alias]
            if table not in schema:
                # Table might be a CTE or view — skip
                continue
            if column not in schema[table]:
                errors.append(
                    f"{query_name}: {alias}.{column} -> "
                    f"column '{column}' does not exist in table '{table}'. "
                    f"Available columns: {sorted(schema[table])}"
                )

    return errors
