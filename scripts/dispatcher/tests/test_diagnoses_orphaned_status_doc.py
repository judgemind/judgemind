"""Issue #3392 — schema-level assertions for ``dispatcher.diagnoses.status``
column comment.

Migration 56 adds COMMENT ON COLUMN for the status column, documenting all
four lifecycle values: pending | completed | failed | orphaned.

The orphaned value was introduced in PR #3388 (issue #3383) so that
boot-reap of a previous daemon's pending rows does not trip the 24h
fallback-rate circuit breaker. No column comment existed before this
migration — verified by grep returning zero hits on
``COMMENT ON COLUMN dispatcher.diagnoses.status`` in the pre-PR tree.

These tests parse the SQL files directly rather than spinning up a
Postgres instance. The integration-level smoke test happens via
``scripts/regenerate_schema.sh`` against a fresh container.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_56 = (
    REPO_ROOT
    / "packages/api/migrations/56_dispatcher-diagnoses-orphaned-status-doc.sql"
)
SCHEMA_SQL = REPO_ROOT / "packages/api/src/data-access/schema.sql"


# --------------------------------------------------------------------------
# Migration file
# --------------------------------------------------------------------------


class TestMigration56Exists:
    def test_file_exists(self) -> None:
        assert MIGRATION_56.is_file(), f"missing: {MIGRATION_56}"

    def test_has_up_and_down_sections(self) -> None:
        text = MIGRATION_56.read_text()
        assert "-- Up Migration" in text
        assert "-- Down Migration" in text


class TestMigration56UpSection:
    def test_up_comments_status_column(self) -> None:
        text = MIGRATION_56.read_text()
        up = text.split("-- Down Migration")[0]
        assert "COMMENT ON COLUMN dispatcher.diagnoses.status" in up

    def test_up_mentions_all_four_values(self) -> None:
        text = MIGRATION_56.read_text()
        up = text.split("-- Down Migration")[0]
        for value in ("pending", "completed", "failed", "orphaned"):
            assert value in up, f"Up section must mention status value '{value}'"


class TestMigration56DownSection:
    def test_down_restores_prior_comment(self) -> None:
        text = MIGRATION_56.read_text()
        down = text.split("-- Down Migration")[1]
        # Prior state was no comment — restored by setting to NULL.
        assert "COMMENT ON COLUMN dispatcher.diagnoses.status IS NULL" in down


# --------------------------------------------------------------------------
# schema.sql snapshot
# --------------------------------------------------------------------------


class TestSchemaSqlSnapshot:
    def test_schema_includes_status_comment(self) -> None:
        text = SCHEMA_SQL.read_text()
        assert "COMMENT ON COLUMN dispatcher.diagnoses.status" in text

    def test_schema_status_comment_mentions_orphaned(self) -> None:
        text = SCHEMA_SQL.read_text()
        # Locate the status comment line and confirm it names orphaned.
        lines = text.splitlines()
        status_comment_lines = [
            ln for ln in lines if "COMMENT ON COLUMN dispatcher.diagnoses.status" in ln
        ]
        assert status_comment_lines, (
            "schema.sql must contain COMMENT ON COLUMN dispatcher.diagnoses.status"
        )
        combined = " ".join(status_comment_lines)
        assert "orphaned" in combined, (
            "status comment in schema.sql must mention the 'orphaned' value"
        )
