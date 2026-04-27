"""Shape test for migration 56 — backfill ended_at on zombie agent rows.

Issue #3574. The migration repairs ``dispatcher.agents`` rows that have
a terminal status (failed / succeeded / crashed / plan_blocked /
needs_review) but a NULL ``ended_at``, caused by two write paths in
``agent-runner-entrypoint.sh`` that set ``status`` without also
stamping ``ended_at``.

This test does not require a live database. It verifies:
- The migration file exists at the expected path.
- The UP section targets the correct set of terminal statuses.
- The UP section filters ``ended_at IS NULL``.
- The UP section reads ``MAX(ts)`` from ``dispatcher.phase_transitions``
  as the backfill timestamp.
- The DOWN section is a no-op (irreversible cleanup; comment required).
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "api"
    / "migrations"
    / "57_dispatcher-backfill-ended-at.sql"
)

# Expected terminal statuses that the UP migration must filter on.
_EXPECTED_TERMINAL_STATUSES = frozenset(
    {
        "failed",
        "succeeded",
        "crashed",
        "plan_blocked",
        "needs_review",
    }
)


def _up_section(text: str) -> str:
    """Return the UP section of the migration (before ``-- Down Migration``)."""
    parts = text.split("-- Down Migration")
    return parts[0]


def _down_section(text: str) -> str:
    """Return the DOWN section of the migration (after ``-- Down Migration``)."""
    parts = text.split("-- Down Migration")
    assert len(parts) >= 2, "Migration must have a '-- Down Migration' section"
    return parts[1]


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), (
        f"Migration file missing: {MIGRATION_PATH}\n"
        "Create packages/api/migrations/57_dispatcher-backfill-ended-at.sql (#3574)"
    )


def test_up_section_exists() -> None:
    text = MIGRATION_PATH.read_text()
    assert "-- Up Migration" in text, "Migration must have an '-- Up Migration' section"


def test_down_section_exists() -> None:
    text = MIGRATION_PATH.read_text()
    assert "-- Down Migration" in text, (
        "Migration must have a '-- Down Migration' section"
    )


def test_up_targets_dispatcher_agents() -> None:
    text = MIGRATION_PATH.read_text()
    up = _up_section(text)
    assert re.search(r"UPDATE\s+dispatcher\.agents", up, re.IGNORECASE), (
        "UP section must UPDATE dispatcher.agents"
    )


def test_up_filters_terminal_statuses() -> None:
    """UP must filter on all five terminal statuses."""
    text = MIGRATION_PATH.read_text()
    up = _up_section(text)
    for status in _EXPECTED_TERMINAL_STATUSES:
        assert status in up, (
            f"UP section must filter on terminal status '{status}' (#3574)"
        )


def test_up_filters_ended_at_is_null() -> None:
    text = MIGRATION_PATH.read_text()
    up = _up_section(text)
    assert re.search(r"ended_at\s+IS\s+NULL", up, re.IGNORECASE), (
        "UP section must filter WHERE ended_at IS NULL to be idempotent"
    )


def test_up_reads_max_ts_from_phase_transitions() -> None:
    """UP must use MAX(ts) from dispatcher.phase_transitions as the
    backfill timestamp (the best estimate of when the agent actually ended)."""
    text = MIGRATION_PATH.read_text()
    up = _up_section(text)
    assert re.search(r"MAX\s*\(\s*\w*ts\w*\s*\)", up, re.IGNORECASE), (
        "UP section must read MAX(ts) from dispatcher.phase_transitions"
    )
    assert re.search(r"dispatcher\.phase_transitions", up, re.IGNORECASE), (
        "UP section must reference dispatcher.phase_transitions for backfill timestamp"
    )


def test_up_sets_ended_at() -> None:
    text = MIGRATION_PATH.read_text()
    up = _up_section(text)
    assert re.search(r"SET\s+ended_at\s*=", up, re.IGNORECASE), (
        "UP section must SET ended_at on the matched rows"
    )


def test_up_uses_coalesce_for_fallback() -> None:
    """UP should COALESCE the MAX(ts) with started_at (or similar) so
    ended_at is never left NULL even for agents with no phase_transitions."""
    text = MIGRATION_PATH.read_text()
    up = _up_section(text)
    assert re.search(r"COALESCE\s*\(", up, re.IGNORECASE), (
        "UP section must use COALESCE so ended_at is non-NULL even when "
        "no phase_transitions rows exist for the agent"
    )


def test_down_is_noop_with_comment() -> None:
    """DOWN must not execute a data-modifying statement.

    The backfill is irreversible — restoring NULL ended_at would
    re-introduce the monitoring blindness bug. The DOWN section must
    contain a comment explaining this, and must not contain an UPDATE or
    DELETE that would mutate data.
    """
    text = MIGRATION_PATH.read_text()
    down = _down_section(text)
    # Must contain a comment referencing irreversibility or no-op intent.
    assert re.search(r"irreversible|no.op|no_op|noop", down, re.IGNORECASE), (
        "DOWN section must document that the backfill is irreversible "
        "and this is a no-op down migration"
    )
    # Must not contain a data-modifying UPDATE that touches ended_at
    # (a SELECT or comment-only is fine).
    data_modifying = re.search(
        r"^\s*UPDATE\s+dispatcher\.agents",
        down,
        re.IGNORECASE | re.MULTILINE,
    )
    assert data_modifying is None, (
        "DOWN section must not UPDATE dispatcher.agents (irreversible backfill)"
    )
