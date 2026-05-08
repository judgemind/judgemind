"""Migration test for migration 63 — audit-llm-carry-forward scheduled skill row (#4309).

Asserts that the migration file inserts a row for the audit-llm-carry-forward
skill with the expected name, skill_invocation, trigger_kind, trigger_value,
and enabled=true. Models the pattern from
test_scheduled_skills_dispatcher_audit_row.py.
"""

from __future__ import annotations

from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "63_dispatcher-llm-carry-forward-skill.sql"
)


def _migration_text() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), (
        f"expected migration file at {MIGRATION_PATH}; issue #4309 ships migration 63"
    )


def test_llm_carry_forward_row_seeded() -> None:
    """Migration 63 must INSERT the audit-llm-carry-forward row."""
    text = _migration_text()
    assert "INSERT INTO dispatcher.scheduled_skills" in text
    assert "'audit-llm-carry-forward'" in text
    assert "'/audit-llm-carry-forward'" in text
    assert "'cron'" in text
    # Sunday 06:00 UTC weekly cadence.
    assert "'0 6 * * 0'" in text


def test_llm_carry_forward_row_enabled() -> None:
    """Migration 63 INSERT row must have enabled=true."""
    text = _migration_text()
    assert "true" in text, "enabled column must be set to true"


def test_llm_carry_forward_row_idempotent() -> None:
    """Migration 63 INSERT must use ON CONFLICT (name) DO NOTHING."""
    text = _migration_text()
    assert "ON CONFLICT (name) DO NOTHING" in text


def test_llm_carry_forward_down_migration() -> None:
    """Migration 63 must have a Down Migration block that deletes the row."""
    text = _migration_text()
    assert "-- Down Migration" in text
    assert "DELETE FROM dispatcher.scheduled_skills" in text
    assert "'audit-llm-carry-forward'" in text


def test_llm_carry_forward_references_issue() -> None:
    """The notes / comments should reference issue #4309 for traceability."""
    text = _migration_text()
    assert "#4309" in text
