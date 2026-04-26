"""Tests for check-nullable-column-reads.py — nullable-column read-site
detection against synthetic Python fixtures."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Import the module despite its dash-containing filename.
# Register it in sys.modules BEFORE exec_module so `@dataclass` can find
# its own module by name (it calls sys.modules[cls.__module__]).
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "check-nullable-column-reads.py"
_spec = importlib.util.spec_from_file_location(
    "check_nullable_column_reads", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_nullable_column_reads"] = mod
_spec.loader.exec_module(mod)

parse_dropped_not_null = mod.parse_dropped_not_null
audit_column_reads = mod.audit_column_reads
_column_has_nullable_ok = mod._column_has_nullable_ok


# ---------------------------------------------------------------------------
# parse_dropped_not_null
# ---------------------------------------------------------------------------


class TestParseDroppedNotNull:
    def test_extracts_from_migration_49(self) -> None:
        """Inline SQL mirroring migration #49 (dispatcher.agents.issue_number)."""
        sql = (
            "ALTER TABLE dispatcher.agents\n"
            "    ALTER COLUMN issue_number DROP NOT NULL;\n"
        )
        result = parse_dropped_not_null(sql)
        assert result == [("agents", "issue_number")]

    def test_extracts_from_migration_16(self) -> None:
        """Inline SQL mirroring migration #16 (derived.rulings.hearing_date)."""
        sql = "ALTER TABLE derived.rulings ALTER COLUMN hearing_date DROP NOT NULL;\n"
        result = parse_dropped_not_null(sql)
        assert result == [("rulings", "hearing_date")]

    def test_ignores_add_column(self) -> None:
        """ADD COLUMN does not make anything nullable; must not be extracted."""
        sql = "ALTER TABLE foo ADD COLUMN bar TEXT;\n"
        result = parse_dropped_not_null(sql)
        assert result == []

    def test_ignores_alter_type(self) -> None:
        """ALTER COLUMN ... TYPE does not drop NOT NULL."""
        sql = "ALTER TABLE foo ALTER COLUMN bar TYPE TEXT;\n"
        result = parse_dropped_not_null(sql)
        assert result == []

    def test_ignores_set_not_null(self) -> None:
        """SET NOT NULL (the opposite direction) must not be extracted."""
        sql = "ALTER TABLE foo ALTER COLUMN bar SET NOT NULL;\n"
        result = parse_dropped_not_null(sql)
        assert result == []

    def test_extracts_multiple_in_one_file(self) -> None:
        """Multiple DROP NOT NULL statements in one migration are all captured."""
        sql = (
            "ALTER TABLE derived.rulings ALTER COLUMN hearing_date DROP NOT NULL;\n"
            "ALTER TABLE dispatcher.agents ALTER COLUMN issue_number DROP NOT NULL;\n"
        )
        result = parse_dropped_not_null(sql)
        assert result == [("rulings", "hearing_date"), ("agents", "issue_number")]


# ---------------------------------------------------------------------------
# audit_column_reads
# ---------------------------------------------------------------------------


class TestAuditColumnReads:
    """Uses tmp_path to create synthetic .py fixture files inside a fake
    'scripts/' subdirectory so audit_column_reads walks them."""

    def _make_scripts_file(self, tmp_path: Path, name: str, content: str) -> Path:
        """Write *content* to tmp_path/scripts/<name> and return the path."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        target = scripts_dir / name
        target.write_text(content, encoding="utf-8")
        return target

    def test_flags_unguarded_select(self, tmp_path: Path) -> None:
        """A SELECT referencing the column without IS NOT NULL is a violation."""
        # Mirrors pre-#3394 daemon code that did: row["issue_number"]
        code = (
            'query = "SELECT issue_number FROM dispatcher.agents"\n'
            'issue_num = row["issue_number"]\n'
        )
        self._make_scripts_file(tmp_path, "daemon.py", code)
        violations = audit_column_reads(tmp_path, [("agents", "issue_number")])
        assert len(violations) == 1
        assert violations[0].column == "issue_number"

    def test_passes_when_is_not_null_present(self, tmp_path: Path) -> None:
        """A SELECT that already contains IS NOT NULL is clean — no violation."""
        # Mirrors post-#3394 daemon code with the guard in place.
        code = (
            "query = (\n"
            '    "SELECT issue_number FROM dispatcher.agents"\n'
            '    " WHERE issue_number IS NOT NULL"\n'
            ")\n"
            'issue_num = row["issue_number"]\n'
        )
        self._make_scripts_file(tmp_path, "daemon_fixed.py", code)
        violations = audit_column_reads(tmp_path, [("agents", "issue_number")])
        assert violations == []

    def test_passes_with_nullable_ok_annotation(self, tmp_path: Path) -> None:
        """A file carrying # nullable-ok: <column>: <reason> is explicitly ack'd — no violation."""
        code = (
            "# nullable-ok: issue_number: is None for scheduled-skill agents\n"
            'query = "SELECT issue_number FROM dispatcher.agents"\n'
            'issue_num = row["issue_number"]\n'
        )
        self._make_scripts_file(tmp_path, "daemon_acked.py", code)
        violations = audit_column_reads(tmp_path, [("agents", "issue_number")])
        assert violations == []

    def test_ignores_files_not_referencing_column(self, tmp_path: Path) -> None:
        """Files that never mention the column are not flagged."""
        code = (
            'query = "SELECT id, title FROM cases WHERE county = %s"\n'
            'case_id = row["id"]\n'
        )
        self._make_scripts_file(tmp_path, "cases.py", code)
        violations = audit_column_reads(tmp_path, [("agents", "issue_number")])
        assert violations == []

    def test_empty_columns_list_returns_no_violations(self, tmp_path: Path) -> None:
        """When no columns are passed, audit returns empty immediately."""
        code = 'row["issue_number"]\n'
        self._make_scripts_file(tmp_path, "daemon.py", code)
        violations = audit_column_reads(tmp_path, [])
        assert violations == []

    def test_no_select_shaped_reference_not_flagged(self, tmp_path: Path) -> None:
        """Mentioning the column name in a comment or docstring is not flagged."""
        code = (
            "# issue_number is the GitHub issue identifier\n"
            'COLUMN_NAME = "issue_number"  # constant, no SELECT\n'
        )
        self._make_scripts_file(tmp_path, "constants.py", code)
        violations = audit_column_reads(tmp_path, [("agents", "issue_number")])
        assert violations == []


# ---------------------------------------------------------------------------
# parse_dropped_not_null — multi-action ALTER TABLE (AC1)
# ---------------------------------------------------------------------------


class TestParseDroppedNotNullMultiAction:
    def test_extracts_from_multi_action_alter(self) -> None:
        """Comma-separated actions: TYPE change + DROP NOT NULL yields one pair."""
        sql = (
            "ALTER TABLE foo ALTER COLUMN a TYPE TEXT, ALTER COLUMN b DROP NOT NULL;\n"
        )
        result = parse_dropped_not_null(sql)
        assert result == [("foo", "b")]

    def test_extracts_from_multi_action_two_drops(self) -> None:
        """Two DROP NOT NULL actions in one statement both yield pairs."""
        sql = (
            "ALTER TABLE foo "
            "ALTER COLUMN a DROP NOT NULL, "
            "ALTER COLUMN b DROP NOT NULL;\n"
        )
        result = parse_dropped_not_null(sql)
        assert result == [("foo", "a"), ("foo", "b")]


# ---------------------------------------------------------------------------
# _column_has_nullable_ok — per-column annotation (AC3)
# ---------------------------------------------------------------------------


class TestColumnHasNullableOk:
    def test_nullable_ok_requires_column_name_match(self, tmp_path: Path) -> None:
        """Annotation for column 'foo' does NOT suppress a violation on 'bar';
        the same annotation DOES suppress a violation on 'foo'."""
        # Write a file that reads both 'foo' and 'bar' in a SELECT context,
        # annotated only for 'foo'.
        code = (
            "# nullable-ok: foo: handled upstream\n"
            'query_foo = "SELECT foo FROM t"\n'
            'query_bar = "SELECT bar FROM t"\n'
            'v_foo = row["foo"]\n'
            'v_bar = row["bar"]\n'
        )
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        (scripts_dir / "mixed.py").write_text(code, encoding="utf-8")

        violations_foo = audit_column_reads(tmp_path, [("t", "foo")])
        violations_bar = audit_column_reads(tmp_path, [("t", "bar")])

        # 'foo' is annotated — no violation.
        assert violations_foo == [], (
            f"Expected no violation for 'foo', got {violations_foo}"
        )
        # 'bar' is NOT annotated — must be flagged.
        assert len(violations_bar) == 1
        assert violations_bar[0].column == "bar"
