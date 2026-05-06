#!/usr/bin/env python3
"""check-sql-columns.py -- Validate SQL column references in Python scripts.

Scans Python files in scripts/ and packages/ for SQL string literals,
extracts both qualified (``alias.column``) and unqualified (bare
``column`` in single-table queries) references, and validates each
column exists in the schema definition (schema.sql).

Catches two related bug classes:

1. Qualified-reference drift (#1929 motivating case): writing
   ``documents.updated_at`` when the ``documents`` table has no
   ``updated_at`` column.
2. Unqualified-reference drift (#4271 motivating case): writing
   ``WHERE capture_timestamp >= %s`` against ``derived.documents``
   when the actual column is ``captured_at``. Mocked unit tests do
   not surface this -- the test doubles only see the SQL prefix and
   parameters, not the column names. The first signal is the live
   ECS run failing with ``psycopg.errors.UndefinedColumn``.

For unqualified references, the check is conservative: it only fires
on queries with a single base table (no JOINs, no CTEs, no
subqueries in FROM). Multi-table queries introduce ambiguity about
which table an unqualified column belongs to, so the safe play is
to skip them entirely and require explicit qualification.

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

    Adjacent string literals are merged following Python's implicit
    string concatenation rules so that

        cur.execute(
            "SELECT COUNT(*) FROM derived.documents "
            "WHERE scraper_id = %s AND captured_at >= %s"
        )

    is analyzed as a single SQL string rather than two unrelated tokens.
    This is essential for catching the #4264 bug class -- mocked unit
    tests only saw the SELECT prefix, so the column-name typo in the
    second literal was invisible to them.
    """
    lines = content.splitlines()
    raw_tokens: list[tuple[int, int, str]] = []
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
                raw_tokens.append((srow, erow, val))
            elif tok_type in (
                tokenize.NEWLINE,
                tokenize.NL,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.COMMENT,
                tokenize.ENCODING,
            ):
                # Whitespace / newlines / comments do not break implicit
                # string concatenation. Skip them when deciding whether
                # adjacent STRING tokens should be merged.
                continue
            else:
                # A non-string, non-whitespace token closes any open run
                # of adjacent string literals.
                raw_tokens.append((-1, -1, ""))  # sentinel break
    except tokenize.TokenError:
        pass

    # Merge adjacent string-literal runs. The sentinel `(-1, -1, "")` marks
    # a non-string token that broke concatenation.
    results: list[tuple[int, str]] = []
    run_start: int | None = None
    run_text: list[str] = []
    for srow, erow, val in raw_tokens:
        if srow == -1:
            if run_start is not None:
                results.append((run_start, "".join(run_text)))
                run_start = None
                run_text = []
            continue
        if run_start is None:
            run_start = srow
            run_text = [val]
        else:
            run_text.append(val)
    if run_start is not None:
        results.append((run_start, "".join(run_text)))
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
# Unqualified column reference extraction (#4271)
# ---------------------------------------------------------------------------
#
# The qualified-reference path above only catches ``alias.column`` /
# ``table.column`` typos. The bug class motivating #4271 is the
# *unqualified* reference: ``WHERE capture_timestamp >= %s`` against
# ``derived.documents`` when the actual column is ``captured_at``. To
# avoid false positives on multi-table queries (where bare identifiers
# could refer to either table), the unqualified path only runs when the
# SQL string has exactly one base table -- i.e. a single FROM / UPDATE /
# INSERT INTO / DELETE FROM, no JOINs, no CTEs, no subqueries in FROM,
# no comma-joined tables.

# Pattern: SELECT ... FROM <table> [alias] [WHERE/ORDER/...]  (single-table)
# We capture only the first FROM target and stop at any continuation that
# would indicate a join or comma.
_SELECT_FROM_RE = re.compile(
    r"\bFROM\s+([a-z_]+(?:\.[a-z_]+)?)",
    re.IGNORECASE,
)

# Tokens after FROM that signal "this is no longer a single-table query"
_MULTI_TABLE_SIGNALS_RE = re.compile(
    r"\b(JOIN|,\s*[a-z_])",
    re.IGNORECASE,
)


def _strip_string_literals(sql: str) -> str:
    """Return *sql* with single- and double-quoted string literals,
    DB-API parameter placeholders, and Python format placeholders
    replaced by spaces of the same length.

    Used so identifier-extraction regexes don't match:
      - user-supplied data that happens to spell a column name,
      - the ``s`` from ``%s`` / the ``name`` from ``%(name)s``,
      - the contents of Python ``str.format`` placeholders like
        ``{county_filter}`` that downstream code substitutes into
        the SQL string at runtime.

    Newlines inside the replaced spans are preserved so line-offset
    accounting in ``scan_file`` stays correct.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                c = sql[i]
                if c == quote:
                    # SQL doubles a quote to escape it -- skip past the pair.
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append("  ")
                        i += 2
                        continue
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue
        # DB-API parameter placeholders: %s, %d, %(name)s, %(name)d
        if ch == "%" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt in ("s", "d"):
                out.append("  ")
                i += 2
                continue
            if nxt == "(":
                # %(name)s -- find the closing `)s` / `)d` and blank it all.
                j = i + 2
                while j < n and sql[j] != ")":
                    j += 1
                # j is at `)` or end; check for trailing `s` or `d`.
                if j < n and j + 1 < n and sql[j + 1] in ("s", "d"):
                    span = (j + 2) - i
                    # Replace span chars with spaces, preserving newlines.
                    for k in range(i, i + span):
                        out.append("\n" if sql[k] == "\n" else " ")
                    i += span
                    continue
        # Python str.format placeholders: {name}, {name:fmt}, {0}, etc.
        # The contents are substituted at runtime; from the static check's
        # point of view they are opaque.
        if ch == "{" and i + 1 < n and sql[i + 1] != "{":
            j = i + 1
            depth = 1
            while j < n and depth > 0:
                if sql[j] == "{":
                    depth += 1
                elif sql[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0:
                span = (j + 1) - i
                for k in range(i, i + span):
                    out.append("\n" if sql[k] == "\n" else " ")
                i += span
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _single_base_table(sql: str) -> str | None:
    """Return the single base table for *sql*, or None if not analyzable.

    Returns None when:
      - The SQL contains a JOIN.
      - The SQL has comma-joined tables (legacy join syntax).
      - The SQL contains a CTE (``WITH ... AS``).
      - FROM is followed by a subquery (``FROM (SELECT ...)``).
      - There is no recognizable base table at all.
      - There are multiple statements (the regexes wouldn't reconcile
        them anyway).

    For UPDATE / INSERT INTO / DELETE FROM, the table is unambiguous.

    Schema prefixes (``derived.``, ``staging.``, ``public.``,
    ``telemetry.``) are stripped so the result keys against the schema
    map produced by ``build_table_columns``.
    """
    cleaned = _strip_string_literals(sql)
    lower = cleaned.lower()

    # Bail on CTEs -- they introduce alias bindings the simple regex can't track.
    if re.search(r"\bwith\s+[a-z_][a-z0-9_]*\s+as\s*\(", lower):
        return None

    # Detect FROM ( -- subquery in FROM
    if re.search(r"\bfrom\s*\(", lower):
        return None

    # Determine statement kind by leading keyword (after stripping whitespace
    # and comments). DELETE FROM also contains the substring "FROM rulings"
    # which would otherwise also match _SELECT_FROM_RE -- check the leading
    # keyword first so we route exclusively to the right path.
    leading = re.match(
        r"\s*(?:--[^\n]*\n\s*)*(SELECT|UPDATE|INSERT|DELETE|WITH)\b",
        cleaned,
        re.IGNORECASE,
    )
    if leading is None:
        return None
    kind = leading.group(1).lower()

    if kind == "select":
        select_from = _SELECT_FROM_RE.search(cleaned)
        if select_from is None:
            return None
        # Reject multi-table SELECTs.
        if _MULTI_TABLE_SIGNALS_RE.search(cleaned[select_from.end():]):
            return None
        table = select_from.group(1)
    elif kind == "update":
        update_match = _UPDATE_TABLE_RE.search(cleaned)
        if update_match is None:
            return None
        # Reject UPDATE...FROM (PG syntax for cross-table updates).
        # An UPDATE that references a second table introduces ambiguity.
        if re.search(r"\bUPDATE\b.*\bFROM\b", cleaned, re.IGNORECASE | re.DOTALL):
            return None
        table = update_match.group(1)
    elif kind == "insert":
        insert_match = _INSERT_TABLE_RE.search(cleaned)
        if insert_match is None:
            return None
        # Reject INSERT...SELECT (the SELECT side could pull from anywhere).
        if re.search(
            r"\bINSERT\b.*\bSELECT\b.*\bFROM\b",
            cleaned,
            re.IGNORECASE | re.DOTALL,
        ):
            return None
        table = insert_match.group(1)
    elif kind == "delete":
        delete_match = _DELETE_TABLE_RE.search(cleaned)
        if delete_match is None:
            return None
        # Reject DELETE...USING (cross-table delete).
        if re.search(r"\bDELETE\b.*\bUSING\b", cleaned, re.IGNORECASE | re.DOTALL):
            return None
        table = delete_match.group(1)
    else:  # WITH -- already handled by the CTE bail-out above; defensive return.
        return None

    table = table.lower()
    for prefix in ("public.", "derived.", "telemetry."):
        if table.startswith(prefix):
            table = table[len(prefix):]
            break
    return table


# Reserved tokens that look like identifiers but are SQL syntax / functions.
# This is intentionally a superset of _ALIAS_KEYWORD_EXCLUDES + common
# functions; it gates the bare-identifier scan in WHERE/SET/SELECT.
_UNQUALIFIED_SKIP = frozenset(
    {
        # Keywords
        "select", "from", "where", "and", "or", "not", "in", "is", "as",
        "on", "set", "values", "into", "insert", "update", "delete",
        "create", "alter", "drop", "table", "if", "exists", "null",
        "true", "false", "default", "primary", "key", "unique",
        "constraint", "foreign", "references", "check", "with", "recursive",
        "limit", "offset", "order", "by", "group", "having", "union",
        "except", "intersect", "returning", "using", "natural", "between",
        "like", "ilike", "case", "when", "then", "else", "end", "do",
        "nothing", "cascade", "restrict", "asc", "desc", "nulls", "first",
        "last", "lateral", "join", "left", "right", "inner", "outer",
        "cross", "full", "any", "all", "some", "distinct", "filter",
        "over", "partition", "window", "rows", "range", "groups",
        "current", "row", "unbounded", "preceding", "following",
        "for", "of", "share", "no", "key", "skip", "locked",
        # Casts / types (not exhaustive, but common ones)
        "uuid", "text", "integer", "int", "bigint", "smallint", "boolean",
        "bool", "numeric", "decimal", "real", "double", "precision",
        "date", "time", "timestamp", "timestamptz", "interval", "jsonb",
        "json", "bytea", "char", "varchar",
        # Common functions
        "count", "sum", "avg", "min", "max", "now", "coalesce", "lower",
        "upper", "trim", "length", "row_number", "rank", "dense_rank",
        "lag", "lead", "first_value", "last_value", "array_agg",
        "string_agg", "jsonb_build_object", "jsonb_agg", "json_agg",
        "extract", "date_trunc", "to_char", "to_date", "to_timestamp",
        "regexp_replace", "regexp_match", "regexp_matches", "substring",
        "position", "replace", "concat", "format", "split_part", "btrim",
        "ltrim", "rtrim", "chr", "ascii", "encode", "decode", "md5",
        "sha256", "gen_random_uuid", "gen_random_bytes", "greatest",
        "least", "abs", "ceil", "floor", "round", "trunc", "random",
        "generate_series", "unnest", "bool_and", "bool_or", "every",
        "exists",
        # Boolean / SQL literals
        "current_timestamp", "current_date", "current_time", "localtime",
        "localtimestamp", "current_user", "session_user",
        # Special
        "excluded", "new", "old",
    }
)


# Bare identifier: a word boundary, then a letter/underscore, then word chars.
# We match these only inside the clauses we care about (extracted below).
_BARE_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b", re.IGNORECASE)


# Clause boundaries: when scanning for unqualified column refs, we look
# inside WHERE/SET/SELECT/ORDER BY/GROUP BY/HAVING/INSERT-column-list/
# RETURNING. The clause start regexes capture the position to start from.
# We extract everything after the clause keyword up to the next clause
# keyword (or end of string).
_CLAUSE_BOUNDARY_RE = re.compile(
    r"\b("
    r"WHERE|SET|GROUP\s+BY|ORDER\s+BY|HAVING|RETURNING|"
    r"VALUES|LIMIT|OFFSET|UNION|EXCEPT|INTERSECT|"
    r"JOIN|FROM|ON|WHEN|END"
    r")\b",
    re.IGNORECASE,
)


def _select_list_text(sql: str) -> str:
    """Return the text of the top-level SELECT list (between SELECT and FROM)."""
    m = re.search(r"\bSELECT\b", sql, re.IGNORECASE)
    if m is None:
        return ""
    start = m.end()
    # Find the matching FROM at depth 0.
    depth = 0
    i = start
    while i < len(sql):
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            # Match \bFROM\b at this position
            if (
                sql[i : i + 4].lower() == "from"
                and (i == 0 or not sql[i - 1].isalnum() and sql[i - 1] != "_")
                and (i + 4 == len(sql) or not sql[i + 4].isalnum() and sql[i + 4] != "_")
            ):
                return sql[start:i]
        i += 1
    return sql[start:]


def _column_list_text(sql: str) -> str:
    """Return the text of the INSERT INTO column list, e.g. ``(id, state)``.

    Empty when the INSERT has no explicit column list.
    """
    m = re.search(
        r"\bINSERT\s+INTO\s+[a-z_]+(?:\.[a-z_]+)?\s*\(([^)]*)\)",
        sql,
        re.IGNORECASE,
    )
    return m.group(1) if m else ""


def _clause_segments(sql: str) -> list[str]:
    """Return text segments between clause keywords for unqualified scan.

    Includes WHERE / SET / ORDER BY / GROUP BY / HAVING / RETURNING bodies.
    Excludes FROM / JOIN / VALUES bodies (those don't contain column refs
    in the form we care about).
    """
    segments: list[str] = []
    keywords = re.compile(
        r"\b(WHERE|SET|GROUP\s+BY|ORDER\s+BY|HAVING|RETURNING)\b",
        re.IGNORECASE,
    )
    matches = list(keywords.finditer(sql))
    for i, m in enumerate(matches):
        start = m.end()
        # End at the next clause boundary (any boundary keyword, not just
        # the column-bearing ones) or end-of-string.
        next_boundary = _CLAUSE_BOUNDARY_RE.search(sql, start)
        end = next_boundary.start() if next_boundary else len(sql)
        segments.append(sql[start:end])
    return segments


def _extract_select_aliases(sql: str) -> set[str]:
    """Return the set of column alias names defined in the top-level SELECT.

    Handles both ``expr AS alias`` and ``expr alias`` syntax, but is
    conservative about the latter to avoid over-matching: we only recognize
    a trailing word as an alias when it's preceded by ``)`` (function call
    end) or another simple expression closing token.
    """
    aliases: set[str] = set()
    select_text = _select_list_text(sql)
    # Match `AS alias_name` -- this is the unambiguous form.
    for m in re.finditer(r"\bAS\s+([a-z_][a-z0-9_]*)", select_text, re.IGNORECASE):
        aliases.add(m.group(1).lower())
    return aliases


def _extract_unqualified_column_references(sql: str) -> list[str]:
    """Extract bare (unqualified) column identifiers from *sql*.

    Returns a list of lowercase identifiers referenced in WHERE / SET /
    SELECT-list / ORDER BY / GROUP BY / HAVING / RETURNING / INSERT
    column-list clauses, excluding:
      - SQL keywords / type names / common functions (``_UNQUALIFIED_SKIP``)
      - Identifiers that appear as ``alias.column`` (qualified -- handled
        by the existing path)
      - Identifiers inside string literals (including Python format
        placeholders like ``{county_filter}`` -- those are stripped
        during preprocessing)
      - Column aliases defined via ``AS alias`` in the SELECT list -- those
        are valid references in ORDER BY / GROUP BY / HAVING and are not
        column names on the base table
      - Function calls (identifier directly followed by ``(``)

    The returned list is deduplicated but preserves order of first
    appearance.
    """
    cleaned = _strip_string_literals(sql)

    # Collect candidate text regions:
    #   1. Top-level SELECT list.
    #   2. INSERT INTO column list.
    #   3. WHERE / SET / ORDER BY / GROUP BY / HAVING / RETURNING bodies.
    regions: list[str] = []
    if re.search(r"\bSELECT\b", cleaned, re.IGNORECASE):
        regions.append(_select_list_text(cleaned))
    col_list = _column_list_text(cleaned)
    if col_list:
        regions.append(col_list)
    regions.extend(_clause_segments(cleaned))

    # Track positions of qualified refs in the original cleaned SQL so we
    # can skip them in the bare-ident pass. Build a set of "the column
    # part of qualified.column" by string match in each region.
    qualified_cols: set[str] = set()
    for m in _QUALIFIED_COL_RE.finditer(cleaned):
        qualified_cols.add(m.group(2).lower())

    # Track column aliases defined in the top-level SELECT (e.g.
    # `COUNT(*) AS null_motion`). These are valid references later in
    # the same query (ORDER BY, GROUP BY, HAVING) but they are NOT base
    # table columns.
    select_aliases = _extract_select_aliases(cleaned)

    seen: set[str] = set()
    ordered: list[str] = []
    for region in regions:
        # Walk identifiers in the region, skipping function calls and
        # qualified refs.
        for m in _BARE_IDENT_RE.finditer(region):
            ident = m.group(1).lower()
            if ident in _UNQUALIFIED_SKIP:
                continue
            if ident in select_aliases:
                continue
            # Skip if this identifier is the qualifier part of a
            # qualified ref: i.e. immediately followed by `.`.
            end = m.end()
            if end < len(region) and region[end] == ".":
                continue
            # Skip function calls (identifier followed by `(`).
            if end < len(region) and region[end] == "(":
                continue
            # Skip if preceded by `.` (means it's the column of a qualified
            # ref -- already handled by the qualified path).
            start = m.start()
            if start > 0 and region[start - 1] == ".":
                continue
            # Skip parameter placeholders -- %s is not an identifier here,
            # but psycopg2's `%(name)s` style would produce `name` as a
            # candidate. The literal-stripper already removed quotes; the
            # `%(...)s` syntax leaves identifiers exposed. Detect by checking
            # whether the identifier sits inside parens with %()s context.
            if start >= 2 and region[start - 2 : start] == "%(":
                continue
            # Skip numeric-only forms (e.g. `123` would not match the regex
            # but be safe).
            if ident.isdigit():
                continue
            # Skip if also appears as qualifier elsewhere in the SQL
            # (it's an alias, not a column).
            # NOTE: this is an over-conservative filter -- we'd rather
            # under-flag than over-flag.
            if ident in qualified_cols and ident not in ordered:
                # Could legitimately be a column; only skip if it appears
                # exclusively as a qualified ref (handled elsewhere).
                pass
            if ident in seen:
                continue
            seen.add(ident)
            ordered.append(ident)
    return ordered


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

        # ----- Unqualified column reference pass (#4271) -----
        # Only run on single-table queries to keep false positives at zero.
        base_table = _single_base_table(string_val)
        if base_table is not None:
            cols = table_columns.get(base_table)
            if cols is not None:
                # Build the alias->table map so we can skip identifiers
                # that are actually aliases (would already get flagged via
                # qualified path if the alias is wrong).
                alias_names = set(aliases.keys())
                for ident in _extract_unqualified_column_references(string_val):
                    if ident == base_table:
                        continue
                    if ident in alias_names:
                        # It's an alias for the base table itself -- skip.
                        continue
                    if ident in cols:
                        if verbose:
                            print(
                                f"  {filepath.name}:{start_line}: "
                                f"{ident} -> {base_table}.{ident} -- OK"
                            )
                        continue
                    dedup_key = (base_table, ident, "")
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    # Approximate line number from first occurrence in the
                    # SQL string.
                    cleaned = _strip_string_literals(string_val)
                    m = re.search(rf"\b{re.escape(ident)}\b", cleaned, re.IGNORECASE)
                    line_offset = (
                        cleaned[: m.start()].count("\n") if m else 0
                    )
                    errors.append(
                        f"  {filepath.name}:{start_line + line_offset}: "
                        f"column '{ident}' does not exist on table "
                        f"'{base_table}' (referenced unqualified). "
                        f"Available columns: {', '.join(sorted(cols))}"
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
