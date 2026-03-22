"""Schema validation tests for SQL queries across all scripts.

Validates that every alias.column reference in SQL query constants across
the codebase matches a real column in the schema DDL. This catches typos
like ``d.doc_type`` instead of ``d.document_type`` at test time.

Auto-discovers scripts with ``*_QUERY`` constants by scanning
``scripts/backfill_*.py``, ``scripts/cleanup_*.py``, ``scripts/dedup_*.py``,
``scripts/merge_*.py``, and ``scripts/audit_*.py``.

See also: ``test_data_quality_check.py::TestSqlSchemaValidation`` for the
``data-quality-check.py`` specific tests and helper unit tests.
"""

from __future__ import annotations

import logging
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

# Add scripts/ to sys.path so we can import script modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

# ruff: noqa: E402
from helpers.schema_validation import (
    SCHEMA_SQL_PATH,
    get_query_constants,
    parse_schema_columns,
    validate_queries_against_schema,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Script module loading helpers
# ---------------------------------------------------------------------------

# Cache the parsed schema for the entire test session.
_schema: dict[str, set[str]] | None = None

# Glob patterns for scripts that may contain *_QUERY constants.
_SCRIPT_GLOB_PATTERNS: list[str] = [
    "backfill_*.py",
    "cleanup_*.py",
    "dedup_*.py",
    "merge_*.py",
    "audit_*.py",
]

# Scripts excluded from auto-discovery because they are tested elsewhere
# (e.g. data-quality-check.py is tested in test_data_quality_check.py).
_EXCLUDED_SCRIPTS: set[str] = {
    "data-quality-check",
}

# Scripts with known schema drift (column references that don't match
# schema.sql). These are marked as xfail in the column-reference
# validation test so they surface visually but don't block CI.
# Each entry maps a script name to the reason for the drift.
_KNOWN_SCHEMA_DRIFT: dict[str, str] = {}


def _get_schema() -> dict[str, set[str]]:
    global _schema
    if _schema is None:
        _schema = parse_schema_columns()
    return _schema


def _import_script(name: str) -> ModuleType:
    """Import a script from ``scripts/`` by filename (without ``.py``)."""
    return import_module(name.replace("-", "_") if "-" not in name else name)


def _discover_scripts_with_queries() -> list[tuple[str, list[str]]]:
    """Auto-discover scripts in ``scripts/`` that have ``*_QUERY`` constants.

    Scans files matching :data:`_SCRIPT_GLOB_PATTERNS`, imports each one,
    and uses :func:`get_query_constants` to find uppercase attributes ending
    in ``_QUERY``.  Scripts that fail to import or have no query constants
    are silently skipped.

    Returns a sorted list of ``(script_name, [constant_names])`` tuples.
    """
    discovered: list[tuple[str, list[str]]] = []

    for pattern in _SCRIPT_GLOB_PATTERNS:
        for path in sorted(_SCRIPTS_DIR.glob(pattern)):
            script_name = path.stem  # e.g. "backfill_ruling_fields"
            if script_name in _EXCLUDED_SCRIPTS:
                continue
            try:
                mod = _import_script(script_name)
            except Exception:  # noqa: BLE001
                logger.warning("Could not import script %s, skipping", script_name)
                continue
            queries = get_query_constants(mod)
            if queries:
                discovered.append((script_name, sorted(queries.keys())))

    return discovered


# Auto-discover scripts with *_QUERY constants at collection time.
_SCRIPTS_WITH_QUERY_CONSTANTS: list[tuple[str, list[str]]] = _discover_scripts_with_queries()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchemaFileAvailable:
    """Ensure the schema DDL is available for all validation tests."""

    def test_schema_file_exists(self) -> None:
        assert SCHEMA_SQL_PATH.exists(), f"Schema file not found at {SCHEMA_SQL_PATH}"


class TestAutoDiscovery:
    """Verify that auto-discovery finds scripts with query constants."""

    def test_discovery_finds_known_scripts(self) -> None:
        """Auto-discovery must find scripts that are known to have queries."""
        discovered_names = {name for name, _ in _SCRIPTS_WITH_QUERY_CONSTANTS}
        # These scripts are known to have *_QUERY constants and must always
        # be discovered.  If any disappears, the glob patterns or import
        # logic may have regressed.
        expected_names = {
            "audit_field_completeness",
            "backfill_la_judge_from_dept",
            "backfill_parties",
            "backfill_ruling_fields",
            "cleanup_org_judges",
            "dedup_documents",
            "dedup_rulings",
            "merge_honorific_judges",
        }
        missing = expected_names - discovered_names
        assert not missing, (
            f"Auto-discovery failed to find these known scripts: {sorted(missing)}. "
            f"Discovered: {sorted(discovered_names)}"
        )

    def test_discovery_finds_query_constants(self) -> None:
        """Every discovered script must have at least one *_QUERY constant."""
        for script_name, constants in _SCRIPTS_WITH_QUERY_CONSTANTS:
            assert constants, f"Script '{script_name}' was discovered but has no query constants"
            for const in constants:
                assert const.endswith("_QUERY") and const.isupper(), (
                    f"Constant '{const}' in {script_name} does not match *_QUERY naming convention"
                )


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


def _parametrize_with_xfail() -> list[pytest.param | tuple[str, list[str]]]:
    """Build parametrize args, marking known-drift scripts as xfail."""
    params: list[pytest.param | tuple[str, list[str]]] = []
    for script_name, constants in _SCRIPTS_WITH_QUERY_CONSTANTS:
        if script_name in _KNOWN_SCHEMA_DRIFT:
            params.append(
                pytest.param(
                    script_name,
                    constants,
                    id=script_name,
                    marks=pytest.mark.xfail(
                        reason=_KNOWN_SCHEMA_DRIFT[script_name],
                        strict=False,
                    ),
                )
            )
        else:
            params.append(pytest.param(script_name, constants, id=script_name))
    return params


class TestColumnReferencesExistInSchema:
    """Validate alias.column references in SQL queries against schema.sql."""

    @pytest.mark.parametrize(
        "script_name,_expected",
        _parametrize_with_xfail(),
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
