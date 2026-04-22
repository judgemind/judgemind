"""Unit tests for per-iteration ralph patch persistence (issue #3026).

Covers the new daemon helpers layered on top of the #3012 / #3013
SHIP-only path:

* :meth:`DispatcherDaemon._persist_ralph_iteration_patch` — additive
  INSERT per ralph iteration (no DELETE supersede). Called at
  end-of-iteration via the ``scripts/dispatcher/persist_ralph_iteration.py``
  helper CLI, writes a row carrying ``(agent_id, issue_number,
  iteration_n, verdict, patch_content, commit_sha)``.
* :meth:`DispatcherDaemon._format_resume_with_conflict_block` — renders
  the structured "RESUME WITH CONFLICT" prompt block surfaced to
  ralph when the unified resume lookup's ``git am --3way`` hit a
  conflict.
* :func:`daemon._format_age_ago` — compact human age formatter for the
  RESUME WITH CONFLICT ``{age_ago}`` placeholder.

Schema coverage: the migration adding ``iteration_n`` and ``verdict``
columns is structural (no new function to exercise); the columns are
asserted indirectly here by checking the INSERT SQL signature.

All DB interaction is against the same ``_FakeCursor`` /
``_FakeConnection`` stubs used by the sibling tests; no real Postgres.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        pass

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — identical shape to test_daemon_ralph_patches.py
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
    logger = logging.getLogger(f"dispatcher.test.per_iteration.{id(tmp_path)}")
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


# --------------------------------------------------------------------------
# _persist_ralph_iteration_patch
# --------------------------------------------------------------------------


class TestPersistRalphIterationPatch:
    """AC #2, #3 — additive INSERT per iteration (no DELETE supersede)."""

    def test_insert_runs_with_iteration_and_verdict_columns(
        self, tmp_path: Path
    ) -> None:
        """Happy path: format-patch returns bytes, INSERT runs with
        ``iteration_n`` and ``verdict`` columns populated."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        agent_id = "11111111-2222-3333-4444-555555555555"

        patch_bytes = (
            "From abc1234 Mon Sep 17 00:00:00 2001\n"
            "Subject: [PATCH] WIP: ralph output\n"
            "---\n"
            " src/foo.py | 2 +-\n"
        )
        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=0,
            stdout=patch_bytes,
            stderr="",
        )
        rev_parse_ok = subprocess.CompletedProcess(
            args=["git", "rev-parse"],
            returncode=0,
            stdout="deadbeefcafe1234\n",
            stderr="",
        )
        conn.cursor_instance.fetch_queue = [("generated-iter-patch-uuid",)]

        with patch("subprocess.run", side_effect=[format_patch_ok, rev_parse_ok]):
            result = d._persist_ralph_iteration_patch(
                agent_id=agent_id,
                issue_number=3026,
                iteration_n=2,
                verdict="LOOP",
                worktree=worktree,
            )

        assert result == "generated-iter-patch-uuid"

        # Exactly one INSERT, no DELETE (additive semantics).
        executed = conn.cursor_instance.executed
        inserts = [
            (s, p) for s, p in executed if "INSERT INTO dispatcher.ralph_patches" in s
        ]
        deletes = [
            (s, p) for s, p in executed if "DELETE FROM dispatcher.ralph_patches" in s
        ]
        assert len(inserts) == 1
        assert len(deletes) == 0

        # INSERT params shape: (agent_id, issue_number, patch_content,
        # commit_sha, iteration_n, verdict).
        sql, params = inserts[0]
        assert "iteration_n" in sql
        assert "verdict" in sql
        assert params == (
            agent_id,
            3026,
            patch_bytes,
            "deadbeefcafe1234",
            2,
            "LOOP",
        )
        assert conn.commits >= 1
        # Success event emitted with the iteration_n + verdict fields
        # for CloudWatch querying.
        events = handler.events("ralph_iteration_patch_persisted")
        assert len(events) == 1
        record = events[0]
        assert record.agent_id == agent_id
        assert record.issue_number == 3026
        assert record.iteration_n == 2
        assert record.verdict == "LOOP"

    def test_format_patch_uses_origin_main_range(self, tmp_path: Path) -> None:
        """The capture uses ``format-patch origin/main..HEAD --stdout``
        (full cumulative range) rather than ``-1 HEAD``. Regression
        guard against silently dropping to single-commit mode."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=0,
            stdout="patch bytes\n",
            stderr="",
        )
        rev_parse_ok = subprocess.CompletedProcess(
            args=["git", "rev-parse"], returncode=0, stdout="sha\n", stderr=""
        )
        conn.cursor_instance.fetch_queue = [("uuid",)]

        with patch(
            "subprocess.run", side_effect=[format_patch_ok, rev_parse_ok]
        ) as run_mock:
            d._persist_ralph_iteration_patch(
                agent_id="ag",
                issue_number=1,
                iteration_n=1,
                verdict="LOOP",
                worktree=worktree,
            )

        # First subprocess call is format-patch; verify the range form.
        first_call_args = run_mock.call_args_list[0].args[0]
        assert "format-patch" in first_call_args
        assert "origin/main..HEAD" in first_call_args
        assert "--stdout" in first_call_args
        # Guard against the -1 HEAD form.
        assert "-1" not in first_call_args

    def test_multiple_iterations_accumulate_rows(self, tmp_path: Path) -> None:
        """Calling the helper 3 times for 3 iterations produces 3
        INSERTs, no DELETE between them — rows accumulate so a crash
        after iteration N+1 can still resume from iteration N."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=0,
            stdout="patch\n",
            stderr="",
        )
        rev_parse_ok = subprocess.CompletedProcess(
            args=["git", "rev-parse"], returncode=0, stdout="sha\n", stderr=""
        )

        # 3 iterations × 2 subprocess calls each = 6 side_effects, and
        # 3 fetch_queue entries for the INSERT RETURNING.
        conn.cursor_instance.fetch_queue = [("u1",), ("u2",), ("u3",)]
        side_effects = [format_patch_ok, rev_parse_ok] * 3

        with patch("subprocess.run", side_effect=side_effects):
            for n in (1, 2, 3):
                d._persist_ralph_iteration_patch(
                    agent_id="ag",
                    issue_number=99,
                    iteration_n=n,
                    verdict="LOOP",
                    worktree=worktree,
                )

        inserts = [
            (s, p)
            for s, p in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.ralph_patches" in s
        ]
        deletes = [
            (s, p)
            for s, p in conn.cursor_instance.executed
            if "DELETE FROM dispatcher.ralph_patches" in s
        ]
        assert len(inserts) == 3
        assert len(deletes) == 0
        # iteration_n ascends 1, 2, 3.
        iter_ns = [params[4] for _sql, params in inserts]
        assert iter_ns == [1, 2, 3]

    def test_empty_patch_returns_none_and_skips_insert(self, tmp_path: Path) -> None:
        """No diff → no DB write, no crash."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        empty_patch = subprocess.CompletedProcess(
            args=["git", "format-patch"], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", side_effect=[empty_patch]):
            result = d._persist_ralph_iteration_patch(
                agent_id="ag",
                issue_number=1,
                iteration_n=1,
                verdict="LOOP",
                worktree=worktree,
            )

        assert result is None
        assert not any(
            "dispatcher.ralph_patches" in sql
            for sql, _p in conn.cursor_instance.executed
        )
        assert handler.events("ralph_iteration_patch_empty_or_failed")

    def test_db_error_rolls_back_returns_none(self, tmp_path: Path) -> None:
        """A DB error during INSERT rolls back; helper returns None.
        Non-fatal: ralph keeps iterating — missing an intermediate
        write just means resume inherits the previous iteration's state."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=0,
            stdout="patch\n",
            stderr="",
        )
        rev_parse_ok = subprocess.CompletedProcess(
            args=["git", "rev-parse"], returncode=0, stdout="sha\n", stderr=""
        )

        original = conn.cursor_instance.execute

        def failing(sql: str, params: Any = None) -> None:
            original(sql, params)
            if "INSERT INTO dispatcher.ralph_patches" in sql:
                raise RuntimeError("deadlock")

        conn.cursor_instance.execute = failing  # type: ignore[method-assign]

        with patch("subprocess.run", side_effect=[format_patch_ok, rev_parse_ok]):
            result = d._persist_ralph_iteration_patch(
                agent_id="ag",
                issue_number=1,
                iteration_n=1,
                verdict="LOOP",
                worktree=worktree,
            )

        assert result is None
        assert conn.rollbacks >= 1
        assert handler.events("ralph_iteration_patch_persist_failed")


# --------------------------------------------------------------------------
# _format_resume_with_conflict_block
# --------------------------------------------------------------------------


class TestFormatResumeWithConflictBlock:
    """AC #6 — the RESUME WITH CONFLICT prompt block contains the
    exact placeholders the issue body specifies: ``{N}``,
    ``{agent_id_short}``, ``{iter_n}``, ``{verdict}``, ``{age_ago}``,
    ``{K}``."""

    def test_all_placeholders_populated(self) -> None:
        block = daemon.DispatcherDaemon._format_resume_with_conflict_block(
            issue_number=3026,
            conflict_info={
                "source_agent_id": "abcdef0123456789",
                "iteration_n": 3,
                "verdict": "LOOP",
                "age_seconds": 3725.0,  # 1h 2m
                "conflict_files": ["src/foo.py", "src/bar.py"],
            },
        )

        # Heading (must match exactly — ralph's worker is trained on
        # this verbatim string).
        assert block.startswith("RESUME WITH CONFLICT")
        # Issue number placeholder.
        assert "issue #3026" in block
        # agent_id_short = first 8 chars of source_agent_id.
        assert "agent abcdef01" in block
        # iter_n.
        assert "iteration 3" in block
        # verdict.
        assert "verdict LOOP" in block
        # age_ago (coarse: 1h 2m).
        assert "1h 2m ago" in block
        # K = count of conflict files.
        assert "2 files are in conflict" in block
        # The two canonical options ralph must choose between.
        assert "git am --continue" in block
        assert "git am --abort" in block
        # Conflict files enumerated for operator visibility.
        assert "src/foo.py" in block
        assert "src/bar.py" in block

    def test_unknown_source_agent_id_uses_placeholder(self) -> None:
        block = daemon.DispatcherDaemon._format_resume_with_conflict_block(
            issue_number=1,
            conflict_info={
                # No source_agent_id — defensive fallback.
                "iteration_n": 1,
                "verdict": "LOOP",
                "age_seconds": 30.0,
                "conflict_files": [],
            },
        )
        assert "agent unknown" in block

    def test_ship_verdict_no_iteration_n_renders_marker(self) -> None:
        """SHIP rows carry ``iteration_n=NULL`` by #3026 convention —
        the block must render a non-numeric marker rather than
        "iteration None"."""
        block = daemon.DispatcherDaemon._format_resume_with_conflict_block(
            issue_number=42,
            conflict_info={
                "source_agent_id": "11111111-xxxx",
                "iteration_n": None,
                "verdict": "SHIP",
                "age_seconds": 600.0,
                "conflict_files": [],
            },
        )
        assert "iteration SHIP (no iteration)" in block
        assert "iteration None" not in block

    def test_zero_conflict_files_still_renders(self) -> None:
        """K=0 is possible when ``git am`` fails with no unmerged
        paths (e.g. pure index corruption). The block still renders
        and reports 0."""
        block = daemon.DispatcherDaemon._format_resume_with_conflict_block(
            issue_number=7,
            conflict_info={
                "source_agent_id": "a" * 16,
                "iteration_n": 1,
                "verdict": "LOOP",
                "age_seconds": 0.0,
                "conflict_files": [],
            },
        )
        assert "0 files are in conflict" in block
        # No "Conflict files:" sub-section when the list is empty.
        assert "Conflict files:" not in block


# --------------------------------------------------------------------------
# _format_age_ago
# --------------------------------------------------------------------------


class TestFormatAgeAgo:
    """Coarse age buckets used by the RESUME WITH CONFLICT block."""

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0.0, "just now"),
            (30.0, "just now"),
            (59.9, "just now"),
            (60.0, "1m ago"),
            (120.0, "2m ago"),
            (3599.0, "59m ago"),
            (3600.0, "1h 0m ago"),
            (3725.0, "1h 2m ago"),
            (86399.0, "23h 59m ago"),
            (86400.0, "1d 0h ago"),
            (90000.0, "1d 1h ago"),
            (259200.0, "3d 0h ago"),
        ],
    )
    def test_buckets(self, seconds: float, expected: str) -> None:
        assert daemon._format_age_ago(seconds) == expected


# --------------------------------------------------------------------------
# _list_git_am_conflict_files
# --------------------------------------------------------------------------


class TestListGitAmConflictFiles:
    """Helper used by ``_apply_prior_ralph_patch`` on the conflict path
    to populate ``conflict_files`` in the return dict."""

    def test_returns_unmerged_paths(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        diff_result = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="src/foo.py\nsrc/bar.py\n",
            stderr="",
        )
        with patch("subprocess.run", side_effect=[diff_result]):
            files = d._list_git_am_conflict_files(worktree)

        assert files == ["src/foo.py", "src/bar.py"]

    def test_uses_diff_filter_U(self, tmp_path: Path) -> None:
        """Query must use ``--diff-filter=U`` to catch unmerged entries."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        diff_result = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", side_effect=[diff_result]) as run_mock:
            d._list_git_am_conflict_files(worktree)

        args = run_mock.call_args_list[0].args[0]
        assert "--diff-filter=U" in args
        assert "--name-only" in args

    def test_returns_empty_on_subprocess_error(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch("subprocess.run", side_effect=Exception("boom")):
            files = d._list_git_am_conflict_files(worktree)

        assert files == []
