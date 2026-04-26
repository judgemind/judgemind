"""Tests for check-nullable-column-reads.py — nullable-column read-site
detection against synthetic Python fixtures."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
find_changed_migrations = mod.find_changed_migrations
main = mod.main


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


# ---------------------------------------------------------------------------
# main() — CLI entry point (AC1)
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for the main() CLI entry point.

    All tests use --repo-root pointing to tmp_path so the audit walk stays
    confined to a controlled fixture directory.
    """

    def _write_sql(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def _write_scripts_file(self, tmp_path: Path, name: str, content: str) -> Path:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        p = scripts_dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_main_no_drop_not_null_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Migration with only ADD COLUMN — no DROP NOT NULL — exits 0."""
        sql_file = self._write_sql(
            tmp_path, "add_only.sql", "ALTER TABLE foo ADD COLUMN bar TEXT;\n"
        )
        with patch(
            "sys.argv",
            [
                "check-nullable-column-reads",
                "--repo-root",
                str(tmp_path),
                "--migration",
                str(sql_file),
            ],
        ):
            result = main()
        assert result == 0
        out = capsys.readouterr().out
        assert "no DROP NOT NULL statements" in out

    def test_main_no_violations_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Migration with DROP NOT NULL but all reads are guarded — exits 0."""
        sql_file = self._write_sql(
            tmp_path,
            "drop_nn.sql",
            "ALTER TABLE t ALTER COLUMN foo DROP NOT NULL;\n",
        )
        self._write_scripts_file(
            tmp_path,
            "clean.py",
            'query = "SELECT foo FROM t WHERE foo IS NOT NULL"\nv = row["foo"]\n',
        )
        with patch(
            "sys.argv",
            [
                "check-nullable-column-reads",
                "--repo-root",
                str(tmp_path),
                "--migration",
                str(sql_file),
            ],
        ):
            result = main()
        assert result == 0
        out = capsys.readouterr().out
        assert "no unguarded read sites" in out

    def test_main_violations_return_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Migration with DROP NOT NULL and unguarded read — exits 1."""
        sql_file = self._write_sql(
            tmp_path,
            "drop_nn.sql",
            "ALTER TABLE t ALTER COLUMN foo DROP NOT NULL;\n",
        )
        self._write_scripts_file(
            tmp_path,
            "dirty.py",
            'query = "SELECT foo FROM t"\nv = row["foo"]\n',
        )
        with patch(
            "sys.argv",
            [
                "check-nullable-column-reads",
                "--repo-root",
                str(tmp_path),
                "--migration",
                str(sql_file),
            ],
        ):
            result = main()
        assert result == 1
        err = capsys.readouterr().err
        assert "FAIL" in err
        assert "unguarded reads" in err

    def test_main_no_migrations_changed_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No migration files found via git-diff path — exits 0 immediately."""
        with patch(
            "sys.argv", ["check-nullable-column-reads", "--repo-root", str(tmp_path)]
        ):
            with patch(
                "check_nullable_column_reads.find_changed_migrations", return_value=[]
            ):
                result = main()
        assert result == 0
        out = capsys.readouterr().out
        assert "no migration files touched" in out


# ---------------------------------------------------------------------------
# find_changed_migrations() — git subprocess wrapper (AC2)
# ---------------------------------------------------------------------------


class TestFindChangedMigrations:
    """Tests for find_changed_migrations using real git repos where possible."""

    def _git(self, *args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def test_real_git_diff_returns_modified_migration(self, tmp_path: Path) -> None:
        """Real git init: a committed SQL migration shows up in the diff."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@example.com", cwd=tmp_path)
        self._git("config", "user.name", "Test User", cwd=tmp_path)
        self._git("checkout", "-b", "main", cwd=tmp_path)

        migrations_dir = tmp_path / "packages" / "api" / "migrations"
        migrations_dir.mkdir(parents=True)
        sql_file = migrations_dir / "49_x.sql"
        sql_file.write_text("-- initial\n", encoding="utf-8")

        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)

        self._git("checkout", "-b", "feature", cwd=tmp_path)
        sql_file.write_text("-- modified\n", encoding="utf-8")
        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "modify migration", cwd=tmp_path)

        result = find_changed_migrations("main", tmp_path)
        assert len(result) == 1
        assert result[0] == sql_file

    def test_subprocess_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        """Non-zero git exit raises RuntimeError with 'git diff failed'."""
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: bad revision"
        )
        with patch("subprocess.run", return_value=fake_result):
            with pytest.raises(RuntimeError, match="git diff failed"):
                find_changed_migrations("main", tmp_path)

    def test_filters_to_sql_files_only(self, tmp_path: Path) -> None:
        """Non-.sql files in the diff are excluded from the result."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@example.com", cwd=tmp_path)
        self._git("config", "user.name", "Test User", cwd=tmp_path)
        self._git("checkout", "-b", "main", cwd=tmp_path)

        migrations_dir = tmp_path / "packages" / "api" / "migrations"
        migrations_dir.mkdir(parents=True)
        sql_file = migrations_dir / "50_y.sql"
        sql_file.write_text("-- initial\n", encoding="utf-8")
        readme = migrations_dir / "README.md"
        readme.write_text("# migrations\n", encoding="utf-8")

        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)

        self._git("checkout", "-b", "feature", cwd=tmp_path)
        sql_file.write_text("-- modified\n", encoding="utf-8")
        readme.write_text("# migrations updated\n", encoding="utf-8")
        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "modify both", cwd=tmp_path)

        result = find_changed_migrations("main", tmp_path)
        assert all(p.suffix == ".sql" for p in result)
        assert sql_file in result
        assert readme not in result
