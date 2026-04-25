"""Schema test for migration 49 — drop NOT NULL on agents.issue_number.

Issue #3380 (follow-up to #3374). Synthetic scheduled-skill agents
INSERT a row with ``issue_number IS NULL``; the original
NOT NULL constraint blocked the spawn path.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "49_dispatcher-agents-issue-number-nullable.sql"
)
SCHEMA_SQL = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "src"
    / "data-access"
    / "schema.sql"
)


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), f"missing: {MIGRATION_PATH}"


def test_up_drops_not_null() -> None:
    text = MIGRATION_PATH.read_text()
    up = text.split("-- Down Migration")[0]
    assert re.search(r"ALTER COLUMN issue_number\s+DROP NOT NULL", up, re.IGNORECASE), (
        "Up section must DROP NOT NULL on issue_number"
    )


def test_down_restores_not_null() -> None:
    text = MIGRATION_PATH.read_text()
    down = text.split("-- Down Migration")[1]
    assert re.search(
        r"ALTER COLUMN issue_number\s+SET NOT NULL", down, re.IGNORECASE
    ), "Down section must restore NOT NULL on issue_number"
    # Down also must coerce NULLs to 0 first to avoid constraint
    # violation on synthetic-skill rows.
    assert re.search(
        r"UPDATE dispatcher\.agents.*WHERE issue_number IS NULL",
        down,
        re.IGNORECASE | re.DOTALL,
    ), "Down section must UPDATE NULLs to 0 before re-adding constraint"


def test_schema_sql_reflects_nullable() -> None:
    """schema.sql snapshot must show issue_number without NOT NULL."""
    text = SCHEMA_SQL.read_text()
    # Find the dispatcher.agents CREATE TABLE block.
    m = re.search(r"CREATE TABLE dispatcher\.agents\s*\((.*?)\);", text, re.DOTALL)
    assert m, "missing CREATE TABLE dispatcher.agents in schema.sql"
    body = m.group(1)
    # Find the issue_number line; it should NOT contain "NOT NULL".
    line_match = re.search(r"^\s*issue_number\s+[^\n,]*", body, re.MULTILINE)
    assert line_match, "issue_number column not declared in schema.sql"
    assert "NOT NULL" not in line_match.group(0), (
        f"issue_number must be nullable post-migration 49; got: {line_match.group(0)!r}"
    )
