"""Runtime schema validation for backfill scripts.

Provides utilities to validate that SQL queries reference real tables and
columns before executing any mutations.  Two validation modes are supported:

1. **Live DB validation** — queries ``information_schema.columns`` to verify
   table/column references against the actual database.
2. **Static DDL validation** — parses ``schema.sql`` to verify references
   without a database connection.

Typical usage in a backfill script::

    from validation.schema import validate_script_queries

    # Before running mutations, validate all *_QUERY constants
    errors = validate_script_queries(conn, __import__(__name__))
    if errors:
        for e in errors:
            logger.error("Schema validation: %s", e)
        sys.exit(1)

Or with the ``--validate-schema`` CLI flag pattern::

    if args.validate_schema:
        import the_script_module
        errors = validate_script_queries(conn, the_script_module)
        if errors:
            ...
            sys.exit(1)
        logger.info("Schema validation passed")
        if args.validate_schema_only:
            sys.exit(0)
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)


def fetch_live_schema(conn: psycopg.Connection) -> dict[str, set[str]]:
    """Query ``information_schema.columns`` to get table->columns mapping.

    Returns a dict mapping table name (lowercase) to a set of column names
    (lowercase).  Only includes tables in the ``public`` schema.
    """
    query = """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """
    table_columns: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(query)
        for table_name, column_name in cur.fetchall():
            table_lower = table_name.lower()
            if table_lower not in table_columns:
                table_columns[table_lower] = set()
            table_columns[table_lower].add(column_name.lower())
    return table_columns


def parse_schema_ddl(schema_path: Path) -> dict[str, set[str]]:
    """Parse CREATE TABLE statements from a schema DDL file.

    Returns a dict mapping table name (lowercase) to a set of column names.
    This is the static equivalent of :func:`fetch_live_schema` for use when
    no database connection is available.
    """
    text = schema_path.read_text()
    table_columns: dict[str, set[str]] = {}

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
            if not line or line.startswith("--"):
                continue
            upper = line.upper().lstrip()
            if any(
                upper.startswith(kw)
                for kw in ("CHECK", "UNIQUE", "PRIMARY", "FOREIGN", "CONSTRAINT")
            ):
                continue
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


def validate_columns(
    schema: dict[str, set[str]],
    table: str,
    columns: list[str],
) -> list[str]:
    """Validate that the given columns exist in the specified table.

    Parameters
    ----------
    schema : dict[str, set[str]]
        Schema mapping from :func:`fetch_live_schema` or :func:`parse_schema_ddl`.
    table : str
        Table name to validate against.
    columns : list[str]
        Column names to check.

    Returns
    -------
    list[str]
        Error messages for each invalid column. Empty if all valid.
    """
    table_lower = table.lower()
    if table_lower not in schema:
        return [f"Table '{table}' does not exist in schema"]

    valid_columns = schema[table_lower]
    errors: list[str] = []
    for col in columns:
        if col.lower() not in valid_columns:
            errors.append(
                f"Column '{col}' does not exist in table '{table}'. "
                f"Available columns: {sorted(valid_columns)}"
            )
    return errors


def extract_alias_to_table(query: str) -> dict[str, str]:
    """Extract table alias mappings from FROM, JOIN, and UPDATE clauses.

    Handles patterns like ``FROM documents d``, ``JOIN courts ct ON ...``,
    ``UPDATE rulings r SET ...``.
    Returns a dict mapping alias (lowercase) to table name (lowercase).
    """
    alias_map: dict[str, str] = {}

    # FROM / JOIN aliases
    from_join_pattern = re.compile(
        r"(?:FROM|(?:LEFT|RIGHT|INNER|OUTER|CROSS|FULL)?\s*JOIN)\s+"
        r"(?:\w+\.)?(\w+)\s+(\w+)",
        re.IGNORECASE,
    )

    for match in from_join_pattern.finditer(query):
        table = match.group(1).lower()
        alias = match.group(2).lower()
        if alias.upper() not in ("ON", "WHERE", "AND", "OR", "SET", "AS", "USING"):
            alias_map[alias] = table

    # UPDATE table alias SET ... — alias is between table name and SET keyword
    update_pattern = re.compile(
        r"UPDATE\s+(?:\w+\.)?(\w+)\s+(\w+)\s+SET\b",
        re.IGNORECASE,
    )
    for match in update_pattern.finditer(query):
        table = match.group(1).lower()
        alias = match.group(2).lower()
        alias_map[alias] = table

    return alias_map


def extract_column_references(query: str) -> list[tuple[str, str]]:
    """Extract alias.column references from a SQL query.

    Returns a list of (alias, column) tuples, both lowercase.
    Skips references inside string literals and format placeholders.
    """
    cleaned = re.sub(r"'[^']*'", "''", query)
    cleaned = re.sub(r"\{[^}]+\}", "", cleaned)
    cleaned = cleaned.replace("%s", "")

    refs: list[tuple[str, str]] = []
    for match in re.finditer(r"\b(\w+)\.(\w+)\b", cleaned):
        alias = match.group(1).lower()
        column = match.group(2).lower()
        if alias in ("staging", "json_build_object"):
            continue
        refs.append((alias, column))

    return refs


def get_query_constants(module: ModuleType) -> dict[str, str]:
    """Discover all SQL query constants from a module.

    Returns a dict mapping constant name to query string.
    Identifies query constants by uppercase names ending in ``_QUERY``.
    """
    queries: dict[str, str] = {}
    for attr_name in dir(module):
        if attr_name.endswith("_QUERY") and attr_name.isupper():
            value = getattr(module, attr_name)
            if isinstance(value, str):
                queries[attr_name] = value
    return queries


def validate_queries(
    queries: dict[str, str],
    schema: dict[str, set[str]],
) -> list[str]:
    """Validate that all alias.column references in SQL queries exist in the schema.

    Parameters
    ----------
    queries : dict[str, str]
        Mapping of query name to SQL string.
    schema : dict[str, set[str]]
        Schema mapping from :func:`fetch_live_schema` or :func:`parse_schema_ddl`.

    Returns
    -------
    list[str]
        Error messages for each invalid column reference. Empty if all valid.
    """
    errors: list[str] = []
    for query_name, query_sql in queries.items():
        alias_map = extract_alias_to_table(query_sql)
        col_refs = extract_column_references(query_sql)

        for alias, column in col_refs:
            if alias not in alias_map:
                continue
            table = alias_map[alias]
            if table not in schema:
                continue
            if column not in schema[table]:
                errors.append(
                    f"{query_name}: {alias}.{column} -> "
                    f"column '{column}' does not exist in table '{table}'. "
                    f"Available columns: {sorted(schema[table])}"
                )

    return errors


def validate_script_queries(
    conn: psycopg.Connection,
    module: ModuleType,
) -> list[str]:
    """Validate all ``*_QUERY`` constants in a script module against the live DB.

    This is the primary entry point for backfill scripts.  It queries
    ``information_schema.columns`` and checks every alias.column reference
    in the module's SQL constants.

    Parameters
    ----------
    conn : psycopg.Connection
        Active database connection for schema introspection.
    module : ModuleType
        The script module containing ``*_QUERY`` constants.

    Returns
    -------
    list[str]
        Error messages for each invalid reference. Empty if all valid.

    Example
    -------
    ::

        import sys
        from validation.schema import validate_script_queries

        # Inside main(), after connecting:
        errors = validate_script_queries(conn, sys.modules[__name__])
        if errors:
            for e in errors:
                logger.error("Schema validation: %s", e)
            sys.exit(1)
    """
    schema = fetch_live_schema(conn)
    queries = get_query_constants(module)
    if not queries:
        logger.warning("No *_QUERY constants found in module %s", module.__name__)
        return []
    return validate_queries(queries, schema)


def add_validate_schema_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--validate-schema`` flag to an argparse parser.

    This is a convenience helper for backfill scripts to add the standard
    schema validation flag.  The flag causes the script to validate all
    SQL column references before executing any mutations.

    Usage::

        parser = argparse.ArgumentParser(...)
        add_validate_schema_flag(parser)
        args = parser.parse_args()

        # After connecting to DB:
        if args.validate_schema:
            errors = validate_script_queries(conn, sys.modules[__name__])
            if errors:
                for e in errors:
                    logger.error(e)
                sys.exit(1)
            logger.info("Schema validation passed — all column references are valid")
    """
    parser.add_argument(
        "--validate-schema",
        action="store_true",
        help=(
            "Validate all SQL column references against the live database schema "
            "before executing any mutations. Exits with an error if any invalid "
            "references are found."
        ),
    )
