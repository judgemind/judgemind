"""Tests for check-sql-conflicts.py -- ON CONFLICT target validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Import the module despite its dash-containing filename.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "check-sql-conflicts.py"
_spec = importlib.util.spec_from_file_location("check_sql_conflicts", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
check_sql_conflicts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_sql_conflicts)

# Pull out the functions we want to test.
_extract_string_tokens = check_sql_conflicts._extract_string_tokens
_extract_conflicts_from_strings = check_sql_conflicts._extract_conflicts_from_strings
_validate_conflict = check_sql_conflicts._validate_conflict
_extract_unique_constraints_from_sql = (
    check_sql_conflicts._extract_unique_constraints_from_sql
)
_normalize_sql_statements = check_sql_conflicts._normalize_sql_statements
build_unique_constraints = check_sql_conflicts.build_unique_constraints
scan_file = check_sql_conflicts.scan_file
UNIQUE_CONSTRAINTS = check_sql_conflicts.UNIQUE_CONSTRAINTS


class TestExtractStringTokens:
    """Tests for _extract_string_tokens."""

    def test_simple_string(self) -> None:
        code = '''x = "INSERT INTO courts VALUES (1)"'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 1
        assert "INSERT INTO courts" in tokens[0][1]

    def test_triple_quoted_string(self) -> None:
        code = '''x = """
            INSERT INTO courts VALUES (1)
            ON CONFLICT (court_code) DO NOTHING
        """'''
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 1
        assert "ON CONFLICT" in tokens[0][1]

    def test_skips_comments(self) -> None:
        code = "# INSERT INTO courts ON CONFLICT (court_code)\nx = 1"
        tokens = _extract_string_tokens(code)
        assert len(tokens) == 0

    def test_skips_non_string_code(self) -> None:
        code = "INSERT = 'not sql'\nINTO = 'also not'"
        tokens = _extract_string_tokens(code)
        # These are string tokens but don't contain INSERT INTO together
        assert all("INSERT INTO" not in t[1] for t in tokens)


class TestExtractConflictsFromStrings:
    """Tests for _extract_conflicts_from_strings."""

    def test_insert_with_conflict_columns(self) -> None:
        code = '''cur.execute("""
            INSERT INTO cases (case_number, court_id)
            VALUES (%s, %s)
            ON CONFLICT (court_id, case_number) DO UPDATE
                SET case_number = EXCLUDED.case_number
        """)'''
        conflicts = _extract_conflicts_from_strings(code)
        assert len(conflicts) == 1
        _line, table, cols = conflicts[0]
        assert table == "cases"
        assert cols == frozenset({"court_id", "case_number"})

    def test_insert_with_bare_conflict(self) -> None:
        code = '''cur.execute("""
            INSERT INTO judge_aliases (judge_id, raw_name)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """)'''
        conflicts = _extract_conflicts_from_strings(code)
        assert len(conflicts) == 1
        _line, table, cols = conflicts[0]
        assert table == "judge_aliases"
        assert cols is None

    def test_skips_docstrings_without_insert(self) -> None:
        code = '''def foo():
    """This mentions ON CONFLICT (document_id) in a docstring."""
    pass'''
        conflicts = _extract_conflicts_from_strings(code)
        assert len(conflicts) == 0

    def test_skips_comments(self) -> None:
        code = "# INSERT INTO foo ON CONFLICT (bar) DO NOTHING"
        conflicts = _extract_conflicts_from_strings(code)
        assert len(conflicts) == 0

    def test_multiple_conflicts_in_one_file(self) -> None:
        code = '''
a = """INSERT INTO courts (state) VALUES (%s)
ON CONFLICT (court_code) DO NOTHING"""

b = """INSERT INTO cases (case_number, court_id) VALUES (%s, %s)
ON CONFLICT (court_id, case_number) DO UPDATE SET updated_at = NOW()"""
'''
        conflicts = _extract_conflicts_from_strings(code)
        assert len(conflicts) == 2
        tables = {c[1] for c in conflicts}
        assert tables == {"courts", "cases"}

    def test_schema_prefixed_table(self) -> None:
        code = '''cur.execute("""
            INSERT INTO staging.captures (court_id)
            VALUES (%s)
            ON CONFLICT (id) DO NOTHING
        """)'''
        conflicts = _extract_conflicts_from_strings(code)
        assert len(conflicts) == 1
        assert conflicts[0][1] == "staging.captures"


class TestValidateConflict:
    """Tests for _validate_conflict."""

    def test_valid_single_column(self) -> None:
        result = _validate_conflict("courts", frozenset({"court_code"}))
        assert result is None

    def test_valid_multi_column(self) -> None:
        result = _validate_conflict("cases", frozenset({"court_id", "case_number"}))
        assert result is None

    def test_valid_bare_conflict(self) -> None:
        result = _validate_conflict("courts", None)
        assert result is None

    def test_invalid_column(self) -> None:
        result = _validate_conflict("parties", frozenset({"canonical_name"}))
        assert result is not None
        assert "no UNIQUE constraint" in result
        assert "parties" in result

    def test_invalid_partial_columns(self) -> None:
        result = _validate_conflict("case_parties", frozenset({"case_id", "party_id"}))
        assert result is not None
        assert "no UNIQUE constraint" in result

    def test_unknown_table(self) -> None:
        result = _validate_conflict("nonexistent_table", frozenset({"id"}))
        assert result is not None
        assert "unknown table" in result

    def test_valid_primary_key(self) -> None:
        result = _validate_conflict("documents", frozenset({"id"}))
        assert result is None


class TestScanFile:
    """Integration tests for scan_file."""

    def test_valid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "valid.py"
        f.write_text('''
cur.execute("""
    INSERT INTO courts (state, county, court_name, court_code)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (court_code) DO NOTHING
""")
''')
        errors = scan_file(f)
        assert errors == []

    def test_invalid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "invalid.py"
        f.write_text('''
cur.execute("""
    INSERT INTO parties (canonical_name)
    VALUES (%s)
    ON CONFLICT (canonical_name) DO UPDATE
        SET canonical_name = EXCLUDED.canonical_name
""")
''')
        errors = scan_file(f)
        assert len(errors) == 1
        assert "no UNIQUE constraint" in errors[0]

    def test_file_with_no_sql(self, tmp_path: Path) -> None:
        f = tmp_path / "nosql.py"
        f.write_text("x = 1\ny = 2\n")
        errors = scan_file(f)
        assert errors == []

    def test_docstring_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "doconly.py"
        f.write_text('''
def foo():
    """Uses ON CONFLICT (canonical_name) on parties table."""
    pass
''')
        errors = scan_file(f)
        assert errors == []


class TestNormalizeSqlStatements:
    """Tests for _normalize_sql_statements."""

    def test_single_line_statement(self) -> None:
        sql = "ALTER TABLE foo ADD COLUMN bar TEXT;"
        stmts = _normalize_sql_statements(sql)
        assert len(stmts) == 1
        assert "alter table foo add column bar text;" in stmts[0]

    def test_multi_line_statement_joined(self) -> None:
        sql = "ALTER TABLE judges\n    ADD CONSTRAINT uq UNIQUE (a, b);"
        stmts = _normalize_sql_statements(sql)
        assert len(stmts) == 1
        assert "alter table judges add constraint uq unique (a, b);" in stmts[0]

    def test_comments_and_blanks_flush_statement(self) -> None:
        sql = "ALTER TABLE a ADD COLUMN x TEXT;\n\n-- comment\nALTER TABLE b ADD COLUMN y TEXT;"
        stmts = _normalize_sql_statements(sql)
        assert len(stmts) == 2

    def test_create_unique_index_multi_line(self) -> None:
        sql = (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_test\n"
            "    ON rulings (case_id, ruling_text_hash)\n"
            "    WHERE ruling_text_hash IS NOT NULL;"
        )
        stmts = _normalize_sql_statements(sql)
        assert len(stmts) == 1
        assert "on rulings (case_id, ruling_text_hash)" in stmts[0]


class TestExtractUniqueConstraintsFromSql:
    """Tests for _extract_unique_constraints_from_sql."""

    def test_primary_key_inline(self) -> None:
        sql = "CREATE TABLE foo (\n    id UUID PRIMARY KEY\n);"
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"id"}) in result["foo"]

    def test_primary_key_composite(self) -> None:
        sql = (
            "CREATE TABLE case_judges (\n"
            "    case_id UUID NOT NULL,\n"
            "    judge_id UUID NOT NULL,\n"
            "    PRIMARY KEY (case_id, judge_id)\n"
            ");"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"case_id", "judge_id"}) in result["case_judges"]

    def test_inline_unique_column(self) -> None:
        sql = (
            "CREATE TABLE courts (\n"
            "    id UUID PRIMARY KEY,\n"
            "    court_code TEXT UNIQUE NOT NULL\n"
            ");"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"court_code"}) in result["courts"]
        assert frozenset({"id"}) in result["courts"]

    def test_constraint_unique_inside_create_table(self) -> None:
        sql = (
            "CREATE TABLE judges (\n"
            "    id UUID PRIMARY KEY,\n"
            "    canonical_name TEXT NOT NULL,\n"
            "    court_id UUID NOT NULL,\n"
            "    CONSTRAINT judges_key UNIQUE (canonical_name, court_id)\n"
            ");"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"canonical_name", "court_id"}) in result["judges"]
        assert frozenset({"id"}) in result["judges"]

    def test_anonymous_unique_multi_column(self) -> None:
        sql = (
            "CREATE TABLE cases (\n"
            "    id UUID PRIMARY KEY,\n"
            "    court_id UUID NOT NULL,\n"
            "    case_number TEXT NOT NULL,\n"
            "    UNIQUE (court_id, case_number)\n"
            ");"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"court_id", "case_number"}) in result["cases"]

    def test_alter_table_add_constraint_unique(self) -> None:
        sql = (
            "-- Up Migration\n"
            "ALTER TABLE rulings\n"
            "    ADD CONSTRAINT uq_rulings_document_id UNIQUE (document_id);\n"
            "\n"
            "-- Down Migration\n"
            "ALTER TABLE rulings DROP CONSTRAINT IF EXISTS uq_rulings_document_id;"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"document_id"}) in result["rulings"]

    def test_create_unique_index(self) -> None:
        sql = (
            "-- Up Migration\n"
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_test\n"
            "    ON rulings (case_id, ruling_text_hash)\n"
            "    WHERE ruling_text_hash IS NOT NULL;\n"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"case_id", "ruling_text_hash"}) in result["rulings"]

    def test_schema_prefixed_table(self) -> None:
        sql = (
            "CREATE TABLE staging.captures (\n"
            "    id UUID PRIMARY KEY DEFAULT gen_random_uuid()\n"
            ");"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"id"}) in result["staging.captures"]

    def test_down_migration_ignored(self) -> None:
        sql = (
            "-- Up Migration\n"
            "ALTER TABLE foo ADD CONSTRAINT uq UNIQUE (bar);\n"
            "\n"
            "-- Down Migration\n"
            "ALTER TABLE baz ADD CONSTRAINT uq2 UNIQUE (qux);"
        )
        result = _extract_unique_constraints_from_sql(sql)
        assert "foo" in result
        assert "baz" not in result

    def test_serial_primary_key(self) -> None:
        sql = "CREATE TABLE t (\n    id SERIAL PRIMARY KEY\n);"
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"id"}) in result["t"]

    def test_bigserial_primary_key(self) -> None:
        sql = "CREATE TABLE t (\n    id BIGSERIAL PRIMARY KEY\n);"
        result = _extract_unique_constraints_from_sql(sql)
        assert frozenset({"id"}) in result["t"]

    def test_deduplication(self) -> None:
        """Same constraint from schema.sql and migration should not duplicate."""
        sql = (
            "CREATE TABLE judges (\n"
            "    id UUID PRIMARY KEY,\n"
            "    CONSTRAINT uq UNIQUE (canonical_name, court_id)\n"
            ");\n"
        )
        result = _extract_unique_constraints_from_sql(sql)
        # Should have exactly two constraints: id and the named one
        assert len(result["judges"]) == 2


class TestBuildUniqueConstraints:
    """Tests for build_unique_constraints with synthetic schema files."""

    def test_from_schema_and_migrations(self, tmp_path: Path) -> None:
        """Build constraints from a synthetic schema.sql + migration file."""
        # Create directory structure
        api_dir = tmp_path / "packages" / "api" / "src" / "data-access"
        api_dir.mkdir(parents=True)
        mig_dir = tmp_path / "packages" / "api" / "migrations"
        mig_dir.mkdir(parents=True)

        schema = (
            "CREATE TABLE courts (\n"
            "    id UUID PRIMARY KEY,\n"
            "    court_code TEXT UNIQUE NOT NULL\n"
            ");\n"
            "CREATE TABLE judges (\n"
            "    id UUID PRIMARY KEY,\n"
            "    canonical_name TEXT NOT NULL,\n"
            "    court_id UUID NOT NULL,\n"
            "    CONSTRAINT uq UNIQUE (canonical_name, court_id)\n"
            ");\n"
        )
        (api_dir / "schema.sql").write_text(schema)

        migration = (
            "-- Up Migration\n"
            "ALTER TABLE courts\n"
            "    ADD CONSTRAINT uq_courts_state UNIQUE (state);\n"
            "\n"
            "-- Down Migration\n"
            "ALTER TABLE courts DROP CONSTRAINT IF EXISTS uq_courts_state;\n"
        )
        (mig_dir / "2_add-state-unique.sql").write_text(migration)

        result = build_unique_constraints(tmp_path)

        # courts: id (PK) + court_code (inline UNIQUE) + state (from migration)
        assert frozenset({"id"}) in result["courts"]
        assert frozenset({"court_code"}) in result["courts"]
        assert frozenset({"state"}) in result["courts"]

        # judges: id (PK) + (canonical_name, court_id) from CONSTRAINT UNIQUE
        assert frozenset({"id"}) in result["judges"]
        assert frozenset({"canonical_name", "court_id"}) in result["judges"]

    def test_missing_schema_file(self, tmp_path: Path) -> None:
        """Gracefully handle missing schema.sql."""
        result = build_unique_constraints(tmp_path)
        assert result == {}

    def test_constraint_not_lost_when_removed_from_hardcoded_map(
        self,
        tmp_path: Path,
    ) -> None:
        """Acceptance criterion: a constraint from schema.sql is detected even
        if someone hypothetically removed it from a hardcoded map."""
        api_dir = tmp_path / "packages" / "api" / "src" / "data-access"
        api_dir.mkdir(parents=True)
        mig_dir = tmp_path / "packages" / "api" / "migrations"
        mig_dir.mkdir(parents=True)

        schema = (
            "CREATE TABLE courts (\n"
            "    id UUID PRIMARY KEY,\n"
            "    court_code TEXT UNIQUE NOT NULL\n"
            ");\n"
        )
        (api_dir / "schema.sql").write_text(schema)

        result = build_unique_constraints(tmp_path)
        # Even without any hardcoded map, the constraint is detected
        assert frozenset({"court_code"}) in result["courts"]


class TestUniqueConstraintsCompleteness:
    """Verify the auto-generated UNIQUE_CONSTRAINTS map covers expected tables."""

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
        }
        assert expected.issubset(set(UNIQUE_CONSTRAINTS.keys()))

    def test_parties_has_no_canonical_name_constraint(self) -> None:
        """Verify the constraint that caused #1524 is NOT listed."""
        constraints = UNIQUE_CONSTRAINTS["parties"]
        for c in constraints:
            assert "canonical_name" not in c, (
                "parties should not have a UNIQUE constraint on canonical_name"
            )

    def test_case_parties_requires_role(self) -> None:
        """Verify case_parties constraint includes role column."""
        constraints = UNIQUE_CONSTRAINTS["case_parties"]
        multi_col = [c for c in constraints if len(c) > 1]
        assert len(multi_col) == 1
        assert "role" in multi_col[0], (
            "case_parties unique constraint must include role column"
        )

    def test_rulings_has_document_id_unique(self) -> None:
        """Verify rulings has document_id from migration 3."""
        constraints = UNIQUE_CONSTRAINTS["rulings"]
        assert frozenset({"document_id"}) in constraints

    def test_rulings_has_case_text_hash_unique(self) -> None:
        """Verify rulings has (case_id, ruling_text_hash) from migration 11."""
        constraints = UNIQUE_CONSTRAINTS["rulings"]
        assert frozenset({"case_id", "ruling_text_hash"}) in constraints

    def test_judges_has_canonical_name_court_id(self) -> None:
        """Verify judges has (canonical_name, court_id) from schema.sql."""
        constraints = UNIQUE_CONSTRAINTS["judges"]
        assert frozenset({"canonical_name", "court_id"}) in constraints

    def test_users_has_all_unique_columns(self) -> None:
        """Verify users has email, google_id, and api_key UNIQUE constraints."""
        constraints = UNIQUE_CONSTRAINTS["users"]
        assert frozenset({"email"}) in constraints
        assert frozenset({"google_id"}) in constraints
        assert frozenset({"api_key"}) in constraints

    def test_constraints_are_auto_generated(self) -> None:
        """Verify the map is built from schema files, not hardcoded.

        This is the key acceptance criterion: if we build from a fresh
        repo root, we get the same result as the module-level variable.
        """
        repo_root = Path(__file__).resolve().parent.parent.parent
        fresh = build_unique_constraints(repo_root)
        assert fresh == UNIQUE_CONSTRAINTS
