"""Unit tests for ``_read_phase_output`` mtime-ordering and corrupt-fallback
behaviour added in issue #3480.

Two test classes / four tests:

* ``TestReadPhaseOutputStaleBaseline`` — verifies that when both the baseline-
  root path and the worktree path exist, the *newer* file wins regardless of
  which root it lives under (AC #1).
* ``TestReadPhaseOutputCorruptPrimary`` — verifies that a corrupt primary file
  falls through to the valid fallback, and that both-corrupt returns ``None``
  with structured log events (AC #2).

Follows the ``_make_daemon(tmp_path)`` + ``_CapturingLogHandler`` pattern from
``test_daemon_phase3a.py`` and ``test_daemon_phase3b.py``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — minimal subset sufficient for _read_phase_output tests.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.fetch_queue:
            return None
        return self.fetch_queue.pop(0)

    def fetchall(self) -> list[Any]:
        return []


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _CapturingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.phase_io.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


def _write_output(root: Path, phase: str, payload: dict[str, Any]) -> Path:
    """Write *payload* as JSON to ``{root}/tmp/dispatcher-output/{phase}.json``."""
    out_dir = root / "tmp" / "dispatcher-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{phase}.json"
    path.write_text(json.dumps(payload))
    return path


def _write_corrupt(root: Path, phase: str) -> Path:
    """Write invalid JSON to ``{root}/tmp/dispatcher-output/{phase}.json``."""
    out_dir = root / "tmp" / "dispatcher-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{phase}.json"
    path.write_text("{not valid json")
    return path


# --------------------------------------------------------------------------
# AC #1 — mtime ordering: newest file wins regardless of which root it is in
# --------------------------------------------------------------------------


class TestReadPhaseOutputStaleBaseline:
    """``_read_phase_output`` must prefer the file with the higher mtime."""

    def test_fresh_worktree_output_wins_over_stale_baseline(
        self, tmp_path: Path
    ) -> None:
        """When the worktree file is newer than the baseline file, the worktree
        contents must be returned — not the (stale) baseline-root contents.

        This is the primary regression guard for issue #3480 AC #1: a stale
        baseline-root ``verify.json`` left over from a previous agent run must
        not shadow a freshly-written worktree ``verify.json``."""
        d, _conn, _handler = _make_daemon(tmp_path)

        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        stale_payload = {"verdict": "STALE_BASELINE", "source": "baseline"}
        fresh_payload = {"verdict": "FRESH_WORKTREE", "source": "worktree"}

        # Write both files, then manipulate mtimes so baseline is old.
        baseline_file = _write_output(baseline, "verify", stale_payload)
        worktree_file = _write_output(worktree, "verify", fresh_payload)

        now = time.time()
        old_mtime = now - 100
        new_mtime = now

        os.utime(baseline_file, (old_mtime, old_mtime))
        os.utime(worktree_file, (new_mtime, new_mtime))

        result = d._read_phase_output(worktree, "verify")

        assert result == fresh_payload, (
            f"Expected worktree (newer) payload, got {result!r}"
        )

    def test_stale_worktree_loses_to_fresh_baseline(self, tmp_path: Path) -> None:
        """Symmetric regression guard: when the baseline-root file is newer,
        its contents are returned.

        This prevents a simple "always return worktree" short-circuit from
        accidentally passing the mtime test."""
        d, _conn, _handler = _make_daemon(tmp_path)

        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        stale_payload = {"verdict": "STALE_WORKTREE", "source": "worktree"}
        fresh_payload = {"verdict": "FRESH_BASELINE", "source": "baseline"}

        baseline_file = _write_output(baseline, "verify", fresh_payload)
        worktree_file = _write_output(worktree, "verify", stale_payload)

        now = time.time()
        old_mtime = now - 100
        new_mtime = now

        os.utime(worktree_file, (old_mtime, old_mtime))
        os.utime(baseline_file, (new_mtime, new_mtime))

        result = d._read_phase_output(worktree, "verify")

        assert result == fresh_payload, (
            f"Expected baseline (newer) payload, got {result!r}"
        )


# --------------------------------------------------------------------------
# AC #2 — corrupt-primary fall-through with structured logging
# --------------------------------------------------------------------------


class TestReadPhaseOutputCorruptPrimary:
    """``_read_phase_output`` must fall through to the fallback on corrupt JSON
    and log ``daemon.phase_output_corrupt`` for each corrupt file seen."""

    def test_corrupt_primary_falls_through_to_valid_fallback(
        self, tmp_path: Path
    ) -> None:
        """When the primary (baseline-root) file contains corrupt JSON but the
        fallback (worktree) file is valid, the fallback contents are returned
        and exactly one ``daemon.phase_output_corrupt`` event is logged."""
        d, _conn, handler = _make_daemon(tmp_path)

        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        valid_payload = {"verdict": "VERIFIED", "evidence": "curl 200"}

        # Primary (baseline, newer) is corrupt; fallback (worktree, older) is valid.
        baseline_file = _write_corrupt(baseline, "verify")
        worktree_file = _write_output(worktree, "verify", valid_payload)

        now = time.time()
        os.utime(worktree_file, (now - 100, now - 100))
        os.utime(baseline_file, (now, now))

        result = d._read_phase_output(worktree, "verify")

        assert result == valid_payload, (
            f"Expected fallback payload after corrupt primary, got {result!r}"
        )
        corrupt_events = handler.events("phase_output_corrupt")
        assert len(corrupt_events) >= 1, (
            "Expected at least one phase_output_corrupt log event"
        )
        # The event must name the corrupt path.
        assert str(baseline_file) in getattr(corrupt_events[0], "path", ""), (
            f"Corrupt event path should reference the baseline file; got {corrupt_events[0]!r}"
        )

    def test_both_corrupt_returns_none_and_logs(self, tmp_path: Path) -> None:
        """When both paths contain corrupt JSON, ``None`` is returned and a
        ``daemon.phase_output_corrupt`` event is logged for each corrupt file."""
        d, _conn, handler = _make_daemon(tmp_path)

        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        baseline_file = _write_corrupt(baseline, "verify")
        worktree_file = _write_corrupt(worktree, "verify")

        now = time.time()
        os.utime(worktree_file, (now - 100, now - 100))
        os.utime(baseline_file, (now, now))

        result = d._read_phase_output(worktree, "verify")

        assert result is None, (
            f"Expected None when all candidates are corrupt, got {result!r}"
        )
        corrupt_events = handler.events("phase_output_corrupt")
        assert len(corrupt_events) >= 2, (
            f"Expected at least two phase_output_corrupt events (one per corrupt file), "
            f"got {len(corrupt_events)}"
        )
