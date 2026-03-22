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


class TestUniqueConstraintsCompleteness:
    """Verify the UNIQUE_CONSTRAINTS map covers expected tables."""

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
