"""Unit tests for summary-emitted ``deferred_acs`` persistence + verify threading (#3010).

Covers:

* :meth:`daemon.DispatcherDaemon._run_summary_phase` persists the whole
  summary output (including ``deferred_acs``) via
  :meth:`_persist_phase_output` when ``verdict == 'OK'`` and proceeds to
  the downstream push_and_pr flow.
* The verify input-bundle build threads ``deferred_acs`` through from
  the persisted summary output, per the spec §6a `^verify-deferred-acs`
  footnote — the verify skill reads this list and runs the deferred ACs
  first post-deploy.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402


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
    logger = logging.getLogger(f"dispatcher.test.summary_deferred_acs.{id(tmp_path)}")
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
# _run_summary_phase — OK verdict persists full output incl. deferred_acs.
# --------------------------------------------------------------------------


class TestSummaryDeferredAcsPersisted:
    def test_deferred_acs_persisted_alongside_summary_output(
        self, tmp_path: Path
    ) -> None:
        """On verdict=OK, the whole summary output (including
        ``deferred_acs``) is persisted to ``dispatcher.phase_outputs``.
        The verify phase reads the same row later to pick up the list."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        summary_output = {
            "verdict": "OK",
            "process_summary_md": "…",
            "commit_message": "feat: x",
            "pr_title": "feat: x",
            "pr_body_md": "…",
            "unmet_criteria": [],
            "deferred_acs": [
                {
                    "index": 5,
                    "reason": "marker",
                    "verify_instruction": (
                        "Verify: (post-deploy) query OS count against dev"
                    ),
                },
                {
                    "index": 7,
                    "reason": "heuristic",
                    "verify_instruction": "Verify: curl dev.api.judgemind.org/foo",
                },
            ],
            "infeasible_acs": [],
        }

        # Stub helpers.
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=summary_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._write_failure = MagicMock()  # type: ignore[method-assign]
        # Mock _fetch_phase_output to return DB-backed predecessor outputs.
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda agent_id, phase: {
                "ralph": {
                    "verdict": "SHIP",
                    "summary": "…",
                    "changed_files": ["scripts/foo.py"],
                },
                "plan": {"acceptance_criteria": [], "scope_check": [], "collapsed_comments": []},
            }.get(phase)
        )
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 3010,
                "issue_title": "",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )

        ok = d._run_summary_phase("agent-def", 3010, worktree)

        assert ok is True
        # _persist_phase_output was called with summary_output that
        # includes deferred_acs verbatim.
        d._persist_phase_output.assert_called_once()  # type: ignore[union-attr]
        persisted_call = d._persist_phase_output.call_args  # type: ignore[union-attr]
        # _persist_phase_output positional args: (agent_id, phase, output_dict, ...)
        persisted_args = persisted_call.args
        assert persisted_args[0] == "agent-def"
        assert persisted_args[1] == "summary"
        persisted_output = persisted_args[2]
        assert persisted_output["deferred_acs"] == summary_output["deferred_acs"]
        # The normal push-and-pr branch is taken (no failure, no
        # terminal, no AC_INFEASIBLE write).
        d._write_failure.assert_not_called()  # type: ignore[union-attr]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        # The summary input written via _write_phase_input must use
        # ``collapsed_comments`` (not ``issue_comments``) — issue #2716.
        summary_writes = [
            call
            for call in d._write_phase_input.call_args_list  # type: ignore[union-attr]
            if len(call.args) >= 2 and call.args[1] == "summary"
        ]
        assert summary_writes, "summary input must be written via _write_phase_input"
        summary_input = summary_writes[0].args[2]
        assert "collapsed_comments" in summary_input, (
            "summary input must carry collapsed_comments (issue #2716)"
        )
        assert "issue_comments" not in summary_input, (
            "summary input must not carry the old issue_comments field"
        )

    def test_ok_verdict_with_empty_deferred_acs_still_persisted(
        self, tmp_path: Path
    ) -> None:
        """Empty deferred_acs is the common case (no post-deploy ACs).
        The key still exists in the persisted output so verify can
        read a stable shape."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        summary_output = {
            "verdict": "OK",
            "process_summary_md": "…",
            "commit_message": "feat: x",
            "pr_title": "feat: x",
            "pr_body_md": "…",
            "unmet_criteria": [],
            "deferred_acs": [],
            "infeasible_acs": [],
        }

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=summary_output)  # type: ignore[method-assign]
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
        # Mock _fetch_phase_output to return DB-backed predecessor outputs.
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda agent_id, phase: {
                "ralph": {"verdict": "SHIP", "summary": "", "changed_files": []},
                "plan": {"acceptance_criteria": [], "scope_check": [], "collapsed_comments": []},
            }.get(phase)
        )
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 3010,
                "issue_title": "",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )

        ok = d._run_summary_phase("agent-empty", 3010, worktree)

        assert ok is True
        persisted_args = d._persist_phase_output.call_args.args  # type: ignore[union-attr]
        assert persisted_args[2]["deferred_acs"] == []


# --------------------------------------------------------------------------
# Verify input-bundle build — threads deferred_acs from persisted summary.
# --------------------------------------------------------------------------


class TestVerifyInputThreadsDeferredAcs:
    """The verify input build reads ``deferred_acs`` from the persisted
    summary output (via ``_fetch_phase_output``) and injects it into
    ``verify.json`` so the verify skill can run those ACs first
    post-deploy."""

    def _make_agent(self) -> dict[str, Any]:
        return {
            "agent_id": "agent-verify",
            "issue_number": 3010,
            "pr_number": 4242,
            "worktree_path": "/tmp/fake-worktree",
        }

    def test_verify_input_contains_deferred_acs_from_summary(
        self, tmp_path: Path
    ) -> None:
        """When the summary output in ``dispatcher.phase_outputs``
        carries a non-empty ``deferred_acs`` list, the verify input
        bundle includes it verbatim."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        agent = self._make_agent()
        agent["worktree_path"] = str(worktree)

        deferred_acs_persisted = [
            {
                "index": 5,
                "reason": "marker",
                "verify_instruction": "Verify: (post-deploy) query OS count",
            }
        ]

        # Stub helpers _spawn_verify_phase relies on.
        d._read_verify_skip_reason = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 3010,
                "issue_title": "Test",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        d._extract_acceptance_criteria = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._select_deploy_status = MagicMock(  # type: ignore[method-assign]
            return_value={
                "workflow_name": "deploy-api.yml",
                "run_id": 1,
                "conclusion": "success",
                "duration_s": 120,
            }
        )
        d._infer_change_type = MagicMock(return_value="api")  # type: ignore[method-assign]
        d._touched_services_from_runs = MagicMock(  # type: ignore[method-assign]
            return_value=["judgemind-api-dev"]
        )
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "verdict": "OK",
                "deferred_acs": deferred_acs_persisted,
            }
        )
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=None)  # type: ignore[method-assign]

        # Contract: during verify-input assembly, the daemon calls
        # ``_write_phase_input(worktree, "verify", {...})`` with a dict
        # that includes the ``deferred_acs`` threaded from the summary
        # output. The verify-spawn entry point is
        # ``_run_verify_and_complete(agent, pr_status, merge_sha,
        # deploy_runs)`` — see daemon.py.
        d._run_verify_and_complete(
            agent, pr_status={}, merge_sha="deadbee", deploy_runs=[]
        )

        # The write_phase_input call for phase="verify" must have
        # included the deferred_acs threaded from the summary output.
        verify_writes = [
            call
            for call in d._write_phase_input.call_args_list  # type: ignore[union-attr]
            if len(call.args) >= 2 and call.args[1] == "verify"
        ]
        assert verify_writes, (
            "verify input must be written via _write_phase_input(worktree, 'verify', ...)"
        )
        verify_input = verify_writes[0].args[2]
        assert "deferred_acs" in verify_input
        assert verify_input["deferred_acs"] == deferred_acs_persisted

    def test_verify_input_no_deferred_acs_when_summary_absent(
        self, tmp_path: Path
    ) -> None:
        """Backwards compatibility — on pre-#3010 agents the summary
        row has no deferred_acs key. The verify input still includes
        the ``deferred_acs`` field (empty list) so the verify skill's
        input contract is stable across agent vintages."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        agent = self._make_agent()
        agent["worktree_path"] = str(worktree)

        d._read_verify_skip_reason = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 3010,
                "issue_title": "",
                "issue_body": "",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        d._extract_acceptance_criteria = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._select_deploy_status = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._infer_change_type = MagicMock(return_value="docs")  # type: ignore[method-assign]
        d._touched_services_from_runs = MagicMock(return_value=[])  # type: ignore[method-assign]
        # Pre-#3010 row — no deferred_acs key.
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={"verdict": "OK"}
        )
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=None)  # type: ignore[method-assign]

        d._run_verify_and_complete(
            agent, pr_status={}, merge_sha="deadbee", deploy_runs=[]
        )

        verify_writes = [
            call
            for call in d._write_phase_input.call_args_list  # type: ignore[union-attr]
            if len(call.args) >= 2 and call.args[1] == "verify"
        ]
        assert verify_writes
        verify_input = verify_writes[0].args[2]
        assert verify_input.get("deferred_acs") == []
