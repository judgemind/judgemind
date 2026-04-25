"""AC4 boot-recovery integration tests for DB-backed phase-output materialization.

Issue #2975: every spawn site in :class:`~dispatcher.daemon.DispatcherDaemon`
must read prior phase outputs from ``dispatcher.phase_outputs`` via
:meth:`_fetch_phase_output` and write them to disk **before** spawning the
subprocess.  These tests synthesize an agent whose ``worktree/tmp/`` is
completely empty (simulating a daemon restart where the in-memory caches were
cleared) and confirm that each spawn site correctly materializes the expected
files from the DB.

Covered spawn sites
-------------------
* :meth:`_run_ralph_phase` — materializes ``plan.json`` before ralph spawn.
* :meth:`_run_summary_phase` — materializes ``plan.json`` and ``ralph.json``
  before summary spawn.
* :meth:`_push_and_open_pr` — materializes ``summary.json`` before the
  mechanical push/PR phase.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


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


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection]:
    logger = logging.getLogger(f"dispatcher.test.materialize.{id(tmp_path)}")
    logger.handlers = []
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
    return d, conn


# ---------------------------------------------------------------------------
# Canonical phase-output payloads used across the tests
# ---------------------------------------------------------------------------

_PLAN_OUTPUT: dict[str, Any] = {
    "plan_text": "## Plan\n\nDo the thing.",
    "change_type": "feature",
    "files_to_touch": ["scripts/dispatcher/daemon.py"],
    "dependencies_to_install": [],
}

_RALPH_OUTPUT: dict[str, Any] = {
    "verdict": "SHIP",
    "summary": "Implemented the thing.",
    "changed_files": ["scripts/dispatcher/daemon.py"],
    "iterations_used": 1,
}

_SUMMARY_OUTPUT: dict[str, Any] = {
    "commit_message": "feat(dispatcher): boot-recovery test (#2975)",
    "pr_title": "feat(dispatcher): boot-recovery test (#2975)",
    "pr_body_md": "## Summary\n\nBoot-recovery test.",
    "unmet_criteria": [],
}


# ---------------------------------------------------------------------------
# AC4 — ralph spawn materializes plan.json from DB into empty worktree
# ---------------------------------------------------------------------------


class TestRalphSpawnMaterializesFromDB:
    """Calling ``_run_ralph_phase`` on an empty worktree/tmp/ must:
    1. Call ``_fetch_phase_output(agent_id, "plan")`` to read from DB.
    2. Write ``worktree/tmp/dispatcher-output/plan.json`` with the DB content.
    3. Include ``plan`` in the ralph input bundle written to
       ``worktree/tmp/dispatcher-input/ralph.json``.
    """

    def test_plan_json_written_to_disk_from_db(self, tmp_path: Path) -> None:
        """plan.json is absent before the call and present (with DB content)
        after ``_run_ralph_phase`` returns."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Deliberately empty — simulates post-restart state.
        assert not (worktree / "tmp").exists()

        # Stub helpers that touch subprocesses / DB writes.
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._apply_prior_ralph_patch = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value=_RALPH_OUTPUT
        )
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        # DB supplies the plan output — real _materialize_phase_output runs.
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value=_PLAN_OUTPUT
        )

        d._run_ralph_phase("agent-materialize-0001", 2975, worktree)

        plan_json_path = worktree / "tmp" / "dispatcher-output" / "plan.json"
        assert plan_json_path.exists(), (
            "plan.json must be written by _run_ralph_phase even when "
            "worktree/tmp/ started empty (boot-recovery scenario)"
        )
        written = json.loads(plan_json_path.read_text())
        assert written == _PLAN_OUTPUT, (
            f"plan.json content should match DB payload; got {written!r}"
        )

    def test_ralph_input_json_embeds_plan_from_db(self, tmp_path: Path) -> None:
        """The ralph input bundle written to dispatcher-input/ralph.json
        must carry ``plan`` equal to what ``_fetch_phase_output`` returned."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._apply_prior_ralph_patch = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=_RALPH_OUTPUT)  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value=_PLAN_OUTPUT
        )

        d._run_ralph_phase("agent-materialize-0002", 2975, worktree)

        ralph_input_path = worktree / "tmp" / "dispatcher-input" / "ralph.json"
        assert ralph_input_path.exists(), (
            "dispatcher-input/ralph.json must be written by _run_ralph_phase"
        )
        bundle = json.loads(ralph_input_path.read_text())
        assert bundle.get("plan") == _PLAN_OUTPUT, (
            f"ralph input bundle must embed the DB plan; got plan={bundle.get('plan')!r}"
        )

    def test_fetch_phase_output_called_with_correct_args(self, tmp_path: Path) -> None:
        """``_fetch_phase_output`` is called with the agent_id and ``"plan"``
        — not a stale in-memory attribute."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._apply_prior_ralph_patch = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=_RALPH_OUTPUT)  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        fetch_mock = MagicMock(return_value=_PLAN_OUTPUT)
        d._fetch_phase_output = fetch_mock  # type: ignore[method-assign]

        d._run_ralph_phase("agent-materialize-0003", 2975, worktree)

        # At least one call must be (agent_id, "plan").
        plan_calls = [
            c
            for c in fetch_mock.call_args_list
            if c.args == ("agent-materialize-0003", "plan")
        ]
        assert plan_calls, (
            f"Expected _fetch_phase_output('agent-materialize-0003', 'plan') call; "
            f"got calls: {fetch_mock.call_args_list}"
        )


# ---------------------------------------------------------------------------
# Summary spawn — plan.json and ralph.json materialized from DB
# ---------------------------------------------------------------------------


class TestSummarySpawnMaterializesFromDB:
    """Calling ``_run_summary_phase`` on an empty worktree/tmp/ must materialize
    both plan.json and ralph.json from DB before spawning summary.
    """

    def _patch_summary_helpers(
        self,
        d: daemon.DispatcherDaemon,
        phase_outputs: dict[str, dict[str, Any]],
    ) -> None:
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 2975,
                "issue_title": "Boot-recovery test",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        # _run_summary_phase reads summary output from the subprocess via
        # _read_phase_output; feed a minimal SHIP output so it advances.
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "verdict": "SHIP",
                "commit_message": "feat(dispatcher): boot (#2975)",
                "pr_title": "feat(dispatcher): boot (#2975)",
                "pr_body_md": "body",
                "unmet_criteria": [],
            }
        )
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda _aid, phase: phase_outputs.get(phase)
        )

    def test_plan_and_ralph_json_written_from_db(self, tmp_path: Path) -> None:
        """Both plan.json and ralph.json are absent before the call and
        present (with DB content) after ``_run_summary_phase`` returns."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        assert not (worktree / "tmp").exists()

        self._patch_summary_helpers(
            d,
            phase_outputs={"plan": _PLAN_OUTPUT, "ralph": _RALPH_OUTPUT},
        )

        # Stub subprocess.run so the git diff calls inside
        # _run_summary_phase don't touch the real fs.
        import unittest.mock

        with unittest.mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], returncode=0, stdout="", stderr=""
            ),
        ):
            d._run_summary_phase("agent-sum-0001", 2975, worktree)

        output_dir = worktree / "tmp" / "dispatcher-output"
        plan_path = output_dir / "plan.json"
        ralph_path = output_dir / "ralph.json"

        assert plan_path.exists(), "plan.json must be materialized before summary spawn"
        assert ralph_path.exists(), (
            "ralph.json must be materialized before summary spawn"
        )

        written_plan = json.loads(plan_path.read_text())
        written_ralph = json.loads(ralph_path.read_text())

        assert written_plan == _PLAN_OUTPUT, f"plan.json mismatch: got {written_plan!r}"
        assert written_ralph == _RALPH_OUTPUT, (
            f"ralph.json mismatch: got {written_ralph!r}"
        )

    def test_summary_input_json_embeds_ralph_summary_from_db(
        self, tmp_path: Path
    ) -> None:
        """The summary input bundle must carry the ralph ``summary`` field
        derived from the DB-fetched ralph output."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        self._patch_summary_helpers(
            d,
            phase_outputs={"plan": _PLAN_OUTPUT, "ralph": _RALPH_OUTPUT},
        )

        import unittest.mock

        with unittest.mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], returncode=0, stdout="", stderr=""
            ),
        ):
            d._run_summary_phase("agent-sum-0002", 2975, worktree)

        summary_input_path = worktree / "tmp" / "dispatcher-input" / "summary.json"
        assert summary_input_path.exists(), (
            "dispatcher-input/summary.json must be written by _run_summary_phase"
        )
        bundle = json.loads(summary_input_path.read_text())
        assert bundle.get("ralph_summary") == _RALPH_OUTPUT["summary"], (
            f"summary input must embed ralph summary from DB; "
            f"got ralph_summary={bundle.get('ralph_summary')!r}"
        )


# ---------------------------------------------------------------------------
# push_and_pr spawn — summary.json materialized from DB
# ---------------------------------------------------------------------------


class TestPushAndPrMaterializesFromDB:
    """Calling ``_push_and_open_pr`` on an empty worktree/tmp/ must
    materialize summary.json from DB before the push/PR mechanics run.
    """

    def test_summary_json_written_from_db(self, tmp_path: Path) -> None:
        """summary.json is absent before the call and present (with DB
        content) after ``_push_and_open_pr`` runs the fast-path where
        rev-list returns > 0 commits ahead."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Deliberately empty — no tmp/ directory at all.
        assert not (worktree / "tmp").exists()

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._delete_ralph_patches_for_agent = MagicMock()  # type: ignore[method-assign]
        # DB supplies the summary output — real _materialize_phase_output runs.
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value=_SUMMARY_OUTPUT
        )

        # Stub subprocesses: rev-list says 1 ahead, then commit, show,
        # push, and gh pr create all succeed.
        import unittest.mock

        with unittest.mock.patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(
                    ["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["git", "show"], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["git", "push"], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["gh", "pr", "create"],
                    returncode=0,
                    stdout=("https://github.com/judgemind/judgemind/pull/2975\n"),
                    stderr="",
                ),
            ],
        ):
            d._push_and_open_pr(
                agent_id="agent-pr-materialize-0001",
                issue_number=2975,
                worktree=worktree,
            )

        summary_path = worktree / "tmp" / "dispatcher-output" / "summary.json"
        assert summary_path.exists(), (
            "summary.json must be materialized by _push_and_open_pr even when "
            "worktree/tmp/ started empty (boot-recovery scenario)"
        )
        written = json.loads(summary_path.read_text())
        assert written == _SUMMARY_OUTPUT, (
            f"summary.json content should match DB payload; got {written!r}"
        )

    def test_fetch_phase_output_called_for_summary(self, tmp_path: Path) -> None:
        """``_fetch_phase_output`` is called with ``"summary"`` so the
        commit message and PR metadata are sourced from DB, not from a
        deleted in-memory attribute."""
        d, _conn = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._delete_ralph_patches_for_agent = MagicMock()  # type: ignore[method-assign]
        fetch_mock = MagicMock(return_value=_SUMMARY_OUTPUT)
        d._fetch_phase_output = fetch_mock  # type: ignore[method-assign]

        import unittest.mock

        with unittest.mock.patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(
                    ["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["git", "show"], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["git", "push"], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    ["gh", "pr", "create"],
                    returncode=0,
                    stdout=("https://github.com/judgemind/judgemind/pull/2975\n"),
                    stderr="",
                ),
            ],
        ):
            d._push_and_open_pr(
                agent_id="agent-pr-materialize-0002",
                issue_number=2975,
                worktree=worktree,
            )

        summary_calls = [
            c
            for c in fetch_mock.call_args_list
            if c.args == ("agent-pr-materialize-0002", "summary")
        ]
        assert summary_calls, (
            f"Expected _fetch_phase_output('agent-pr-materialize-0002', 'summary') "
            f"call; got calls: {fetch_mock.call_args_list}"
        )
