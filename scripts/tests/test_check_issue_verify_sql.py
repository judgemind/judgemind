"""Tests for scripts/check-issue-verify-sql.py.

Covers AC#4 of issue #4358:
  - clean SQL
  - bad column
  - multi-table JOIN
  - qualified vs unqualified column refs
  - multiple Verify: lines per body
  - non-SQL Verify lines (Python / curl / pytest — should be ignored cleanly)

Plus AC#3: a pytest fixture mirroring #4309's buggy AC produces exit 1
with ``dispatcher.scheduled_skills.schedule`` in stderr.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader (the check script has dashes in the filename, so we have
# to load it via importlib rather than ``import``).
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check-issue-verify-sql.py"
_spec = importlib.util.spec_from_file_location("check_issue_verify_sql", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
check_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mod)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_columns() -> dict[str, set[str]]:
    """Parse the real ``schema.sql`` once per module run."""
    return check_mod.parse_schema(
        _REPO_ROOT / "packages" / "api" / "src" / "data-access" / "schema.sql"
    )


@pytest.fixture
def fake_columns() -> dict[str, set[str]]:
    """A small synthetic column map for unit tests that don't need to
    pull in the full schema."""
    return {
        "dispatcher.scheduled_skills": {
            "name",
            "skill_invocation",
            "trigger_kind",
            "trigger_value",
            "enabled",
            "last_triggered_at",
            "last_triggered_agent_id",
            "notes",
        },
        "dispatcher.agents": {
            "agent_id",
            "issue_number",
            "phase",
            "status",
            "started_at",
            "ended_at",
        },
        "derived.rulings": {
            "id",
            "case_id",
            "court_id",
            "judge_id",
            "document_id",
            "ruling_text",
            "outcome",
            "hearing_date",
        },
        "derived.cases": {
            "id",
            "case_number",
            "case_title",
            "court_id",
            "filed_at",
        },
        "derived.documents": {
            "id",
            "s3_key",
            "status",
            "captured_at",
        },
    }


# ---------------------------------------------------------------------------
# parse_schema — basic structural checks against the real schema.sql.
# ---------------------------------------------------------------------------


class TestParseSchema:
    def test_parse_schema_finds_known_tables(
        self, real_columns: dict[str, set[str]]
    ) -> None:
        """The real schema.sql should produce a non-empty column map
        containing the tables we reference all over the codebase."""
        # Spot-check: tables that have driven the issue authoring rule.
        assert "dispatcher.scheduled_skills" in real_columns
        assert "derived.rulings" in real_columns
        assert "derived.documents" in real_columns

    def test_dispatcher_scheduled_skills_columns(
        self, real_columns: dict[str, set[str]]
    ) -> None:
        cols = real_columns["dispatcher.scheduled_skills"]
        assert "name" in cols
        assert "trigger_kind" in cols
        assert "trigger_value" in cols
        assert "enabled" in cols
        # Sanity: the column the buggy #4309 AC tried to reference does
        # NOT exist on this table.
        assert "schedule" not in cols

    def test_constraint_lines_are_skipped(self, tmp_path: Path) -> None:
        """``CONSTRAINT foo CHECK (...)`` lines must not be misread as
        a column called 'CONSTRAINT'."""
        sql = (
            "CREATE TABLE derived.attorney_aliases (\n"
            "    id uuid DEFAULT gen_random_uuid() NOT NULL,\n"
            "    raw_name text NOT NULL,\n"
            "    confidence double precision,\n"
            "    CONSTRAINT attorney_aliases_confidence_check CHECK (((confidence >= (0)::double precision)))\n"
            ");\n"
        )
        path = tmp_path / "schema.sql"
        path.write_text(sql)
        cols = check_mod.parse_schema(path)
        assert cols["derived.attorney_aliases"] == {"id", "raw_name", "confidence"}
        assert "CONSTRAINT" not in cols["derived.attorney_aliases"]


# ---------------------------------------------------------------------------
# extract_sql_fragments — pulling SQL out of issue body text.
# ---------------------------------------------------------------------------


class TestExtractSqlFragments:
    def test_single_backtick_select(self) -> None:
        body = "Verify: `SELECT id FROM derived.documents`"
        frags = check_mod.extract_sql_fragments(body)
        assert frags == ["SELECT id FROM derived.documents"]

    def test_fenced_code_block_after_verify(self) -> None:
        body = (
            "Verify: query the dispatcher table.\n"
            "```sql\n"
            "SELECT name, trigger_kind FROM dispatcher.scheduled_skills\n"
            "```\n"
        )
        frags = check_mod.extract_sql_fragments(body)
        assert len(frags) == 1
        assert "SELECT name, trigger_kind" in frags[0]

    def test_non_sql_verify_lines_ignored(self) -> None:
        body = (
            "Verify: `pytest -k test_foo`\n"
            "Verify: `curl -s https://example.com`\n"
            "Verify: `grep -n 'pattern' file.py`\n"
            "Verify: `./scripts/foo.sh`\n"
        )
        frags = check_mod.extract_sql_fragments(body)
        assert frags == []

    def test_multiple_verify_lines_per_body(self) -> None:
        body = (
            "- [ ] First criterion.\n"
            "  Verify: `SELECT id FROM derived.documents`\n"
            "- [ ] Second criterion.\n"
            "  Verify: `SELECT name FROM dispatcher.scheduled_skills`\n"
        )
        frags = check_mod.extract_sql_fragments(body)
        assert len(frags) == 2

    def test_explain_recognized_as_sql(self) -> None:
        body = "Verify: `EXPLAIN SELECT id FROM derived.documents`"
        frags = check_mod.extract_sql_fragments(body)
        assert len(frags) == 1
        assert frags[0].startswith("EXPLAIN")

    def test_update_recognized_as_sql(self) -> None:
        body = "Verify: `UPDATE derived.rulings SET outcome = 'granted' WHERE id = 'x'`"
        frags = check_mod.extract_sql_fragments(body)
        assert len(frags) == 1
        assert frags[0].startswith("UPDATE")

    def test_no_verify_lines_returns_empty(self) -> None:
        body = "## Random body text\n\nNothing to validate here.\n"
        frags = check_mod.extract_sql_fragments(body)
        assert frags == []

    def test_lowercase_select_recognized(self) -> None:
        body = "Verify: `select id from derived.documents`"
        frags = check_mod.extract_sql_fragments(body)
        assert len(frags) == 1


# ---------------------------------------------------------------------------
# validate_fragment — the column-validation core.
# ---------------------------------------------------------------------------


class TestValidateFragmentClean:
    def test_clean_select_unqualified(self, fake_columns: dict[str, set[str]]) -> None:
        sql = "SELECT name, trigger_kind, enabled FROM dispatcher.scheduled_skills"
        assert check_mod.validate_fragment(sql, fake_columns) == []

    def test_clean_select_qualified(self, fake_columns: dict[str, set[str]]) -> None:
        sql = "SELECT r.id, r.case_id FROM derived.rulings r"
        assert check_mod.validate_fragment(sql, fake_columns) == []

    def test_clean_select_with_where(self, fake_columns: dict[str, set[str]]) -> None:
        sql = (
            "SELECT name FROM dispatcher.scheduled_skills "
            "WHERE name = 'audit-llm-carry-forward' AND enabled = true"
        )
        assert check_mod.validate_fragment(sql, fake_columns) == []


class TestValidateFragmentBadColumn:
    def test_bad_unqualified_column_in_select(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        sql = "SELECT name, schedule FROM dispatcher.scheduled_skills"
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "dispatcher.scheduled_skills.schedule" in errors[0]

    def test_bad_qualified_column_via_alias(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        sql = "SELECT r.bogus_column FROM derived.rulings r"
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "derived.rulings.bogus_column" in errors[0]

    def test_bad_qualified_column_via_table_name(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        """When no alias is given, the bare table name should resolve too."""
        sql = "SELECT rulings.bogus FROM derived.rulings"
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "derived.rulings.bogus" in errors[0]

    def test_bad_column_in_where_clause(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        sql = "SELECT name FROM dispatcher.scheduled_skills WHERE bogus = 'x'"
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "dispatcher.scheduled_skills.bogus" in errors[0]

    def test_string_literal_does_not_trip(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        """A string literal containing what looks like ``column = value``
        should NOT be parsed as a real predicate."""
        sql = (
            "SELECT name FROM dispatcher.scheduled_skills WHERE name = 'col_x = bogus'"
        )
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert errors == []


class TestValidateFragmentJoin:
    def test_clean_multi_table_join_qualified(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        sql = (
            "SELECT r.case_id, c.case_number "
            "FROM derived.rulings r "
            "JOIN derived.cases c ON c.id = r.case_id"
        )
        assert check_mod.validate_fragment(sql, fake_columns) == []

    def test_bad_alias_on_left_table(self, fake_columns: dict[str, set[str]]) -> None:
        sql = (
            "SELECT r.bogus, c.case_number "
            "FROM derived.rulings r "
            "JOIN derived.cases c ON c.id = r.case_id"
        )
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "derived.rulings.bogus" in errors[0]

    def test_bad_alias_on_right_table(self, fake_columns: dict[str, set[str]]) -> None:
        sql = (
            "SELECT r.case_id, c.bogus "
            "FROM derived.rulings r "
            "JOIN derived.cases c ON c.id = r.case_id"
        )
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "derived.cases.bogus" in errors[0]

    def test_unqualified_columns_skipped_for_multi_table(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        """Unqualified column refs in multi-table JOINs are intentionally
        ambiguous and out of scope per #4358 — should NOT fire."""
        sql = (
            "SELECT id, case_number "
            "FROM derived.rulings r JOIN derived.cases c ON c.id = r.case_id"
        )
        # Even though `case_number` only exists on `derived.cases` and
        # `id` is on both, we don't try to resolve.
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert errors == []


class TestValidateFragmentEdgeCases:
    def test_unknown_schema_skipped(self, fake_columns: dict[str, set[str]]) -> None:
        """References against ``information_schema.*``,
        ``pg_catalog.*``, etc. should not trip the lint."""
        sql = "SELECT table_name, column_name FROM information_schema.columns"
        assert check_mod.validate_fragment(sql, fake_columns) == []

    def test_no_known_from_returns_empty(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        """A fragment with no schema-qualified FROM (e.g. SELECT 1) is
        a no-op."""
        sql = "SELECT 1"
        assert check_mod.validate_fragment(sql, fake_columns) == []

    def test_unknown_qualified_table_reported(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        sql = "SELECT id FROM derived.nonexistent_table"
        errors = check_mod.validate_fragment(sql, fake_columns)
        assert len(errors) == 1
        assert "derived.nonexistent_table" in errors[0]


# ---------------------------------------------------------------------------
# check_body — full pipeline.
# ---------------------------------------------------------------------------


class TestCheckBody:
    def test_clean_body_returns_no_errors(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        body = (
            "## Acceptance criteria\n"
            "- [ ] First.\n"
            "  Verify: `SELECT name, trigger_kind FROM dispatcher.scheduled_skills`\n"
        )
        assert check_mod.check_body(body, fake_columns) == []

    def test_buggy_4309_ac_flagged(self, fake_columns: dict[str, set[str]]) -> None:
        """AC#3: a body containing the original buggy #4309 AC (which
        referenced ``dispatcher.scheduled_skills.schedule``) must be
        flagged with ``schedule`` named in the diagnostic."""
        body = (
            "Verify: `SELECT name, schedule, enabled, last_triggered_at "
            "FROM dispatcher.scheduled_skills "
            "WHERE name = 'audit-llm-carry-forward'`\n"
        )
        errors = check_mod.check_body(body, fake_columns)
        assert len(errors) == 1
        assert "dispatcher.scheduled_skills.schedule" in errors[0]

    def test_multiple_verify_lines_each_validated(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        body = (
            "- [ ] First.\n"
            "  Verify: `SELECT name FROM dispatcher.scheduled_skills`\n"
            "- [ ] Second (buggy).\n"
            "  Verify: `SELECT bogus FROM dispatcher.scheduled_skills`\n"
            "- [ ] Third.\n"
            "  Verify: `SELECT id FROM derived.rulings`\n"
        )
        errors = check_mod.check_body(body, fake_columns)
        assert len(errors) == 1
        assert "dispatcher.scheduled_skills.bogus" in errors[0]

    def test_mixed_sql_and_non_sql_verify_lines(
        self, fake_columns: dict[str, set[str]]
    ) -> None:
        """Non-SQL Verify lines should be silently dropped, leaving the
        SQL ones to be validated."""
        body = (
            "- [ ] First.\n"
            "  Verify: `pytest -k test_foo`\n"
            "- [ ] Second.\n"
            "  Verify: `SELECT name FROM dispatcher.scheduled_skills`\n"
            "- [ ] Third.\n"
            "  Verify: `curl -s https://dev.api.judgemind.org/graphql`\n"
        )
        assert check_mod.check_body(body, fake_columns) == []


# ---------------------------------------------------------------------------
# main — CLI integration (only the body-file path; the gh path is
# exercised separately and gated on gh availability).
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_main_clean_body_returns_zero(
        self,
        tmp_path: Path,
        real_columns: dict[str, set[str]],  # noqa: ARG002
    ) -> None:
        body = "Verify: `SELECT id FROM derived.documents`\n"
        body_path = tmp_path / "body.txt"
        body_path.write_text(body)
        rc = check_mod.main(["--body-file", str(body_path)])
        assert rc == 0

    def test_main_buggy_body_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = "Verify: `SELECT name, schedule FROM dispatcher.scheduled_skills`\n"
        body_path = tmp_path / "body.txt"
        body_path.write_text(body)
        rc = check_mod.main(["--body-file", str(body_path)])
        captured = capsys.readouterr()
        assert rc == 1
        # Diagnostic must name the offending column on the offending
        # table (AC#3).
        assert "dispatcher.scheduled_skills.schedule" in captured.err
        # Per the Fix-block contract (docs/agent/code-standards.md
        # §"Hygiene-check guards: Fix-block contract"), the error
        # path must emit a labelled Fix: block pointing at the
        # canonical remediation (schema.sql + dev-db-query.sh).
        assert "Fix:" in captured.err
        assert "schema.sql" in captured.err

    def test_main_missing_schema_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_path = tmp_path / "body.txt"
        body_path.write_text("Verify: `SELECT 1`\n")
        rc = check_mod.main(
            [
                "--body-file",
                str(body_path),
                "--schema-sql",
                str(tmp_path / "does-not-exist.sql"),
            ]
        )
        captured = capsys.readouterr()
        assert rc == 2
        assert "schema.sql not found" in captured.err

    def test_main_missing_body_file_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = check_mod.main(["--body-file", str(tmp_path / "nonexistent.txt")])
        captured = capsys.readouterr()
        assert rc == 2
        assert "failed to read body file" in captured.err

    def test_main_requires_input_source(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --issue or --body-file, argparse should error."""
        with pytest.raises(SystemExit) as excinfo:
            check_mod.main([])
        # argparse exits 2 on missing required arg.
        assert excinfo.value.code == 2
