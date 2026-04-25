"""Schema test for the ``dispatcher.scheduled_skills`` table (issue #3374).

Asserts the migration file declares the table + columns the daemon's
``_scheduled_skills_tick`` reads. We don't have a live Postgres in unit
tests; we parse the migration file's CREATE TABLE statement and assert
column presence + key types. The collision/files CI guards make sure
the migration filename + numbering is fine; this test makes sure the
contents match the daemon code's expectations.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "48_dispatcher-scheduled-skills.sql"
)


def _migration_text() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), (
        f"expected migration file at {MIGRATION_PATH}; issue #3374 ships migration 48"
    )


def test_create_table_present() -> None:
    text = _migration_text()
    assert "CREATE TABLE dispatcher.scheduled_skills" in text


def test_required_columns_present() -> None:
    text = _migration_text()
    # The daemon reads these columns by name in
    # ``_scheduled_skills_tick`` — they must exist.
    expected_columns = [
        "name",
        "skill_invocation",
        "trigger_kind",
        "trigger_value",
        "enabled",
        "last_triggered_at",
        "last_triggered_agent_id",
        "notes",
    ]
    for col in expected_columns:
        # Allow whitespace + type after the column name. Use a relaxed
        # regex that matches the column declaration anywhere in the
        # CREATE TABLE block.
        pattern = rf"\b{re.escape(col)}\s+[A-Z]"
        assert re.search(pattern, text), f"column {col!r} not declared in migration"


def test_primary_key_on_name() -> None:
    text = _migration_text()
    # The column-level PRIMARY KEY declaration: ``name TEXT PRIMARY KEY``.
    assert re.search(r"name\s+TEXT\s+PRIMARY KEY", text), (
        "name column must be PRIMARY KEY (daemon UPSERTs by name)"
    )


def test_seed_rows_for_existing_config() -> None:
    text = _migration_text()
    # Migration 47 inserts seed rows for audit + spotcheck pulled from
    # the existing dispatcher.config keys.
    assert "INSERT INTO dispatcher.scheduled_skills" in text
    assert "'audit'" in text
    assert "'spotcheck'" in text
    assert "every_n_merges" in text
    assert "'cron'" in text
    assert "idle_audit_every_n_prs" in text
    assert "idle_spotcheck_cron" in text


def test_down_migration_drops_table() -> None:
    text = _migration_text()
    assert "DROP TABLE IF EXISTS dispatcher.scheduled_skills" in text


def test_does_not_drop_legacy_config_keys() -> None:
    """Issue body: keep config keys for one verified deploy cycle."""
    text = _migration_text()
    # The migration must NOT delete the legacy config keys yet — that
    # cleanup belongs in a follow-up after one verified deploy cycle.
    assert "DELETE FROM dispatcher.config" not in text
    assert "delete from dispatcher.config" not in text.lower()


# ---------------------------------------------------------------------------
# Migration 49 — dispatcher-daily-report skill row (issue #3375)
# ---------------------------------------------------------------------------

MIGRATION_49_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "49_dispatcher-daily-report-skill.sql"
)


def _migration_49_text() -> str:
    return MIGRATION_49_PATH.read_text(encoding="utf-8")


def test_daily_report_migration_file_exists() -> None:
    assert MIGRATION_49_PATH.is_file(), (
        f"expected migration file at {MIGRATION_49_PATH}; issue #3375 ships migration 49"
    )


def test_daily_report_row_seeded() -> None:
    """Migration 49 must INSERT the dispatcher-daily-report row."""
    text = _migration_49_text()
    assert "INSERT INTO dispatcher.scheduled_skills" in text
    assert "'dispatcher-daily-report'" in text
    assert "'/dispatcher-daily-report'" in text
    assert "'cron'" in text
    assert "'0 12 * * *'" in text


def test_daily_report_row_idempotent() -> None:
    """Migration 49 INSERT must use ON CONFLICT (name) DO NOTHING."""
    text = _migration_49_text()
    assert "ON CONFLICT (name) DO NOTHING" in text


def test_daily_report_down_migration() -> None:
    """Migration 49 must have a Down Migration block that deletes the row."""
    text = _migration_49_text()
    assert "-- Down Migration" in text
    assert "DELETE FROM dispatcher.scheduled_skills" in text
    assert "'dispatcher-daily-report'" in text
