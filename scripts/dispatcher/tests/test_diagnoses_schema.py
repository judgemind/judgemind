"""Issue #3366 — schema-level assertions for the new ``dispatcher.diagnoses``
columns.

Two columns added in migration 46:

* ``actions_taken`` JSONB — append-only audit log written by the
  empowered diagnoser. Schema is the array-of-{ts, type, ...} shape
  documented in ``.claude/skills/diagnose-failure/SKILL.md`` §Audit
  trail. Daemon never reads the column.
* ``next_directive`` TEXT — 3-state daemon-consumed directive
  (``respawn_at=<phase>`` | ``terminal`` | NULL).

These tests parse the SQL files directly rather than spinning up a
Postgres — they assert the migration's CREATE / ALTER syntax + the
schema.sql snapshot reflects the new columns. The integration-test
job (``scripts/regenerate_schema.sh`` against a fresh container) is
the pg-level smoke test.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_46 = (
    REPO_ROOT
    / "packages/api/migrations/46_dispatcher-diagnoses-actions-and-directive.sql"
)
SCHEMA_SQL = REPO_ROOT / "packages/api/src/data-access/schema.sql"


# --------------------------------------------------------------------------
# Migration file
# --------------------------------------------------------------------------


class TestMigration46Exists:
    def test_file_exists(self) -> None:
        assert MIGRATION_46.is_file(), f"missing: {MIGRATION_46}"

    def test_has_up_and_down_sections(self) -> None:
        text = MIGRATION_46.read_text()
        assert "-- Up Migration" in text
        assert "-- Down Migration" in text

    def test_up_adds_actions_taken_jsonb(self) -> None:
        text = MIGRATION_46.read_text()
        # ALTER TABLE dispatcher.diagnoses ... ADD COLUMN actions_taken JSONB
        # split into Up + Down — confirm the up section has the ADD.
        up = text.split("-- Down Migration")[0]
        assert re.search(r"ADD COLUMN actions_taken\s+JSONB", up, re.IGNORECASE), (
            "Up section must add actions_taken JSONB column"
        )

    def test_up_adds_next_directive_text(self) -> None:
        text = MIGRATION_46.read_text()
        up = text.split("-- Down Migration")[0]
        assert re.search(r"ADD COLUMN next_directive\s+TEXT", up, re.IGNORECASE), (
            "Up section must add next_directive TEXT column"
        )

    def test_down_drops_both_columns(self) -> None:
        text = MIGRATION_46.read_text()
        down = text.split("-- Down Migration")[1]
        assert re.search(r"DROP COLUMN IF EXISTS next_directive", down, re.IGNORECASE)
        assert re.search(r"DROP COLUMN IF EXISTS actions_taken", down, re.IGNORECASE)

    def test_includes_column_comments(self) -> None:
        text = MIGRATION_46.read_text()
        assert "COMMENT ON COLUMN dispatcher.diagnoses.actions_taken" in text
        assert "COMMENT ON COLUMN dispatcher.diagnoses.next_directive" in text


# --------------------------------------------------------------------------
# schema.sql snapshot
# --------------------------------------------------------------------------


class TestSchemaSqlSnapshot:
    def test_diagnoses_table_includes_new_columns(self) -> None:
        text = SCHEMA_SQL.read_text()
        # Find the CREATE TABLE dispatcher.diagnoses block — body up to the
        # closing );
        m = re.search(
            r"CREATE TABLE dispatcher\.diagnoses\s*\((.*?)\);",
            text,
            re.DOTALL,
        )
        assert m, "missing CREATE TABLE dispatcher.diagnoses in schema.sql"
        body = m.group(1)
        assert re.search(r"\bactions_taken\s+jsonb\b", body, re.IGNORECASE), (
            "schema.sql must include actions_taken jsonb column"
        )
        assert re.search(r"\bnext_directive\s+text\b", body, re.IGNORECASE), (
            "schema.sql must include next_directive text column"
        )

    def test_schema_includes_column_comments(self) -> None:
        text = SCHEMA_SQL.read_text()
        assert "COMMENT ON COLUMN dispatcher.diagnoses.actions_taken" in text
        assert "COMMENT ON COLUMN dispatcher.diagnoses.next_directive" in text


# --------------------------------------------------------------------------
# Migration ordering / numbering
# --------------------------------------------------------------------------


class TestMigrationNumbering:
    def test_no_collision_with_existing(self) -> None:
        migrations_dir = REPO_ROOT / "packages/api/migrations"
        files = sorted(migrations_dir.glob("46_*.sql"))
        assert len(files) == 1, f"unexpected 46_* migrations: {files}"
        # The new migration is the only one with the 46 prefix.
        assert files[0].name.startswith("46_dispatcher-")

    def test_no_higher_numbered_migration_exists(self) -> None:
        # 46 is intended as the new tip; 47+ would mean another migration
        # landed between PR draft + CI which would make this test fail
        # noisily in a way that signals "rebase / rename me".
        migrations_dir = REPO_ROOT / "packages/api/migrations"
        higher = sorted(
            migrations_dir.glob("[0-9]*_*.sql"),
            key=lambda p: int(p.name.split("_", 1)[0]),
            reverse=True,
        )
        # Top file is the 46 migration.
        assert higher, "no migrations found"
        top_num = int(higher[0].name.split("_", 1)[0])
        assert top_num <= 46, (
            f"a higher-numbered migration than 46 exists ({higher[0].name}); "
            "rename this migration to a higher number to avoid collision"
        )
