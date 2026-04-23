"""Tests for check-sql-columns.py -- SQL column reference validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Import the module despite its dash-containing filename.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "check-sql-columns.py"
_spec = importlib.util.spec_from_file_location("check_sql_columns", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
check_sql_columns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_sql_columns)

# Pull out the functions we want to test.
_extract_table_columns_from_sql = check_sql_columns._extract_table_columns_from_sql
_extract_string_tokens = check_sql_columns._extract_string_tokens
_is_sql_string = check_sql_columns._is_sql_string
_resolve_aliases = check_sql_columns._resolve_aliases
_extract_column_references = check_sql_columns._extract_column_references
_validate_column_ref = check_sql_columns._validate_column_ref
build_table_columns = check_sql_columns.build_table_columns
scan_file = check_sql_columns.scan_file
TABLE_COLUMNS = check_sql_columns.TABLE_COLUMNS


# ---------------------------------------------------------------------------
# Tests: Schema parsing
# ---------------------------------------------------------------------------


class TestExtractTableColumnsFromSql:
    """Tests for _extract_table_columns_from_sql."""

    def test_simple_table(self) -> None:
        sql = (
            "CREATE TABLE courts (\n"
            "    id UUID PRIMARY KEY,\n"
            "    state CHAR(2) NOT NULL,\n"
            "    county TEXT NOT NULL\n"
            ");"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "courts" in result
        assert {"id", "state", "county"} == result["courts"]

    def test_custom_enum_type(self) -> None:
        """Custom enum types like ruling_outcome should be recognized."""
        sql = (
            "CREATE TYPE ruling_outcome AS ENUM ('granted', 'denied');\n"
            "CREATE TABLE rulings (\n"
            "    id UUID PRIMARY KEY,\n"
            "    outcome ruling_outcome,\n"
            "    motion_type TEXT\n"
            ");"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "rulings" in result
        assert "outcome" in result["rulings"]
        assert "motion_type" in result["rulings"]

    def test_if_not_exists(self) -> None:
        """IF NOT EXISTS should not be treated as a column name."""
        sql = (
            "CREATE TABLE IF NOT EXISTS foo (\n"
            "    id UUID PRIMARY KEY,\n"
            "    name TEXT\n"
            ");"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "foo" in result
        assert "if" not in result["foo"]
        assert "id" in result["foo"]
        assert "name" in result["foo"]

    def test_schema_prefixed_table(self) -> None:
        sql = (
            "CREATE TABLE staging.captures (\n"
            "    id UUID PRIMARY KEY,\n"
            "    court_id UUID NOT NULL\n"
            ");"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "staging.captures" in result
        assert "id" in result["staging.captures"]
        assert "court_id" in result["staging.captures"]

    def test_alter_table_add_column(self) -> None:
        sql = (
            "ALTER TABLE users ADD COLUMN google_id TEXT;\n"
            "ALTER TABLE users ADD email_verified BOOLEAN;\n"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "users" in result
        assert "google_id" in result["users"]
        assert "email_verified" in result["users"]

    def test_down_migration_ignored(self) -> None:
        sql = (
            "-- Up Migration\n"
            "ALTER TABLE foo ADD COLUMN bar TEXT;\n"
            "\n"
            "-- Down Migration\n"
            "ALTER TABLE foo ADD COLUMN baz TEXT;\n"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "foo" in result
        assert "bar" in result["foo"]
        assert "baz" not in result["foo"]

    def test_constraints_not_columns(self) -> None:
        """CONSTRAINT, PRIMARY KEY, UNIQUE etc should not be columns."""
        sql = (
            "CREATE TABLE case_parties (\n"
            "    id UUID PRIMARY KEY,\n"
            "    case_id UUID NOT NULL,\n"
            "    party_id UUID NOT NULL,\n"
            "    role TEXT NOT NULL,\n"
            "    UNIQUE (case_id, party_id, role)\n"
            ");"
        )
        result = _extract_table_columns_from_sql(sql)
        cols = result["case_parties"]
        assert "id" in cols
        assert "case_id" in cols
        assert "party_id" in cols
        assert "role" in cols
        assert "unique" not in cols
        assert "primary" not in cols
        assert "constraint" not in cols

    def test_timestamptz_column(self) -> None:
        sql = (
            "CREATE TABLE events (\n"
            "    id UUID PRIMARY KEY,\n"
            "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
            ");"
        )
        result = _extract_table_columns_from_sql(sql)
        assert "created_at" in result["events"]


class TestBuildTableColumns:
    """Tests for build_table_columns with synthetic schema files."""

    def test_from_schema_and_migrations(self, tmp_path: Path) -> None:
        api_dir = tmp_path / "packages" / "api" / "src" / "data-access"
        api_dir.mkdir(parents=True)
        mig_dir = tmp_path / "packages" / "api" / "migrations"
        mig_dir.mkdir(parents=True)

        schema = (
            "CREATE TABLE courts (\n"
            "    id UUID PRIMARY KEY,\n"
            "    state CHAR(2) NOT NULL,\n"
            "    county TEXT NOT NULL\n"
            ");\n"
        )
        (api_dir / "schema.sql").write_text(schema)

        migration = (
            "-- Up Migration\n"
            "ALTER TABLE courts ADD COLUMN timezone TEXT;\n"
            "\n"
            "-- Down Migration\n"
            "ALTER TABLE courts DROP COLUMN timezone;\n"
        )
        (mig_dir / "2_add-timezone.sql").write_text(migration)

        result = build_table_columns(tmp_path)
        assert "courts" in result
        assert "timezone" in result["courts"]
        assert "state" in result["courts"]

    def test_missing_schema_file(self, tmp_path: Path) -> None:
        result = build_table_columns(tmp_path)
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: SQL string detection
# ---------------------------------------------------------------------------


class TestIsSqlString:
    """Tests for _is_sql_string."""

    def test_select_from(self) -> None:
        assert _is_sql_string("SELECT id FROM rulings")

    def test_update_set(self) -> None:
        assert _is_sql_string("UPDATE rulings SET outcome = 'granted'")

    def test_insert_into(self) -> None:
        assert _is_sql_string("INSERT INTO courts (id) VALUES (1)")

    def test_delete_from(self) -> None:
        assert _is_sql_string("DELETE FROM rulings WHERE id = 1")

    def test_url_not_sql(self) -> None:
        """URLs containing SQL-like words should NOT be detected as SQL."""
        assert not _is_sql_string("https://www.courts.ca.gov/tentative-rulings")

    def test_plain_text_not_sql(self) -> None:
        assert not _is_sql_string("This is a regular string")

    def test_single_keyword_not_sql(self) -> None:
        """A single SQL keyword alone is not a SQL string."""
        assert not _is_sql_string("SELECT something")  # no FROM
        assert not _is_sql_string("UPDATE only a word")  # no SET

    def test_docstring_with_keyword_not_sql(self) -> None:
        """A docstring mentioning SELECT alone should not match."""
        assert not _is_sql_string(
            "This function can SELECT items"
        )  # No FROM clause following the SELECT


class TestExtractStringTokens:
    """Tests for _extract_string_tokens."""

    def test_simple_string(self) -> None:
        code = '''x = "SELECT id FROM courts"'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 1
        assert "SELECT id FROM courts" in tokens[0][1]

    def test_triple_quoted_string(self) -> None:
        code = '''x = """
            SELECT r.id FROM rulings r
        """'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 1
        assert "SELECT r.id FROM rulings r" in tokens[0][1]

    def test_skips_comments(self) -> None:
        code = "# SELECT id FROM courts\nx = 1"
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 0

    def test_ignore_comment_skips_string(self) -> None:
        """Strings with sql-check:ignore comment are skipped."""
        code = '''# sql-check:ignore
x = "SELECT r.bad_col FROM rulings r"'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 0

    def test_ignore_comment_on_same_line(self) -> None:
        code = '''x = "SELECT r.bad FROM rulings r"  # sql-check:ignore'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 0

    def test_ignore_on_closing_triple_quote(self) -> None:
        """sql-check:ignore on the closing triple-quote line works."""
        code = '''x = """
            SELECT r.bad FROM rulings r
        """  # sql-check:ignore'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 0


# ---------------------------------------------------------------------------
# Tests: Alias resolution
# ---------------------------------------------------------------------------


class TestResolveAliases:
    """Tests for _resolve_aliases."""

    def test_from_alias(self) -> None:
        sql = "SELECT c.id FROM cases c WHERE c.id = 1"
        aliases = _resolve_aliases(sql)
        assert aliases.get("c") == "cases"

    def test_join_alias(self) -> None:
        sql = "SELECT r.id FROM rulings r JOIN cases c ON c.id = r.case_id"
        aliases = _resolve_aliases(sql)
        assert aliases.get("r") == "rulings"
        assert aliases.get("c") == "cases"

    def test_update_table(self) -> None:
        sql = "UPDATE rulings SET outcome = 'granted' WHERE id = 1"
        aliases = _resolve_aliases(sql)
        assert aliases.get("rulings") == "rulings"

    def test_insert_table(self) -> None:
        sql = "INSERT INTO courts (id) VALUES (1)"
        aliases = _resolve_aliases(sql)
        assert aliases.get("courts") == "courts"

    def test_delete_table(self) -> None:
        sql = "DELETE FROM rulings WHERE id = 1"
        aliases = _resolve_aliases(sql)
        assert aliases.get("rulings") == "rulings"

    def test_skips_sql_keywords(self) -> None:
        """Words like ON, WHERE should not be treated as aliases."""
        sql = "SELECT r.id FROM rulings r WHERE r.outcome = 'granted'"
        aliases = _resolve_aliases(sql)
        assert "where" not in aliases
        assert "on" not in aliases


# ---------------------------------------------------------------------------
# Tests: Column reference extraction
# ---------------------------------------------------------------------------


class TestExtractColumnReferences:
    """Tests for _extract_column_references."""

    def test_qualified_column(self) -> None:
        sql = "SELECT r.id, r.case_id FROM rulings r"
        refs = _extract_column_references(sql)
        assert ("r", "id") in refs
        assert ("r", "case_id") in refs

    def test_skips_excluded_special_qualifiers(self) -> None:
        sql = "INSERT INTO courts (id) VALUES (1) ON CONFLICT DO UPDATE SET id = EXCLUDED.id"
        refs = _extract_column_references(sql)
        assert all(q != "excluded" for q, _ in refs)

    def test_skips_staging_schema(self) -> None:
        sql = "SELECT id FROM staging.captures"
        refs = _extract_column_references(sql)
        assert all(q != "staging" for q, _ in refs)

    def test_skips_file_extensions(self) -> None:
        """File paths like rulings.pdf should be ignored."""
        sql = "SELECT id FROM rulings WHERE path = 'rulings.pdf'"
        refs = _extract_column_references(sql)
        assert all(c != "pdf" for _, c in refs)

    def test_skips_url_patterns(self) -> None:
        """Domain-like patterns should be ignored."""
        sql = "SELECT id FROM courts WHERE url = 'courts.gov'"
        refs = _extract_column_references(sql)
        assert all(c != "gov" for _, c in refs)

    def test_skips_function_calls(self) -> None:
        """Function calls like lower(x) should not be treated as columns."""
        sql = "SELECT lower.something FROM courts"
        refs = _extract_column_references(sql)
        assert all(q != "lower" for q, _ in refs)


# ---------------------------------------------------------------------------
# Tests: Column validation
# ---------------------------------------------------------------------------


class TestValidateColumnRef:
    """Tests for _validate_column_ref."""

    def test_valid_column(self) -> None:
        aliases = {"r": "rulings"}
        table_cols = {"rulings": {"id", "case_id", "outcome"}}
        result = _validate_column_ref("r", "id", aliases, table_cols)
        assert result is None

    def test_invalid_column(self) -> None:
        aliases = {"r": "rulings"}
        table_cols = {"rulings": {"id", "case_id"}}
        result = _validate_column_ref("r", "nonexistent", aliases, table_cols)
        assert result is not None
        assert "nonexistent" in result
        assert "rulings" in result

    def test_unknown_alias_skipped(self) -> None:
        """Unknown aliases (CTEs, subqueries) are skipped without error."""
        aliases = {"r": "rulings"}
        table_cols = {"rulings": {"id"}}
        result = _validate_column_ref("cte", "val", aliases, table_cols)
        assert result is None

    def test_direct_table_reference(self) -> None:
        """Direct table.column (no alias needed) should work."""
        aliases: dict[str, str] = {}
        table_cols = {"courts": {"id", "state", "county"}}
        result = _validate_column_ref("courts", "state", aliases, table_cols)
        assert result is None

    def test_direct_table_invalid_column(self) -> None:
        aliases: dict[str, str] = {}
        table_cols = {"courts": {"id", "state"}}
        result = _validate_column_ref("courts", "bad_col", aliases, table_cols)
        assert result is not None
        assert "bad_col" in result


# ---------------------------------------------------------------------------
# Tests: File scanning (integration)
# ---------------------------------------------------------------------------


class TestScanFile:
    """Integration tests for scan_file."""

    def test_valid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "valid.py"
        f.write_text('''
QUERY = """
    SELECT r.id, r.case_id, r.outcome
    FROM rulings r
    JOIN cases c ON c.id = r.case_id
"""
''')
        table_cols = {
            "rulings": {"id", "case_id", "outcome"},
            "cases": {"id", "case_number"},
        }
        errors = scan_file(f, table_columns=table_cols)
        assert errors == []

    def test_invalid_column_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "invalid.py"
        f.write_text('''
QUERY = """
    SELECT d.id, d.updated_at
    FROM documents d
"""
''')
        table_cols = {"documents": {"id", "created_at"}}
        errors = scan_file(f, table_columns=table_cols)
        assert len(errors) == 1
        assert "updated_at" in errors[0]

    def test_url_string_not_flagged(self, tmp_path: Path) -> None:
        """URLs should not trigger false positives."""
        f = tmp_path / "urls.py"
        f.write_text('''
URL = "https://www.fresno.courts.ca.gov/tentative-rulings"
''')
        errors = scan_file(f, table_columns=TABLE_COLUMNS)
        assert errors == []

    def test_docstring_not_flagged(self, tmp_path: Path) -> None:
        """Docstrings without SQL clause patterns should not be flagged."""
        f = tmp_path / "doconly.py"
        f.write_text('''
def foo():
    """Fetches courts.ca.gov page for rulings."""
    pass
''')
        errors = scan_file(f, table_columns=TABLE_COLUMNS)
        assert errors == []

    def test_file_level_skip(self, tmp_path: Path) -> None:
        """Files with sql-check:skip-file are entirely skipped."""
        f = tmp_path / "skipped.py"
        f.write_text('''# sql-check:skip-file
QUERY = """
    SELECT d.nonexistent_col
    FROM documents d
"""
''')
        table_cols = {"documents": {"id"}}
        errors = scan_file(f, table_columns=table_cols)
        assert errors == []

    def test_ignore_comment_suppresses(self, tmp_path: Path) -> None:
        """Strings with sql-check:ignore comment are suppressed."""
        f = tmp_path / "ignored.py"
        f.write_text('''
# sql-check:ignore
BAD_QUERY = """
    SELECT d.nonexistent FROM documents d
"""

GOOD_QUERY = """
    SELECT d.id FROM documents d
"""
''')
        table_cols = {"documents": {"id"}}
        errors = scan_file(f, table_columns=table_cols)
        assert errors == []

    def test_no_sql_file(self, tmp_path: Path) -> None:
        f = tmp_path / "nosql.py"
        f.write_text("x = 1\ny = 2\n")
        errors = scan_file(f, table_columns=TABLE_COLUMNS)
        assert errors == []

    def test_catches_the_original_bug(self, tmp_path: Path) -> None:
        """The bug from #1929: documents.updated_at does not exist."""
        f = tmp_path / "buggy.py"
        f.write_text('''
UPDATE_QUERY = """
    UPDATE documents d
    SET d.updated_at = NOW()
    WHERE d.id = %s
"""
''')
        # documents table has created_at but NOT updated_at
        table_cols = {"documents": {"id", "created_at", "content_hash"}}
        errors = scan_file(f, table_columns=table_cols)
        assert len(errors) == 1
        assert "updated_at" in errors[0]
        assert "documents" in errors[0]


# ---------------------------------------------------------------------------
# Tests: Table columns completeness (against real schema)
# ---------------------------------------------------------------------------


class TestTableColumnsCompleteness:
    """Verify the auto-generated TABLE_COLUMNS map covers expected tables."""

    def test_has_core_tables(self) -> None:
        expected = {
            "courts",
            "judges",
            "cases",
            "documents",
            "rulings",
            "parties",
            "case_parties",
            "case_judges",
            "users",
        }
        assert expected.issubset(set(TABLE_COLUMNS.keys()))

    def test_courts_has_expected_columns(self) -> None:
        cols = TABLE_COLUMNS["courts"]
        assert {"id", "state", "county", "court_name", "court_code"}.issubset(cols)

    def test_documents_has_no_updated_at(self) -> None:
        """The original bug: documents does NOT have updated_at."""
        cols = TABLE_COLUMNS["documents"]
        assert "updated_at" not in cols
        assert "created_at" in cols

    def test_rulings_has_outcome(self) -> None:
        """Custom enum types should be recognized as valid types."""
        cols = TABLE_COLUMNS["rulings"]
        assert "outcome" in cols

    def test_rulings_has_expected_columns(self) -> None:
        cols = TABLE_COLUMNS["rulings"]
        expected = {
            "id", "document_id", "case_id", "judge_id", "court_id",
            "ruling_text", "ruling_text_html", "ruling_text_hash",
            "outcome", "motion_type", "hearing_date", "is_tentative",
        }
        assert expected.issubset(cols)

    def test_columns_are_auto_generated(self) -> None:
        """Verify the map is built from schema files, not hardcoded."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        fresh = build_table_columns(repo_root)
        assert fresh == TABLE_COLUMNS
