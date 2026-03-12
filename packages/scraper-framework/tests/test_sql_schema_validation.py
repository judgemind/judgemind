"""Schema validation tests for SQL queries across all scripts.

Validates that every alias.column reference in SQL query constants across
the codebase matches a real column in the schema DDL. This catches typos
like ``d.doc_type`` instead of ``d.document_type`` at test time.

See also: ``test_data_quality_check.py::TestSqlSchemaValidation`` for the
original per-module tests and helper unit tests.
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

# Add scripts/ to sys.path so we can import script modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# Skip venv re-exec during tests.
os.environ["_VENV_HELPER_SKIP"] = "1"

# ruff: noqa: E402
from helpers.schema_validation import (
    SCHEMA_SQL_PATH,
    get_query_constants,
    parse_schema_columns,
    validate_queries_against_schema,
)

# ---------------------------------------------------------------------------
# Script module loading helpers
# ---------------------------------------------------------------------------

# Cache the parsed schema for the entire test session.
_schema: dict[str, set[str]] | None = None


def _get_schema() -> dict[str, set[str]]:
    global _schema
    if _schema is None:
        _schema = parse_schema_columns()
    return _schema


def _import_script(name: str) -> ModuleType:
    """Import a script from ``scripts/`` by filename (without ``.py``)."""
    return import_module(name.replace("-", "_") if "-" not in name else name)


# Scripts with *_QUERY named constants.
_SCRIPTS_WITH_QUERY_CONSTANTS: list[tuple[str, list[str]]] = [
    (
        "cleanup_org_judges",
        [
            "FETCH_JUDGES_QUERY",
            "NULLIFY_RULINGS_QUERY",
            "COUNT_RULINGS_QUERY",
            "DELETE_CASE_JUDGES_QUERY",
            "COUNT_CASE_JUDGES_QUERY",
            "DELETE_JUDGE_ALIASES_QUERY",
            "DELETE_JUDGE_QUERY",
        ],
    ),
    (
        "dedup_documents",
        [
            "COUNT_ORPHANED_QUERY",
            "DELETE_ORPHANED_QUERY",
            "COUNT_CONTENT_HASH_GROUPS_QUERY",
        ],
    ),
    (
        "dedup_rulings",
        [
            "FIND_DUPLICATES_QUERY",
            "COUNT_DUPLICATES_QUERY",
        ],
    ),
    (
        "merge_honorific_judges",
        [
            "FETCH_HONORIFIC_JUDGES_QUERY",
            "FIND_MERGE_TARGET_QUERY",
            "UPDATE_RULINGS_QUERY",
            "COUNT_RULINGS_QUERY",
            "DELETE_CONFLICTING_CASE_JUDGES_QUERY",
            "UPDATE_CASE_JUDGES_QUERY",
            "COUNT_CASE_JUDGES_QUERY",
            "DELETE_DUPLICATE_ALIASES_QUERY",
            "UPDATE_ALIASES_QUERY",
            "DELETE_JUDGE_QUERY",
            "RENAME_JUDGE_QUERY",
        ],
    ),
    (
        "backfill_ruling_fields",
        [
            "FETCH_QUERY",
            "UPDATE_QUERY",
            "CASE_JUDGES_BACKFILL_QUERY",
        ],
    ),
    (
        "backfill_la_judge_from_dept",
        [
            "FETCH_QUERY",
            "UPDATE_RULING_JUDGE_QUERY",
            "SNAPSHOT_QUERY",
        ],
    ),
    (
        "backfill_parties",
        [
            "FETCH_QUERY",
        ],
    ),
    (
        "audit_field_completeness",
        [
            "AUDIT_QUERY",
            "VERBOSE_QUERY",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchemaFileAvailable:
    """Ensure the schema DDL is available for all validation tests."""

    def test_schema_file_exists(self) -> None:
        assert SCHEMA_SQL_PATH.exists(), f"Schema file not found at {SCHEMA_SQL_PATH}"


class TestQueryConstantsDiscovery:
    """Verify that expected query constants are discovered in each script."""

    @pytest.mark.parametrize(
        "script_name,expected_constants",
        _SCRIPTS_WITH_QUERY_CONSTANTS,
        ids=[s[0] for s in _SCRIPTS_WITH_QUERY_CONSTANTS],
    )
    def test_query_constants_discovered(
        self,
        script_name: str,
        expected_constants: list[str],
    ) -> None:
        """All expected *_QUERY constants exist in the script module."""
        mod = _import_script(script_name)
        queries = get_query_constants(mod)
        for name in expected_constants:
            assert name in queries, (
                f"Query constant '{name}' not found in {script_name}. "
                f"Found: {sorted(queries.keys())}"
            )


class TestColumnReferencesExistInSchema:
    """Validate alias.column references in SQL queries against schema.sql."""

    @pytest.mark.parametrize(
        "script_name,_expected",
        _SCRIPTS_WITH_QUERY_CONSTANTS,
        ids=[s[0] for s in _SCRIPTS_WITH_QUERY_CONSTANTS],
    )
    def test_all_column_references_valid(
        self,
        script_name: str,
        _expected: list[str],
    ) -> None:
        """Every alias.column reference must map to a real schema column."""
        mod = _import_script(script_name)
        queries = get_query_constants(mod)
        schema = _get_schema()
        errors = validate_queries_against_schema(queries, schema)
        assert not errors, (
            f"SQL queries in {script_name} reference non-existent columns:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


class TestCleanupJudgeNamesInlineSQL:
    """Validate inline SQL in cleanup_judge_names.py.

    This script builds SQL dynamically in functions rather than using named
    constants. We validate the static SELECT query used in ``_fetch_judges``.
    """

    def test_fetch_judges_query_columns_valid(self) -> None:
        """The SELECT query in _fetch_judges references valid schema columns."""
        schema = _get_schema()

        # This is the SQL from _fetch_judges() — inline, not a named constant.
        fetch_query = """
            SELECT j.id, j.canonical_name, j.court_id, c.court_code,
                   COUNT(r.id) AS ruling_count
            FROM judges j
            JOIN courts c ON c.id = j.court_id
            LEFT JOIN rulings r ON r.judge_id = j.id
        """
        errors = validate_queries_against_schema({"_fetch_judges": fetch_query}, schema)
        assert not errors, (
            "cleanup_judge_names._fetch_judges SQL references non-existent columns:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    def test_merge_judge_query_columns_valid(self) -> None:
        """The UPDATE/DELETE queries in _merge_judge reference valid tables."""
        schema = _get_schema()

        # Key inline queries from _merge_judge and _delete_judge
        inline_queries = {
            "_merge_rulings": "UPDATE rulings SET judge_id = %s WHERE judge_id = %s",
            "_merge_case_judges_check": """
                UPDATE case_judges SET judge_id = %s
                WHERE judge_id = %s
            """,
            "_merge_case_judges_conflict": """
                SELECT 1 FROM case_judges cj2
                WHERE cj2.case_id = %s AND cj2.judge_id = %s
            """,
            "_merge_aliases_check": """
                SELECT 1 FROM judge_aliases ja2
                WHERE ja2.judge_id = %s AND ja2.raw_name = %s
            """,
            "_delete_case_judges": "DELETE FROM case_judges WHERE judge_id = %s",
            "_delete_aliases": "DELETE FROM judge_aliases WHERE judge_id = %s",
            "_delete_judge": "DELETE FROM judges WHERE id = %s",
            "_rename_judge": "UPDATE judges SET canonical_name = %s WHERE id = %s",
        }
        errors = validate_queries_against_schema(inline_queries, schema)
        assert not errors, (
            "cleanup_judge_names inline SQL references non-existent columns:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
