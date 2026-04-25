"""Schema test — ``dispatcher.diagnoses.subprocess_pid`` exists (#3376).

Asserts that migration 47 adds the ``subprocess_pid INTEGER`` column
to ``dispatcher.diagnoses`` AND seeds the ``max_concurrent_diagnoses``
config row. Both changes were consolidated into a single migration
file (originally drafted as 48 + 49 / 51 + 52, then merged after CI
surfaced collisions with PRs #3355, #3367, #3372).

The async fire-and-forget spawn architecture hinges on the
``subprocess_pid`` column — it's where the supervisor-tick spawn
pass records the pid so the reaper pass can find the subprocess on
a later tick.

This is a static-file test (reads the migration SQL); a live DB
schema check happens in production via the migration applier.
"""

from __future__ import annotations

from pathlib import Path

# The dispatcher tests run from ``scripts/dispatcher/tests/``.
# Migrations live at the repo root — three levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _REPO_ROOT / "packages" / "api" / "migrations"
_MIGRATION_PATH = _MIGRATIONS / "47_dispatcher-async-diagnoser-spawn.sql"


def test_subprocess_pid_migration_file_exists() -> None:
    """Migration 47 file exists and references the right table."""
    assert _MIGRATION_PATH.exists(), (
        f"Expected migration {_MIGRATION_PATH.name} to exist; got "
        f"{[p.name for p in _MIGRATIONS.iterdir() if p.suffix == '.sql']}"
    )


def test_subprocess_pid_column_added_to_diagnoses() -> None:
    """The migration's UP block adds ``subprocess_pid INTEGER`` to
    ``dispatcher.diagnoses``."""
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert "ALTER TABLE dispatcher.diagnoses" in sql
    assert "ADD COLUMN subprocess_pid INTEGER" in sql


def test_subprocess_pid_has_partial_index() -> None:
    """The migration creates a partial index that supports the reaper's
    ``WHERE status='pending' AND subprocess_pid IS NOT NULL`` scan
    without dragging in the rest of the table."""
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert "idx_dispatcher_diagnoses_pending_pid" in sql
    assert "WHERE status = 'pending'" in sql
    assert "subprocess_pid IS NOT NULL" in sql


def test_down_migration_drops_column() -> None:
    """Down migration is the inverse — drop the index, then the column,
    then the config row."""
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert "-- Down Migration" in sql
    assert "DROP COLUMN IF EXISTS subprocess_pid" in sql
    assert "DROP INDEX IF EXISTS dispatcher.idx_dispatcher_diagnoses_pending_pid" in sql
    assert "DELETE FROM dispatcher.config" in sql


def test_max_concurrent_diagnoses_seed() -> None:
    """The migration also seeds the ``max_concurrent_diagnoses`` config
    row in the same file (consolidated from migration 49 / 52)."""
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert "INSERT INTO dispatcher.config" in sql
    assert "max_concurrent_diagnoses" in sql
    # Default cap of 2 — matches DEFAULT_MAX_CONCURRENT_DIAGNOSES in
    # the daemon module.
    assert "'2'" in sql
    assert "ON CONFLICT" in sql
