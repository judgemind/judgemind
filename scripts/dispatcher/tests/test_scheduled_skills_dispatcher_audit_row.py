"""Migration test for migration 53 — dispatcher-audit scheduled skill row (issue #2865).

Asserts that the migration file inserts a row for the dispatcher-audit skill
with the expected name, skill_invocation, trigger_kind, trigger_value, and
enabled=true. Models the pattern from test_scheduled_skills_schema.py.
"""

from __future__ import annotations

from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "53_dispatcher-audit-skill.sql"
)


def _migration_text() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), (
        f"expected migration file at {MIGRATION_PATH}; issue #2865 ships migration 53"
    )


def test_dispatcher_audit_row_seeded() -> None:
    """Migration 53 must INSERT the dispatcher-audit row."""
    text = _migration_text()
    assert "INSERT INTO dispatcher.scheduled_skills" in text
    assert "'dispatcher-audit'" in text
    assert "'/dispatcher-audit'" in text
    assert "'cron'" in text
    assert "'0 */6 * * *'" in text


def test_dispatcher_audit_row_enabled() -> None:
    """Migration 53 INSERT row must have enabled=true."""
    text = _migration_text()
    assert "true" in text, "enabled column must be set to true"


def test_dispatcher_audit_row_idempotent() -> None:
    """Migration 53 INSERT must use ON CONFLICT (name) DO NOTHING."""
    text = _migration_text()
    assert "ON CONFLICT (name) DO NOTHING" in text


def test_dispatcher_audit_down_migration() -> None:
    """Migration 53 must have a Down Migration block that deletes the row."""
    text = _migration_text()
    assert "-- Down Migration" in text
    assert "DELETE FROM dispatcher.scheduled_skills" in text
    assert "'dispatcher-audit'" in text
