"""Migration test for migration 64 — scheduled_skills.last_run_state column (#4318).

Asserts that the migration adds a ``last_run_state JSONB`` column to
``dispatcher.scheduled_skills`` with the documented semantics so the
``/audit-llm-carry-forward`` probe (#4309) can carry forward per-county
totals across ECS task restarts.
"""

from __future__ import annotations

from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "64_dispatcher-scheduled-skills-last-run-state.sql"
)


def _migration_text() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), (
        f"expected migration file at {MIGRATION_PATH}; issue #4318 ships migration 64"
    )


def test_adds_last_run_state_column() -> None:
    """The migration must ALTER scheduled_skills to ADD COLUMN last_run_state JSONB."""
    text = _migration_text()
    assert "ALTER TABLE dispatcher.scheduled_skills" in text
    assert "ADD COLUMN last_run_state JSONB" in text


def test_column_has_no_default_or_not_null() -> None:
    """Existing rows stay NULL until the next fire writes a value (per migration design)."""
    text = _migration_text()
    # No DEFAULT clause on the new column.
    assert "ADD COLUMN last_run_state JSONB DEFAULT" not in text
    # No NOT NULL constraint.
    assert "ADD COLUMN last_run_state JSONB NOT NULL" not in text


def test_column_comment_documents_skill_private_semantics() -> None:
    """COMMENT ON COLUMN must mention the JSONB shape and #4318 traceability."""
    text = _migration_text()
    assert "COMMENT ON COLUMN dispatcher.scheduled_skills.last_run_state" in text
    assert "#4318" in text


def test_down_migration_drops_column() -> None:
    """The Down Migration block must drop the column idempotently."""
    text = _migration_text()
    assert "-- Down Migration" in text
    assert "DROP COLUMN IF EXISTS last_run_state" in text


def test_references_audit_llm_carry_forward_skill() -> None:
    """Migration body should reference the canonical consumer skill (#4309)."""
    text = _migration_text()
    assert "audit-llm-carry-forward" in text
    assert "#4309" in text
