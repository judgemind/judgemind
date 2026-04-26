"""Tests for check-graphql-nullability-drift.py — GraphQL nullability drift
detection against synthetic migration and schema fixtures."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Import the module despite its dash-containing filename.
# Register it in sys.modules BEFORE exec_module so `@dataclass` can find
# its own module by name.
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "check-graphql-nullability-drift.py"
)
_spec = importlib.util.spec_from_file_location(
    "check_graphql_nullability_drift", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_graphql_nullability_drift"] = mod
_spec.loader.exec_module(mod)

parse_dropped_not_null = mod.parse_dropped_not_null
check_field_nonnull = mod.check_field_nonnull
find_graphql_drift = mod.find_graphql_drift
KNOWN_MAPPINGS = mod.KNOWN_MAPPINGS


# ---------------------------------------------------------------------------
# parse_dropped_not_null (reuses the same regex as check-nullable-column-reads)
# ---------------------------------------------------------------------------


class TestParseDroppedNotNull:
    def test_extracts_dispatcher_agents_issue_number(self) -> None:
        sql = (
            "ALTER TABLE dispatcher.agents\n"
            "    ALTER COLUMN issue_number DROP NOT NULL;\n"
        )
        result = parse_dropped_not_null(sql)
        assert result == [("agents", "issue_number")]

    def test_extracts_without_schema_prefix(self) -> None:
        sql = "ALTER TABLE agents ALTER COLUMN priority DROP NOT NULL;\n"
        result = parse_dropped_not_null(sql)
        assert result == [("agents", "priority")]

    def test_ignores_add_column(self) -> None:
        sql = "ALTER TABLE foo ADD COLUMN bar TEXT;\n"
        result = parse_dropped_not_null(sql)
        assert result == []

    def test_ignores_set_not_null(self) -> None:
        sql = "ALTER TABLE foo ALTER COLUMN bar SET NOT NULL;\n"
        result = parse_dropped_not_null(sql)
        assert result == []

    def test_extracts_multiple(self) -> None:
        sql = (
            "ALTER TABLE dispatcher.agents ALTER COLUMN issue_number DROP NOT NULL;\n"
            "ALTER TABLE dispatcher.failures ALTER COLUMN issue_number DROP NOT NULL;\n"
        )
        result = parse_dropped_not_null(sql)
        assert result == [("agents", "issue_number"), ("failures", "issue_number")]


# ---------------------------------------------------------------------------
# check_field_nonnull — unit tests for the GraphQL schema scanner
# ---------------------------------------------------------------------------


class TestCheckFieldNonnull:
    """Tests for check_field_nonnull: does it correctly detect non-null / nullable
    field declarations inside a GraphQL type block?"""

    _SCHEMA_NONNULL = """\
export const typeDefs = `#graphql
  type DispatcherAgent {
    id: ID!
    issueNumber: Int!
    priority: String
  }
`;
"""

    _SCHEMA_NULLABLE = """\
export const typeDefs = `#graphql
  type DispatcherAgent {
    id: ID!
    issueNumber: Int
    priority: String
  }
`;
"""

    _SCHEMA_NO_TYPE = """\
export const typeDefs = `#graphql
  type SomeOtherType {
    id: ID!
  }
`;
"""

    def test_nonnull_field_detected(self, tmp_path: Path) -> None:
        sf = tmp_path / "schema.ts"
        sf.write_text(self._SCHEMA_NONNULL)
        is_nonnull, line_no, line_text = check_field_nonnull(
            self._SCHEMA_NONNULL, sf, "DispatcherAgent", "issueNumber"
        )
        assert is_nonnull is True
        assert line_no > 0
        assert "Int!" in line_text

    def test_nullable_field_not_flagged(self, tmp_path: Path) -> None:
        sf = tmp_path / "schema.ts"
        sf.write_text(self._SCHEMA_NULLABLE)
        is_nonnull, line_no, line_text = check_field_nonnull(
            self._SCHEMA_NULLABLE, sf, "DispatcherAgent", "issueNumber"
        )
        assert is_nonnull is False
        assert line_no > 0  # field was found but is nullable
        assert "Int!" not in line_text

    def test_type_not_in_schema_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "schema.ts"
        sf.write_text(self._SCHEMA_NO_TYPE)
        is_nonnull, line_no, line_text = check_field_nonnull(
            self._SCHEMA_NO_TYPE, sf, "DispatcherAgent", "issueNumber"
        )
        assert is_nonnull is False
        assert line_no == 0


# ---------------------------------------------------------------------------
# find_graphql_drift — integration-level tests using tmp_path fixtures
# ---------------------------------------------------------------------------


class TestFindGraphqlDrift:
    """Tests for find_graphql_drift using synthetic migration SQL and
    GraphQL schema fixture files in tmp_path."""

    _MIGRATION_DROPS_ISSUE_NUMBER = (
        "ALTER TABLE dispatcher.agents\n    ALTER COLUMN issue_number DROP NOT NULL;\n"
    )

    _SCHEMA_NONNULL = """\
export const typeDefs = `#graphql
  type DispatcherAgent {
    id: ID!
    issueNumber: Int!
    priority: String
  }
  type RecentCompletion {
    agentId: ID!
    issueNumber: Int!
    status: String!
  }
`;
"""

    _SCHEMA_NULLABLE = """\
export const typeDefs = `#graphql
  type DispatcherAgent {
    id: ID!
    issueNumber: Int
    priority: String
  }
  type RecentCompletion {
    agentId: ID!
    issueNumber: Int
    status: String!
  }
`;
"""

    _MIGRATION_NO_DROP = (
        "ALTER TABLE dispatcher.agents ALTER COLUMN priority SET DEFAULT NULL;\n"
    )

    _MIGRATION_UNMAPPED_COLUMN = (
        "ALTER TABLE derived.rulings ALTER COLUMN hearing_date DROP NOT NULL;\n"
    )

    def _write_migration(self, tmp_path: Path, name: str, content: str) -> Path:
        mf = tmp_path / name
        mf.write_text(content)
        return mf

    def _write_schema(self, tmp_path: Path, name: str, content: str) -> Path:
        sf = tmp_path / name
        sf.write_text(content)
        return sf

    def test_drift_detected_returns_violation(self, tmp_path: Path) -> None:
        """Migration drops NOT NULL on agents.issue_number; GraphQL field
        DispatcherAgent.issueNumber is still Int! → exit 1 with a violation."""
        mf = self._write_migration(
            tmp_path, "49_test.sql", self._MIGRATION_DROPS_ISSUE_NUMBER
        )
        sf = self._write_schema(tmp_path, "schema.ts", self._SCHEMA_NONNULL)

        dropped = parse_dropped_not_null(self._MIGRATION_DROPS_ISSUE_NUMBER)
        violations = find_graphql_drift(mf, dropped, [sf])

        # Should flag at least DispatcherAgent.issueNumber and RecentCompletion.issueNumber
        assert len(violations) >= 1
        field_names = [(v.graphql_type, v.field_name) for v in violations]
        assert ("DispatcherAgent", "issueNumber") in field_names

        # Each violation names the migration file and the field declaration.
        for v in violations:
            assert v.migration_file == mf
            assert v.table == "agents"
            assert v.column == "issue_number"
            assert "Int!" in v.line_text

    def test_field_made_nullable_no_violation(self, tmp_path: Path) -> None:
        """Migration drops NOT NULL; GraphQL field is already nullable → exit 0."""
        mf = self._write_migration(
            tmp_path, "49_test.sql", self._MIGRATION_DROPS_ISSUE_NUMBER
        )
        sf = self._write_schema(tmp_path, "schema.ts", self._SCHEMA_NULLABLE)

        dropped = parse_dropped_not_null(self._MIGRATION_DROPS_ISSUE_NUMBER)
        violations = find_graphql_drift(mf, dropped, [sf])

        assert violations == []

    def test_migration_with_no_drop_not_null_exit_zero(self, tmp_path: Path) -> None:
        """Migration with no DROP NOT NULL → no violations."""
        mf = self._write_migration(tmp_path, "50_test.sql", self._MIGRATION_NO_DROP)
        sf = self._write_schema(tmp_path, "schema.ts", self._SCHEMA_NONNULL)

        dropped = parse_dropped_not_null(self._MIGRATION_NO_DROP)
        assert dropped == []
        violations = find_graphql_drift(mf, dropped, [sf])
        assert violations == []

    def test_unmapped_column_exit_zero(self, tmp_path: Path) -> None:
        """Column not in KNOWN_MAPPINGS → allowlist-driven, silently skip."""
        mf = self._write_migration(
            tmp_path, "16_test.sql", self._MIGRATION_UNMAPPED_COLUMN
        )
        sf = self._write_schema(tmp_path, "schema.ts", self._SCHEMA_NONNULL)

        dropped = parse_dropped_not_null(self._MIGRATION_UNMAPPED_COLUMN)
        assert dropped == [("rulings", "hearing_date")]

        # hearing_date is not in KNOWN_MAPPINGS for this check.
        assert ("rulings", "hearing_date") not in KNOWN_MAPPINGS

        violations = find_graphql_drift(mf, dropped, [sf])
        assert violations == []

    def test_multiple_schema_files_first_match_wins(self, tmp_path: Path) -> None:
        """When the field appears in the first schema file as nullable,
        the second file (where it might be non-null) is not consulted."""
        mf = self._write_migration(
            tmp_path, "49_test.sql", self._MIGRATION_DROPS_ISSUE_NUMBER
        )
        # First schema file: nullable (OK).
        sf1 = self._write_schema(
            tmp_path, "dispatcher_schema.ts", self._SCHEMA_NULLABLE
        )
        # Second schema file: still non-null (would be a violation if consulted).
        sf2 = self._write_schema(tmp_path, "root_schema.ts", self._SCHEMA_NONNULL)

        dropped = parse_dropped_not_null(self._MIGRATION_DROPS_ISSUE_NUMBER)
        violations = find_graphql_drift(mf, dropped, [sf1, sf2])

        # First file is nullable → no violations for fields found there.
        assert violations == []
