"""Tests for validation.schema — runtime schema validation for backfill scripts."""
# sql-check:skip-file — this file intentionally contains invalid SQL to test
# the schema validation system.

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from validation.schema import (
    add_validate_schema_flag,
    extract_alias_to_table,
    extract_column_references,
    get_query_constants,
    parse_schema_ddl,
    validate_columns,
    validate_queries,
    validate_script_queries,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_schema() -> dict[str, set[str]]:
    """A minimal schema for testing."""
    return {
        "rulings": {"id", "case_id", "ruling_text", "judge_id", "outcome", "hearing_date"},
        "cases": {"id", "case_number", "case_title", "court_id", "created_at"},
        "judges": {"id", "canonical_name", "court_id", "updated_at"},
        "courts": {"id", "court_code", "name"},
    }


@pytest.fixture()
def sample_ddl(tmp_path: Path) -> Path:
    """Write a minimal schema DDL file and return its path."""
    ddl = textwrap.dedent("""\
        CREATE TABLE courts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            court_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL
        );

        CREATE TABLE cases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            court_id UUID NOT NULL,
            case_number TEXT NOT NULL,
            case_title TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (court_id, case_number)
        );

        CREATE TABLE judges (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            canonical_name TEXT NOT NULL,
            court_id UUID NOT NULL,
            updated_at TIMESTAMPTZ
        );

        CREATE TABLE rulings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            case_id UUID NOT NULL,
            ruling_text TEXT,
            judge_id UUID,
            outcome TEXT,
            hearing_date DATE,
            FOREIGN KEY (case_id) REFERENCES cases(id)
        );
    """)
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(ddl)
    return schema_file


def _make_module(name: str, **attrs: Any) -> ModuleType:
    """Create a synthetic module with given attributes."""
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# ---------------------------------------------------------------------------
# Tests: parse_schema_ddl
# ---------------------------------------------------------------------------


class TestParseSchemaGDL:
    """Tests for DDL parsing."""

    def test_parses_tables_and_columns(self, sample_ddl: Path) -> None:
        schema = parse_schema_ddl(sample_ddl)
        assert "courts" in schema
        assert "cases" in schema
        assert "judges" in schema
        assert "rulings" in schema

    def test_column_names_correct(self, sample_ddl: Path) -> None:
        schema = parse_schema_ddl(sample_ddl)
        assert "court_code" in schema["courts"]
        assert "case_number" in schema["cases"]
        assert "canonical_name" in schema["judges"]
        assert "ruling_text" in schema["rulings"]

    def test_excludes_constraints(self, sample_ddl: Path) -> None:
        schema = parse_schema_ddl(sample_ddl)
        # UNIQUE and FOREIGN KEY should not appear as columns
        for table_cols in schema.values():
            for col in table_cols:
                assert col not in ("unique", "foreign", "primary", "constraint")

    def test_handles_if_not_exists(self, tmp_path: Path) -> None:
        ddl = textwrap.dedent("""\
            CREATE TABLE IF NOT EXISTS test_table (
                id UUID PRIMARY KEY,
                name TEXT NOT NULL
            );
        """)
        f = tmp_path / "schema.sql"
        f.write_text(ddl)
        schema = parse_schema_ddl(f)
        assert "test_table" in schema
        assert "id" in schema["test_table"]
        assert "name" in schema["test_table"]


# ---------------------------------------------------------------------------
# Tests: validate_columns
# ---------------------------------------------------------------------------


class TestValidateColumns:
    """Tests for the validate_columns helper."""

    def test_valid_columns_returns_empty(self, sample_schema: dict[str, set[str]]) -> None:
        errors = validate_columns(sample_schema, "rulings", ["id", "case_id", "ruling_text"])
        assert errors == []

    def test_invalid_column_returns_error(self, sample_schema: dict[str, set[str]]) -> None:
        errors = validate_columns(sample_schema, "rulings", ["id", "nonexistent_col"])
        assert len(errors) == 1
        assert "nonexistent_col" in errors[0]
        assert "rulings" in errors[0]

    def test_nonexistent_table_returns_error(self, sample_schema: dict[str, set[str]]) -> None:
        errors = validate_columns(sample_schema, "nonexistent_table", ["id"])
        assert len(errors) == 1
        assert "does not exist in schema" in errors[0]

    def test_case_insensitive(self, sample_schema: dict[str, set[str]]) -> None:
        errors = validate_columns(sample_schema, "Rulings", ["ID", "Case_Id"])
        assert errors == []


# ---------------------------------------------------------------------------
# Tests: extract_alias_to_table / extract_column_references
# ---------------------------------------------------------------------------


class TestAliasExtraction:
    """Tests for SQL alias and column reference extraction."""

    def test_simple_from(self) -> None:
        query = "SELECT r.id FROM rulings r WHERE r.case_id = %s"
        aliases = extract_alias_to_table(query)
        assert aliases == {"r": "rulings"}

    def test_join(self) -> None:
        query = """
            SELECT r.id, c.case_number
            FROM rulings r
            JOIN cases c ON c.id = r.case_id
        """
        aliases = extract_alias_to_table(query)
        assert aliases["r"] == "rulings"
        assert aliases["c"] == "cases"

    def test_left_join(self) -> None:
        query = """
            SELECT j.canonical_name
            FROM judges j
            LEFT JOIN rulings r ON r.judge_id = j.id
        """
        aliases = extract_alias_to_table(query)
        assert aliases["j"] == "judges"
        assert aliases["r"] == "rulings"

    def test_skips_sql_keywords(self) -> None:
        query = "SELECT * FROM rulings ON WHERE AND"
        aliases = extract_alias_to_table(query)
        # 'on', 'where', 'and' should not appear as aliases
        for keyword in ("on", "where", "and"):
            assert keyword not in aliases

    def test_update_with_alias(self) -> None:
        query = "UPDATE rulings r SET r.outcome = %s WHERE r.id = %s"
        aliases = extract_alias_to_table(query)
        assert aliases["r"] == "rulings"

    def test_update_without_alias(self) -> None:
        query = "UPDATE rulings SET outcome = %s WHERE id = %s"
        aliases = extract_alias_to_table(query)
        # No alias — 'set' should not be captured as an alias
        assert "set" not in aliases

    def test_column_references(self) -> None:
        query = (
            "SELECT r.id, r.case_id, c.case_number FROM rulings r JOIN cases c ON c.id = r.case_id"
        )
        refs = extract_column_references(query)
        assert ("r", "id") in refs
        assert ("r", "case_id") in refs
        assert ("c", "case_number") in refs
        assert ("c", "id") in refs

    def test_skips_string_literals(self) -> None:
        query = "SELECT r.id FROM rulings r WHERE r.outcome = 'x.y'"
        refs = extract_column_references(query)
        assert ("x", "y") not in refs

    def test_skips_format_placeholders(self) -> None:
        query = "SELECT r.id FROM rulings r WHERE {county_filter}"
        refs = extract_column_references(query)
        # county_filter should not be in refs
        alias_names = [a for a, _ in refs]
        assert "county_filter" not in alias_names


# ---------------------------------------------------------------------------
# Tests: get_query_constants
# ---------------------------------------------------------------------------


class TestGetQueryConstants:
    """Tests for module query constant discovery."""

    def test_discovers_query_constants(self) -> None:
        mod = _make_module(
            "test_mod",
            FETCH_QUERY="SELECT 1",
            UPDATE_QUERY="UPDATE t SET x = 1",
            NOT_A_QUERY=42,
            some_query="not uppercase",
        )
        queries = get_query_constants(mod)
        assert "FETCH_QUERY" in queries
        assert "UPDATE_QUERY" in queries
        assert "NOT_A_QUERY" not in queries
        assert "some_query" not in queries

    def test_empty_module(self) -> None:
        mod = _make_module("empty_mod")
        queries = get_query_constants(mod)
        assert queries == {}


# ---------------------------------------------------------------------------
# Tests: validate_queries
# ---------------------------------------------------------------------------


class TestValidateQueries:
    """Tests for SQL query validation against schema."""

    def test_valid_queries_pass(self, sample_schema: dict[str, set[str]]) -> None:
        queries = {
            "FETCH_QUERY": """
                SELECT r.id, r.ruling_text, r.judge_id
                FROM rulings r
                WHERE r.case_id = %s
            """,
        }
        errors = validate_queries(queries, sample_schema)
        assert errors == []

    def test_invalid_column_detected(self, sample_schema: dict[str, set[str]]) -> None:
        queries = {
            "BAD_QUERY": """
                SELECT r.id, r.nonexistent_col
                FROM rulings r
            """,
        }
        errors = validate_queries(queries, sample_schema)
        assert len(errors) == 1
        assert "nonexistent_col" in errors[0]
        assert "BAD_QUERY" in errors[0]

    def test_join_query_validates_both_tables(self, sample_schema: dict[str, set[str]]) -> None:
        queries = {
            "JOIN_QUERY": """
                SELECT r.id, c.case_number
                FROM rulings r
                JOIN cases c ON c.id = r.case_id
            """,
        }
        errors = validate_queries(queries, sample_schema)
        assert errors == []

    def test_invalid_column_in_join(self, sample_schema: dict[str, set[str]]) -> None:
        queries = {
            "BAD_JOIN_QUERY": """
                SELECT r.id, c.wrong_column
                FROM rulings r
                JOIN cases c ON c.id = r.case_id
            """,
        }
        errors = validate_queries(queries, sample_schema)
        assert len(errors) == 1
        assert "wrong_column" in errors[0]

    def test_unknown_alias_skipped(self, sample_schema: dict[str, set[str]]) -> None:
        """Aliases not in FROM/JOIN (e.g. CTEs) are silently skipped."""
        queries = {
            "CTE_QUERY": """
                WITH cte AS (SELECT 1)
                SELECT cte.val, r.id
                FROM rulings r
            """,
        }
        errors = validate_queries(queries, sample_schema)
        assert errors == []

    def test_multiple_errors_reported(self, sample_schema: dict[str, set[str]]) -> None:
        queries = {
            "MULTI_BAD_QUERY": """
                SELECT r.bad_col1, r.bad_col2
                FROM rulings r
            """,
        }
        errors = validate_queries(queries, sample_schema)
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# Tests: validate_script_queries (with mock connection)
# ---------------------------------------------------------------------------


class TestValidateScriptQueries:
    """Tests for the high-level validate_script_queries function."""

    def _mock_conn(self, schema_rows: list[tuple[str, str]]) -> MagicMock:
        """Create a mock psycopg connection returning schema_rows from cursor."""
        conn = MagicMock()
        cursor_ctx = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = schema_rows
        cursor_ctx.__enter__ = MagicMock(return_value=cursor)
        cursor_ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor_ctx
        return conn

    def test_valid_module_passes(self) -> None:
        schema_rows = [
            ("rulings", "id"),
            ("rulings", "case_id"),
            ("rulings", "ruling_text"),
        ]
        conn = self._mock_conn(schema_rows)
        mod = _make_module(
            "good_script",
            FETCH_QUERY="SELECT r.id, r.ruling_text FROM rulings r",
        )
        errors = validate_script_queries(conn, mod)
        assert errors == []

    def test_invalid_column_fails(self) -> None:
        schema_rows = [
            ("rulings", "id"),
            ("rulings", "case_id"),
        ]
        conn = self._mock_conn(schema_rows)
        mod = _make_module(
            "bad_script",
            FETCH_QUERY="SELECT r.id, r.nonexistent FROM rulings r",
        )
        errors = validate_script_queries(conn, mod)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_no_queries_warns(self) -> None:
        conn = self._mock_conn([])
        mod = _make_module("no_queries_script")
        errors = validate_script_queries(conn, mod)
        assert errors == []


# ---------------------------------------------------------------------------
# Tests: add_validate_schema_flag
# ---------------------------------------------------------------------------


class TestAddValidateSchemaFlag:
    """Tests for the argparse helper."""

    def test_adds_flag(self) -> None:
        parser = argparse.ArgumentParser()
        add_validate_schema_flag(parser)
        args = parser.parse_args(["--validate-schema"])
        assert args.validate_schema is True

    def test_default_is_false(self) -> None:
        parser = argparse.ArgumentParser()
        add_validate_schema_flag(parser)
        args = parser.parse_args([])
        assert args.validate_schema is False


# ---------------------------------------------------------------------------
# Tests: end-to-end validate_schema workflow
# ---------------------------------------------------------------------------


class TestEndToEndValidation:
    """Test the full --validate-schema workflow as used by backfill scripts.

    Acceptance criterion: running with --validate-schema on a script with
    a wrong column name produces a clear error *before* any writes.
    """

    def _mock_conn(self, schema_rows: list[tuple[str, str]]) -> MagicMock:
        """Create a mock connection returning the given schema rows."""
        conn = MagicMock()
        cursor_ctx = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = schema_rows
        cursor_ctx.__enter__ = MagicMock(return_value=cursor)
        cursor_ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor_ctx
        return conn

    def test_wrong_column_produces_clear_error(self) -> None:
        """A script referencing parties.name (wrong) instead of
        parties.canonical_name should produce a clear validation error."""
        schema_rows = [
            ("parties", "id"),
            ("parties", "canonical_name"),
            ("parties", "party_type"),
        ]
        conn = self._mock_conn(schema_rows)

        # Simulate a script that uses the wrong column name
        mod = _make_module(
            "bad_backfill",
            UPDATE_QUERY="UPDATE parties p SET p.name = %s WHERE p.id = %s",
        )

        errors = validate_script_queries(conn, mod)
        assert len(errors) == 1
        assert "name" in errors[0]
        assert "parties" in errors[0]
        assert "canonical_name" in str(errors[0])  # Shows available columns

    def test_correct_column_passes(self) -> None:
        """A script referencing parties.canonical_name (correct) passes."""
        schema_rows = [
            ("parties", "id"),
            ("parties", "canonical_name"),
            ("parties", "party_type"),
        ]
        conn = self._mock_conn(schema_rows)

        mod = _make_module(
            "good_backfill",
            UPDATE_QUERY=("UPDATE parties p SET p.canonical_name = %s WHERE p.id = %s"),
        )

        errors = validate_script_queries(conn, mod)
        assert errors == []

    def test_validation_uses_live_schema_not_hardcoded(self) -> None:
        """Validation should reflect the actual DB schema, not a static list."""
        # Schema has a column that doesn't exist in the DDL file
        schema_rows = [
            ("rulings", "id"),
            ("rulings", "custom_field"),
        ]
        conn = self._mock_conn(schema_rows)

        mod = _make_module(
            "custom_field_script",
            FETCH_QUERY="SELECT r.custom_field FROM rulings r",
        )

        errors = validate_script_queries(conn, mod)
        assert errors == [], "Should pass because live schema has custom_field"

    def test_error_message_includes_available_columns(self) -> None:
        """Error messages should include the list of available columns."""
        schema_rows = [
            ("judges", "id"),
            ("judges", "canonical_name"),
            ("judges", "court_id"),
        ]
        conn = self._mock_conn(schema_rows)

        mod = _make_module(
            "typo_script",
            FETCH_QUERY="SELECT j.cannonical_name FROM judges j",
        )

        errors = validate_script_queries(conn, mod)
        assert len(errors) == 1
        # Error should mention the typo'd column
        assert "cannonical_name" in errors[0]
        # Error should list available columns for debugging
        assert "canonical_name" in errors[0]
