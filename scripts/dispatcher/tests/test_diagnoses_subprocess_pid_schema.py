"""Schema test — ``dispatcher.diagnoses.subprocess_pid`` exists (#3376).

Asserts that migration 48 adds the ``subprocess_pid INTEGER`` column
to ``dispatcher.diagnoses``. The async fire-and-forget spawn architecture
hinges on this column — it's where the supervisor-tick spawn pass
records the pid so the reaper pass can find the subprocess on a later
tick.

This is a static-file test (reads the migration SQL); a live DB
schema check happens in production via the migration applier.
"""

from __future__ import annotations

from pathlib import Path

# The dispatcher tests run from ``scripts/dispatcher/tests/``.
# Migrations live at the repo root — three levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _REPO_ROOT / "packages" / "api" / "migrations"


def test_subprocess_pid_migration_file_exists() -> None:
    """Migration 48 file exists and references the right table."""
    candidates = list(_MIGRATIONS.glob("48_*.sql"))
    assert candidates, (
        f"Expected one migration 48_*.sql under {_MIGRATIONS}, "
        f"got {[p.name for p in _MIGRATIONS.iterdir() if p.suffix == '.sql']}"
    )
    # Issue #3376 owns migration 48 — the column add for subprocess_pid.
    assert any("subprocess-pid" in p.name for p in candidates), candidates


def test_subprocess_pid_column_added_to_diagnoses() -> None:
    """The migration's UP block adds ``subprocess_pid INTEGER`` to
    ``dispatcher.diagnoses``."""
    path = _MIGRATIONS / "48_dispatcher-diagnoses-subprocess-pid.sql"
    sql = path.read_text(encoding="utf-8")

    assert "ALTER TABLE dispatcher.diagnoses" in sql
    assert "ADD COLUMN subprocess_pid INTEGER" in sql


def test_subprocess_pid_has_partial_index() -> None:
    """The migration creates a partial index that supports the reaper's
    ``WHERE status='pending' AND subprocess_pid IS NOT NULL`` scan
    without dragging in the rest of the table."""
    path = _MIGRATIONS / "48_dispatcher-diagnoses-subprocess-pid.sql"
    sql = path.read_text(encoding="utf-8")

    assert "idx_dispatcher_diagnoses_pending_pid" in sql
    assert "WHERE status = 'pending'" in sql
    assert "subprocess_pid IS NOT NULL" in sql


def test_down_migration_drops_column() -> None:
    """Down migration is the inverse — drop the index, then the column."""
    path = _MIGRATIONS / "48_dispatcher-diagnoses-subprocess-pid.sql"
    sql = path.read_text(encoding="utf-8")

    assert "-- Down Migration" in sql
    assert "DROP COLUMN IF EXISTS subprocess_pid" in sql
    assert "DROP INDEX IF EXISTS dispatcher.idx_dispatcher_diagnoses_pending_pid" in sql


def test_max_concurrent_diagnoses_seed_migration() -> None:
    """Migration 49 seeds the ``max_concurrent_diagnoses`` config row."""
    path = _MIGRATIONS / "49_dispatcher-max-concurrent-diagnoses-seed.sql"
    assert path.exists(), f"expected {path} to exist"
    sql = path.read_text(encoding="utf-8")

    assert "INSERT INTO dispatcher.config" in sql
    assert "max_concurrent_diagnoses" in sql
    # Default cap of 2 — matches DEFAULT_MAX_CONCURRENT_DIAGNOSES in
    # the daemon module.
    assert "'2'" in sql
    assert "ON CONFLICT" in sql
