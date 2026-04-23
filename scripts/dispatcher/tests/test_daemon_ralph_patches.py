"""Unit tests for ralph SHIP'd-patch persistence (issue #3012).

Covers the three daemon-side helpers introduced to persist ralph's
SHIP'd diff to ``dispatcher.ralph_patches`` so a daemon restart between
ralph SHIP exit and a successful ``gh pr create`` does not lose the work:

* :meth:`DispatcherDaemon._capture_and_persist_ralph_patch` — on SHIP,
  runs ``git format-patch -1 HEAD --stdout``, supersedes any prior row
  for the same ``issue_number``, INSERTs the fresh row, and UPDATEs the
  matching ralph ``phase_outputs.patch_id`` FK.
* :meth:`DispatcherDaemon._delete_ralph_patches_for_agent` — on a
  successful ``gh pr create``, DELETEs the patch by ``agent_id``.
* :meth:`DispatcherDaemon._apply_prior_ralph_patch` — at the top of the
  ralph phase, queries the latest ``ralph_patches`` row for the issue
  and attempts ``git am``. On success, HEAD carries the inherited diff.
  On failure, ``git am --abort`` restores a clean worktree and the
  patch text is returned for surfacing in ``prior_attempts.md``.

The housekeeping 7-day sweep is covered by the new entry added to
``_HOUSEKEEPING_TARGETS`` + the whole-tuple assertions in
``test_daemon_housekeeping.py``; the specific-target test below pins
the column + config key.

All DB interaction is against the same ``_FakeCursor`` /
``_FakeConnection`` stubs used elsewhere; no real Postgres required.
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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — same shape as test_daemon_push_failed.py / test_daemon_housekeeping.py
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
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
        if not self.fetchall_queue:
            return []
        return self.fetchall_queue.pop(0)


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
    logger = logging.getLogger(f"dispatcher.test.ralph_patches.{id(tmp_path)}")
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
# _capture_and_persist_ralph_patch
# --------------------------------------------------------------------------


class TestCaptureAndPersistRalphPatch:
    """AC #2 — on SHIP exit, supersede + INSERT + UPDATE sequence fires."""

    def test_insert_delete_update_sequence_on_ship(self, tmp_path: Path) -> None:
        """Happy path: format-patch succeeds, DB INSERT+DELETE+UPDATE
        all run, and the returned patch_id is threaded back correctly.

        The pivotal assertion is that the DELETE-prior-rows query runs
        BEFORE the INSERT so a retry-after-failure scenario cleanly
        supersedes the stale row rather than accumulating rows per
        attempt.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        agent_id = "11111111-2222-3333-4444-555555555555"

        # Fake patch bytes + SHA.
        patch_bytes = (
            "From abc1234 Mon Sep 17 00:00:00 2001\n"
            "Subject: [PATCH] WIP: ralph output\n"
            "---\n"
            " src/foo.py | 2 +-\n"
            " 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        )
        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"], returncode=0, stdout=patch_bytes, stderr=""
        )
        rev_parse_ok = subprocess.CompletedProcess(
            args=["git", "rev-parse"],
            returncode=0,
            stdout="deadbeefcafe1234\n",
            stderr="",
        )

        # INSERT returns a patch_id row. The cursor's fetch_queue feeds
        # the RETURNING on the INSERT.
        conn.cursor_instance.fetch_queue = [("generated-patch-uuid",)]

        with patch("subprocess.run", side_effect=[format_patch_ok, rev_parse_ok]):
            result = d._capture_and_persist_ralph_patch(
                agent_id=agent_id, issue_number=3012, worktree=worktree
            )

        assert result == "generated-patch-uuid"

        # DELETE prior rows must precede INSERT.
        executed = conn.cursor_instance.executed
        sql_only = [sql for sql, _params in executed]
        delete_idx = next(
            i
            for i, sql in enumerate(sql_only)
            if "DELETE FROM dispatcher.ralph_patches" in sql
            and "WHERE issue_number" in sql
        )
        insert_idx = next(
            i
            for i, sql in enumerate(sql_only)
            if "INSERT INTO dispatcher.ralph_patches" in sql
        )
        update_idx = next(
            i
            for i, sql in enumerate(sql_only)
            if "UPDATE dispatcher.phase_outputs" in sql and "SET patch_id" in sql
        )
        assert delete_idx < insert_idx < update_idx, (
            f"Expected DELETE({delete_idx}) < INSERT({insert_idx}) < UPDATE({update_idx}); "
            f"executed SQL order: {sql_only}"
        )

        # DELETE is parameterised by issue_number.
        assert executed[delete_idx][1] == (3012,)
        # INSERT params: (agent_id, issue_number, patch_content, commit_sha)
        insert_params = executed[insert_idx][1]
        assert insert_params[0] == agent_id
        assert insert_params[1] == 3012
        assert insert_params[2] == patch_bytes
        assert insert_params[3] == "deadbeefcafe1234"
        # UPDATE params: (patch_id, agent_id)
        update_params = executed[update_idx][1]
        assert update_params[0] == "generated-patch-uuid"
        assert update_params[1] == agent_id
        # UPDATE targets only the ralph phase row.
        assert "phase = 'ralph'" in executed[update_idx][0]

        # Transaction committed.
        assert conn.commits >= 1

    def test_empty_patch_logged_and_returns_none(self, tmp_path: Path) -> None:
        """format-patch returned empty stdout → no DB write; no patch_id."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        empty_patch = subprocess.CompletedProcess(
            args=["git", "format-patch"], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", side_effect=[empty_patch]):
            result = d._capture_and_persist_ralph_patch(
                agent_id="aaaa-bbbb", issue_number=42, worktree=worktree
            )

        assert result is None
        # No DB writes to ralph_patches.
        assert not any(
            "dispatcher.ralph_patches" in sql
            for sql, _p in conn.cursor_instance.executed
        )
        # Diagnostic event emitted.
        assert handler.events("ralph_patch_empty_or_failed")

    def test_format_patch_failure_returns_none(self, tmp_path: Path) -> None:
        """format-patch non-zero exit → no DB write; warning logged."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        fail = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repo",
        )
        with patch("subprocess.run", side_effect=[fail]):
            result = d._capture_and_persist_ralph_patch(
                agent_id="aaaa-bbbb", issue_number=42, worktree=worktree
            )

        assert result is None
        assert not any(
            "dispatcher.ralph_patches" in sql
            for sql, _p in conn.cursor_instance.executed
        )
        assert handler.events("ralph_patch_empty_or_failed")

    def test_db_error_rolls_back_and_returns_none(self, tmp_path: Path) -> None:
        """A DB error during the INSERT transaction rolls back; no patch_id."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=0,
            stdout="From abc Mon...\n--- patch body ---\n",
            stderr="",
        )
        rev_parse_ok = subprocess.CompletedProcess(
            args=["git", "rev-parse"], returncode=0, stdout="sha\n", stderr=""
        )
        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "INSERT INTO dispatcher.ralph_patches" in sql:
                raise RuntimeError("connection broken mid-insert")

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        with patch("subprocess.run", side_effect=[format_patch_ok, rev_parse_ok]):
            result = d._capture_and_persist_ralph_patch(
                agent_id="aaa", issue_number=7, worktree=worktree
            )

        assert result is None
        assert conn.rollbacks >= 1
        assert handler.events("ralph_patch_persist_failed")

    def test_commit_sha_none_still_inserts(self, tmp_path: Path) -> None:
        """rev-parse failure → commit_sha=NULL; INSERT still runs."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        format_patch_ok = subprocess.CompletedProcess(
            args=["git", "format-patch"],
            returncode=0,
            stdout="From abc Mon...\n",
            stderr="",
        )
        rev_parse_fail = subprocess.CompletedProcess(
            args=["git", "rev-parse"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repo",
        )
        conn.cursor_instance.fetch_queue = [("uuid-1",)]

        with patch("subprocess.run", side_effect=[format_patch_ok, rev_parse_fail]):
            result = d._capture_and_persist_ralph_patch(
                agent_id="ag1", issue_number=100, worktree=worktree
            )

        assert result == "uuid-1"
        insert = next(
            (sql, p)
            for sql, p in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.ralph_patches" in sql
        )
        # commit_sha (4th param) is None on rev-parse failure.
        assert insert[1][3] is None


# --------------------------------------------------------------------------
# _delete_ralph_patches_for_agent
# --------------------------------------------------------------------------


class TestDeleteRalphPatchesForAgent:
    """AC #3 — on ``gh pr create`` success, DELETE by agent_id."""

    def test_delete_fires_with_agent_id(self, tmp_path: Path) -> None:
        """DELETE targets the specific agent row, not issue_number."""
        d, conn, handler = _make_daemon(tmp_path)
        agent_id = "deadbeef-0000-1111-2222-333333333333"
        conn.cursor_instance.rowcount = 1

        deleted = d._delete_ralph_patches_for_agent(agent_id)

        assert deleted == 1
        deletes = [
            (sql, p)
            for sql, p in conn.cursor_instance.executed
            if "DELETE FROM dispatcher.ralph_patches" in sql
        ]
        assert len(deletes) == 1
        sql, params = deletes[0]
        assert "WHERE agent_id = %s" in sql
        assert params == (agent_id,)
        assert handler.events("ralph_patch_deleted")

    def test_delete_zero_rows_is_fine(self, tmp_path: Path) -> None:
        """No prior capture → delete returns 0; no warning."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 0

        assert d._delete_ralph_patches_for_agent("agent-xyz") == 0

    def test_db_error_rolls_back_returns_zero(self, tmp_path: Path) -> None:
        """DB error during DELETE rolls back, logs, returns 0."""
        d, conn, handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("deadlock")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]

        assert d._delete_ralph_patches_for_agent("agent-1") == 0
        assert conn.rollbacks >= 1
        assert handler.events("ralph_patch_delete_failed")


# --------------------------------------------------------------------------
# _apply_prior_ralph_patch
# --------------------------------------------------------------------------


class TestApplyPriorRalphPatch:
    """AC #4 — at claim, check for prior patch and apply via git am."""

    def test_returns_none_when_no_prior_patch(self, tmp_path: Path) -> None:
        """First-attempt path: query returns no row → None, no git call."""
        d, conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # fetchone → None (no row).
        conn.cursor_instance.fetch_queue = [None]

        with patch("subprocess.run") as run_mock:
            result = d._apply_prior_ralph_patch(
                agent_id="new-agent",
                issue_number=9999,
                worktree=worktree,
            )

        assert result is None
        # No git am call when there's no prior patch.
        assert run_mock.call_count == 0

    def test_apply_clean_returns_applied_true(self, tmp_path: Path) -> None:
        """git am success → returns {applied: True, patch_id, ...}
        and the patch file is staged in the worktree."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        patch_content = (
            "From abc1234 Mon Sep 17 00:00:00 2001\n"
            "Subject: [PATCH] WIP: ralph output\n"
            "---\n"
        )
        # Row shape (#3026): (patch_id, patch_content, commit_sha,
        # agent_id, iteration_n, verdict, age_seconds).
        conn.cursor_instance.fetch_queue = [
            (
                "prior-patch-uuid",
                patch_content,
                "prior-sha-abc",
                "prior-agent-uuid",
                3,
                "LOOP",
                120.0,
            ),
        ]

        am_ok = subprocess.CompletedProcess(
            args=["git", "am"], returncode=0, stdout="Applying: WIP\n", stderr=""
        )

        with patch("subprocess.run", side_effect=[am_ok]):
            result = d._apply_prior_ralph_patch(
                agent_id="new-agent-uuid",
                issue_number=3012,
                worktree=worktree,
            )

        assert result == {
            "applied": True,
            "patch_id": "prior-patch-uuid",
            "commit_sha": "prior-sha-abc",
            "source_agent_id": "prior-agent-uuid",
            "iteration_n": 3,
            "verdict": "LOOP",
            "age_seconds": 120.0,
            "bytes": len(patch_content),
        }
        # The patch file is written into tmp/dispatcher-input/.
        patch_file = worktree / "tmp" / "dispatcher-input" / "prior-ralph.patch"
        assert patch_file.exists()
        assert patch_file.read_text() == patch_content
        # Success event emitted.
        assert handler.events("ralph_patch_applied")

    def test_apply_conflict_leaves_am_in_progress(self, tmp_path: Path) -> None:
        """git am conflict (#3026) → daemon does NOT run ``git am
        --abort``; the worktree is left in am-in-progress state so
        ralph can decide continue-vs-abort. Returns
        {applied: False, conflicted: True, patch_content, ...}."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        patch_content = "From abc...\npatch body with conflicts\n"
        conn.cursor_instance.fetch_queue = [
            (
                "prior-patch-uuid",
                patch_content,
                None,
                "prior-agent",
                2,
                "LOOP",
                600.0,
            ),
        ]

        am_fail = subprocess.CompletedProcess(
            args=["git", "am"],
            returncode=128,
            stdout="",
            stderr="error: patch failed: src/foo.py:1\nerror: patch does not apply",
        )
        diff_u = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="src/foo.py\n",
            stderr="",
        )

        with patch("subprocess.run", side_effect=[am_fail, diff_u]) as run_mock:
            result = d._apply_prior_ralph_patch(
                agent_id="new-agent",
                issue_number=3012,
                worktree=worktree,
            )

        assert result is not None
        assert result["applied"] is False
        assert result["conflicted"] is True
        assert result["patch_id"] == "prior-patch-uuid"
        assert result["patch_content"] == patch_content
        assert result["source_agent_id"] == "prior-agent"
        assert result["iteration_n"] == 2
        assert result["verdict"] == "LOOP"
        assert result["age_seconds"] == 600.0
        assert result["conflict_files"] == ["src/foo.py"]
        assert "patch does not apply" in result["reason"]

        # CRITICAL (#3026): ``git am --abort`` must NOT be called on
        # the conflict-handoff path. The worktree is left in the
        # am-in-progress state so ralph can inspect it.
        abort_calls = [
            call
            for call in run_mock.call_args_list
            if len(call.args[0]) >= 5 and call.args[0][3:5] == ["am", "--abort"]
        ]
        assert len(abort_calls) == 0
        # Conflict event emitted (replaces the pre-#3026
        # ``ralph_patch_apply_failed`` event).
        assert handler.events("ralph_patch_apply_conflict")

    def test_unified_resume_lookup_uses_ttl_predicate(self, tmp_path: Path) -> None:
        """The resume SELECT uses the 7-day TTL predicate AND returns
        the most-recent row regardless of agent_id (unified lookup,
        #3026). Same-agent rows are valid resume candidates — the
        pre-#3026 self-inherit guard is removed."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        same_agent_id = "same-agent-uuid"
        # Prior row is the SAME agent as the caller — post-#3026 this
        # must be applied, not skipped, because fresh worktrees always
        # start at origin/main HEAD so there's no "already at HEAD"
        # conflict to worry about.
        conn.cursor_instance.fetch_queue = [
            (
                "prior-patch-uuid",
                "patch body\n",
                None,
                same_agent_id,
                1,
                "LOOP",
                30.0,
            ),
        ]
        am_ok = subprocess.CompletedProcess(
            args=["git", "am"], returncode=0, stdout="Applying\n", stderr=""
        )
        with patch("subprocess.run", side_effect=[am_ok]):
            result = d._apply_prior_ralph_patch(
                agent_id=same_agent_id,
                issue_number=123,
                worktree=worktree,
            )

        assert result is not None
        assert result["applied"] is True
        assert result["source_agent_id"] == same_agent_id
        # Verify the SELECT query carries the TTL predicate.
        selects = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "SELECT" in sql and "FROM dispatcher.ralph_patches" in sql
        ]
        assert len(selects) == 1
        sql, params = selects[0]
        assert "make_interval(days => %s)" in sql
        # Parameters: (issue_number, retention_days).
        assert params[0] == 123
        assert params[1] == daemon.DEFAULT_RALPH_PATCH_RETENTION_DAYS
        # No self-inherit skip event; the unified lookup applies.
        assert not handler.events("ralph_patch_self_inherit_skipped")

    def test_db_query_failure_returns_none(self, tmp_path: Path) -> None:
        """A DB error on the SELECT is non-fatal — returns None
        (as if there's no prior patch) so the agent continues normally."""
        d, conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("connection lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]

        with patch("subprocess.run") as run_mock:
            result = d._apply_prior_ralph_patch(
                agent_id="ag", issue_number=1, worktree=worktree
            )

        assert result is None
        assert run_mock.call_count == 0
        assert conn.rollbacks >= 1
        assert handler.events("ralph_patch_query_failed")


# --------------------------------------------------------------------------
# _append_unapplied_patch_to_prior_attempts
# --------------------------------------------------------------------------


class TestAppendUnappliedPatch:
    """Unapplied patch text gets surfaced to the ralph worker via
    ``prior_attempts.md`` so the worker can cherry-pick manually."""

    def test_appends_when_prior_attempts_md_exists(self, tmp_path: Path) -> None:
        """Invocation-failure branch (``conflicted=False``) — renders
        the legacy "Prior ralph patch" heading with the patch text
        below for manual cherry-pick."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        (worktree / "tmp" / "dispatcher-output").mkdir(parents=True)
        existing = worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        existing.write_text("# prior attempts\n\nAttempt 1 content.\n")

        d._append_unapplied_patch_to_prior_attempts(
            worktree,
            {
                "applied": False,
                "conflicted": False,
                "patch_id": "pid-1",
                "patch_content": "From abc...\npatch body\n",
                "reason": "am invocation failed: timeout",
            },
        )

        content = existing.read_text()
        assert "Attempt 1 content." in content
        assert "## Prior ralph patch (did NOT apply cleanly)" in content
        assert "pid-1" in content
        assert "am invocation failed" in content
        # The patch body is present, fenced as a diff block.
        assert "```diff" in content
        assert "patch body" in content

    def test_creates_file_when_prior_attempts_md_missing(self, tmp_path: Path) -> None:
        """If prior_attempts.md doesn't exist and the am failed before
        conflict resolution (invocation failure), still write a
        standalone file so the worker gets the patch context."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"

        d._append_unapplied_patch_to_prior_attempts(
            worktree,
            {
                "applied": False,
                "conflicted": False,
                "patch_id": "pid-2",
                "patch_content": "standalone patch body\n",
                "reason": "no current patch",
            },
        )

        target = worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        assert target.exists()
        content = target.read_text()
        assert "Prior ralph patch" in content
        assert "standalone patch body" in content

    def test_conflicted_branch_renders_resume_block(self, tmp_path: Path) -> None:
        """Conflict-handoff branch (#3026, ``conflicted=True``) — renders
        the RESUME WITH CONFLICT structured prompt block for ralph."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"

        d._append_unapplied_patch_to_prior_attempts(
            worktree,
            {
                "applied": False,
                "conflicted": True,
                "patch_id": "pid-3",
                "patch_content": "patch body\n",
                "source_agent_id": "aaaabbbbccccdddd",
                "iteration_n": 4,
                "verdict": "LOOP",
                "age_seconds": 3725.0,  # 1h 2m
                "conflict_files": ["src/foo.py", "src/bar.py"],
                "reason": "patch does not apply",
            },
            issue_number=3026,
        )

        target = worktree / "tmp" / "dispatcher-output" / "prior_attempts.md"
        content = target.read_text()
        # Structured block heading must appear verbatim — ralph's
        # worker is trained to recognise this exact phrase.
        assert "RESUME WITH CONFLICT" in content
        assert "issue #3026" in content
        assert "agent aaaabbbb" in content  # agent_id_short
        assert "iteration 4" in content
        assert "verdict LOOP" in content
        assert "1h 2m ago" in content
        assert "2 files are in conflict" in content
        assert "git am --continue" in content
        assert "git am --abort" in content
        # Patch body is still included for manual inspection.
        assert "```diff" in content
        assert "patch body" in content
        # Conflict files listed.
        assert "src/foo.py" in content
        assert "src/bar.py" in content


# --------------------------------------------------------------------------
# Housekeeping target — ``ralph_patches`` pruning (AC #6)
# --------------------------------------------------------------------------


class TestRalphPatchesHousekeepingTarget:
    """AC #6 — housekeeping tick DELETEs patches older than 7 days."""

    def test_ralph_patches_entry_in_targets(self) -> None:
        entry = next(
            t
            for t in daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS
            if t[0] == "ralph_patches"
        )
        assert entry == (
            "ralph_patches",
            "created_at",
            "ralph_patch_retention_days",
            daemon.DEFAULT_RALPH_PATCH_RETENTION_DAYS,
        )
        assert daemon.DEFAULT_RALPH_PATCH_RETENTION_DAYS == 7

    def test_delete_runs_with_default_7d_cutoff(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Single-target monkeypatch — only ``ralph_patches`` fires."""
        entry = next(
            t
            for t in daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS
            if t[0] == "ralph_patches"
        )
        monkeypatch.setattr(daemon.DispatcherDaemon, "_HOUSEKEEPING_TARGETS", (entry,))
        d, conn, handler = _make_daemon(tmp_path)
        # config lookup returns None → falls back to default
        conn.cursor_instance.fetch_queue = [None]

        original_execute = conn.cursor_instance.execute

        def patched_execute(sql: str, params: Any = None) -> None:
            original_execute(sql, params)
            if "DELETE FROM dispatcher.ralph_patches" in sql:
                conn.cursor_instance.rowcount = 3

        conn.cursor_instance.execute = patched_execute  # type: ignore[method-assign]

        result = d._housekeeping_tick()

        deletes = [
            (sql, p)
            for sql, p in conn.cursor_instance.executed
            if sql.startswith("DELETE FROM dispatcher.ralph_patches")
        ]
        assert len(deletes) == 1
        sql, params = deletes[0]
        assert "created_at < now() - make_interval(days => %s)" in sql
        assert params == (daemon.DEFAULT_RALPH_PATCH_RETENTION_DAYS,)

        ticks = handler.events("housekeeping_tick")
        assert any(
            r.table == "ralph_patches"
            and r.cutoff_days == daemon.DEFAULT_RALPH_PATCH_RETENTION_DAYS
            and r.rows_deleted == 3
            for r in ticks
        )
        assert result == {"ralph_patches": 3}


# --------------------------------------------------------------------------
# Cleanup on gh pr create success (AC #3, integration view)
# --------------------------------------------------------------------------


class TestPushAndOpenPrDeletesRalphPatch:
    """AC #3 — ``_push_and_open_pr`` calls the delete helper after a
    successful ``gh pr create`` so the postgres copy is cleaned up the
    moment the branch lands on origin."""

    def test_pr_create_success_triggers_delete(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000111"

        # Stub the DB methods _push_and_open_pr touches.
        d._agent_summary_output = {  # type: ignore[attr-defined]
            "commit_message": "feat(dispatcher): ralph patches (#3012)",
            "pr_title": "feat(dispatcher): ralph patches (#3012)",
            "pr_body_md": "body",
        }
        d._agent_unmet_criteria = []  # type: ignore[attr-defined]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._delete_ralph_patches_for_agent = MagicMock(  # type: ignore[method-assign]
            return_value=1
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/4242\n",
            stderr="",
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        with patch(
            "subprocess.run",
            side_effect=[
                rev_list_ahead,
                commit_ok,
                git_show_empty,
                push_ok,
                pr_create_ok,
            ],
        ):
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=3012,
                worktree=worktree,
            )

        # Delete helper invoked exactly once with the agent_id.
        d._delete_ralph_patches_for_agent.assert_called_once_with(agent_id)

    def test_push_failure_does_not_trigger_delete(self, tmp_path: Path) -> None:
        """If push fails, the PR was never created — we must NOT delete
        the persisted patch, because the whole point is to survive to
        the next retry."""
        d, _conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000222"

        d._agent_summary_output = {  # type: ignore[attr-defined]
            "commit_message": "c",
            "pr_title": "t",
            "pr_body_md": "b",
        }
        d._agent_unmet_criteria = []  # type: ignore[attr-defined]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._delete_ralph_patches_for_agent = MagicMock(  # type: ignore[method-assign]
            return_value=0
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        push_fail = subprocess.CompletedProcess(
            args=["git", "push"],
            returncode=1,
            stdout="",
            stderr="fatal: unable to access",
        )

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #3089: ``git push`` now retries 3 times before falling
        # through to ``_handle_agent_failure``. Provide three push_fail
        # results and stub ``time.sleep`` to keep the test fast.
        with (
            patch(
                "subprocess.run",
                side_effect=[
                    rev_list_ahead,
                    commit_ok,
                    git_show_empty,
                    push_fail,
                    push_fail,
                    push_fail,
                ],
            ),
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._push_and_open_pr(
                agent_id=agent_id,
                issue_number=3012,
                worktree=worktree,
            )

        # Delete helper was NOT invoked — the patch must survive the
        # push failure so the retry can inherit it.
        d._delete_ralph_patches_for_agent.assert_not_called()


# --------------------------------------------------------------------------
# Integration view — real git + daemon capture_and_persist end-to-end
# --------------------------------------------------------------------------


def _git(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git -C <worktree> <args>`` with minimal env for hermetic tests."""
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(worktree.parent),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


class TestCaptureThenApplyRoundtrip:
    """End-to-end: daemon captures ralph's SHIP'd commit as a patch, a
    fresh worktree off origin/main applies it via ``git am`` and the
    diff lands.  Exercises the real ``git format-patch`` + ``git am``
    invocations, not mocks.  The DB side is still stubbed — the round
    trip through postgres is covered by the unit tests above.
    """

    def test_format_patch_then_git_am_roundtrip(self, tmp_path: Path) -> None:
        # Seed a "shared origin" bare repo.
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "--bare", str(origin), check=True)
        # Seed a working clone with one commit on main.
        source = tmp_path / "source"
        source.mkdir()
        _git(source, "init", "-b", "main", "--quiet")
        _git(source, "config", "commit.gpgsign", "false")
        _git(source, "remote", "add", "origin", str(origin))
        (source / "README.md").write_text("# test\n")
        _git(source, "add", "README.md")
        _git(source, "commit", "-m", "initial", "--quiet")
        _git(source, "push", "origin", "main")

        # "Agent A" worktree: create branch, make a ralph-style commit.
        agent_a = tmp_path / "agent-a"
        _git(tmp_path, "clone", str(origin), str(agent_a), check=True)
        _git(agent_a, "config", "commit.gpgsign", "false")
        _git(agent_a, "checkout", "-b", "agent/a", "--quiet")
        (agent_a / "new_file.py").write_text("def hello():\n    return 42\n")
        _git(agent_a, "add", "new_file.py")
        _git(agent_a, "commit", "-m", "WIP: ralph output", "--quiet")

        # Capture the patch the way ``_capture_and_persist_ralph_patch``
        # does — ``git format-patch -1 HEAD --stdout``. Routed through
        # ``_git`` so the hermetic env (no user.email / user.name lookup)
        # propagates — CI runners don't set a global identity, and
        # format-patch inherits the committer env from the caller.
        patch_bytes = _git(
            agent_a, "format-patch", "-1", "HEAD", "--stdout", check=True
        ).stdout
        assert "new_file.py" in patch_bytes
        assert "WIP: ralph output" in patch_bytes

        # "Agent B" worktree: fresh clone off origin/main; apply patch
        # via ``git am`` the way ``_apply_prior_ralph_patch`` does.
        agent_b = tmp_path / "agent-b"
        _git(tmp_path, "clone", str(origin), str(agent_b), check=True)
        _git(agent_b, "config", "commit.gpgsign", "false")
        _git(agent_b, "checkout", "-b", "agent/b", "--quiet")
        patch_file = agent_b / "prior-ralph.patch"
        patch_file.write_text(patch_bytes)

        # ``git am`` re-uses committer/author from the patch header but
        # still requires a default identity to be resolvable. Route
        # through ``_git`` so the test env's GIT_AUTHOR_* /
        # GIT_COMMITTER_* envs apply — matches the hermetic pattern in
        # test_daemon_amend_commit.py and avoids the "empty ident name"
        # failure on CI runners that don't set a global git identity.
        am = _git(agent_b, "am", "--3way", str(patch_file), check=False)
        assert am.returncode == 0, (
            f"git am failed: stderr={am.stderr!r} stdout={am.stdout!r}"
        )
        # The inherited diff landed on agent-b's branch.
        assert (agent_b / "new_file.py").exists()
        log = _git(agent_b, "log", "-1", "--format=%s").stdout.strip()
        assert log == "WIP: ralph output"
