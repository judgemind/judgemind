"""Unit tests for summary-originated ``AC_INFEASIBLE`` verdict handling (#3010).

Covers:

* :meth:`daemon.DispatcherDaemon._run_summary_phase` writes a
  ``dispatcher.failures(category='summary_ac_infeasible')`` row with
  ``details`` containing ``infeasible_acs`` + ``deferred_acs`` +
  ``ralph_diff`` + ``summary_ac_mapping`` + ``agent_id`` +
  ``issue_number`` when summary's ``verdict == 'AC_INFEASIBLE'``.
* push_and_pr is NOT advanced to (method returns False).
* OK verdict + empty ``unmet_criteria`` still advances to push_and_pr
  (regression guard so the AC_INFEASIBLE branch doesn't swallow the
  happy path).
* ``summary_ac_infeasible`` is in :data:`daemon.TIER_3_CATEGORIES`.
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
    logger = logging.getLogger(f"dispatcher.test.summary_ac_infeasible.{id(tmp_path)}")
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


def _patch_summary_helpers(
    d: daemon.DispatcherDaemon, *, summary_output: dict[str, Any]
) -> None:
    """Stub the helpers _run_summary_phase relies on so the test doesn't
    need a real subprocess or issue-bundle fetch. The ralph-side output
    stash (``_agent_ralph_output``) is set to a minimal SHIP dict so
    the summary phase sees the expected predecessor state."""
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._write_phase_input = MagicMock()  # type: ignore[method-assign]
    d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._read_phase_output = MagicMock(return_value=summary_output)  # type: ignore[method-assign]
    d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
    d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
    d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._write_failure = MagicMock()  # type: ignore[method-assign]
    # Pretend ralph shipped so the summary phase has the expected
    # precursor state. The real summary_input build reads
    # ``_agent_ralph_output.summary`` and ``changed_files`` from here.
    d._agent_ralph_output = {
        "verdict": "SHIP",
        "summary": "ralph shipped something",
        "changed_files": ["scripts/foo.py"],
    }
    d._agent_plan_output = {"acceptance_criteria": [], "scope_check": []}
    # Issue bundle fetch — short-circuit.
    d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
        return_value={
            "issue_number": 3010,
            "issue_title": "Test issue",
            "issue_body": "",
            "issue_comments": [],
            "issue_labels": [],
            "blocked_by": [],
            "parent_issue": None,
        }
    )


# --------------------------------------------------------------------------
# Tier-3 registration
# --------------------------------------------------------------------------


class TestTierRegistration:
    def test_summary_ac_infeasible_is_tier_3(self) -> None:
        """``summary_ac_infeasible`` diagnoses on first occurrence."""
        assert daemon.FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE in daemon.TIER_3_CATEGORIES

    def test_summary_ac_infeasible_not_in_auto_retry(self) -> None:
        """Summary-AC-infeasible is NOT auto-retried — the diagnoser
        must decide reissue vs. escalate vs. close."""
        assert (
            daemon.FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE
            not in daemon.AUTO_RETRY_CATEGORIES
        )


# --------------------------------------------------------------------------
# _run_summary_phase behavior on verdict=AC_INFEASIBLE
# --------------------------------------------------------------------------


class TestRunSummaryPhaseAcInfeasible:
    def test_ac_infeasible_writes_failure_with_full_bundle(
        self, tmp_path: Path
    ) -> None:
        """Summary's AC_INFEASIBLE writes a failure row whose details
        carry the full diagnoser-context prelude: ``infeasible_acs``,
        ``deferred_acs``, ``ralph_diff``, ``summary_ac_mapping``."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_summary_helpers(
            d,
            summary_output={
                "verdict": "AC_INFEASIBLE",
                "process_summary_md": "## Process Summary\n\n…",
                "commit_message": "feat: wip",
                "pr_title": "feat: wip",
                "pr_body_md": "## Summary\n…",
                "deferred_acs": [
                    {
                        "index": 5,
                        "reason": "marker",
                        "verify_instruction": "Verify: (post-deploy) query OS",
                    }
                ],
                "infeasible_acs": [
                    {
                        "index": 3,
                        "evidence": (
                            "AC #3 requires a column `confidence_score` "
                            "that does not exist on `derived.rulings`."
                        ),
                    }
                ],
                "unmet_criteria": [],
                "ac_mapping": [
                    {
                        "index": 1,
                        "criterion": "c1",
                        "status": "met",
                        "evidence": "path/to/file.py",
                    },
                    {
                        "index": 3,
                        "criterion": "c3",
                        "status": "infeasible",
                        "evidence": "confidence_score does not exist",
                    },
                    {
                        "index": 5,
                        "criterion": "c5",
                        "status": "deferred",
                        "evidence": "post-deploy marker",
                    },
                ],
            },
        )

        ok = d._run_summary_phase("agent-sum-inf", 3010, worktree)

        # Summary does not advance — _push_and_open_pr would have been
        # called otherwise by the caller; _run_summary_phase returns
        # False so the caller aborts.
        assert ok is False
        # Failure row written with the right category and full bundle.
        d._write_failure.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = d._write_failure.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["agent_id"] == "agent-sum-inf"
        assert call_kwargs["category"] == daemon.FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE
        assert call_kwargs["detected_by"] == "summary_output_parse"
        details = call_kwargs["details"]
        assert details["agent_id"] == "agent-sum-inf"
        assert details["issue_number"] == 3010
        # The infeasible_acs list is present and normalized.
        assert len(details["infeasible_acs"]) == 1
        assert details["infeasible_acs"][0]["index"] == 3
        assert "confidence_score" in details["infeasible_acs"][0]["evidence"]
        # The deferred list is carried forward so the diagnoser can
        # distinguish deferred vs infeasible.
        assert len(details["deferred_acs"]) == 1
        assert details["deferred_acs"][0]["index"] == 5
        # ralph_diff is present (empty string because we stubbed
        # subprocess; the key exists).
        assert "ralph_diff" in details
        # summary_ac_mapping preserves summary's per-AC classification.
        assert len(details["summary_ac_mapping"]) == 3
        # Agent marked failed at the summary phase.
        d._mark_agent_terminal.assert_called_once()  # type: ignore[union-attr]
        terminal_kwargs = d._mark_agent_terminal.call_args.kwargs  # type: ignore[union-attr]
        assert terminal_kwargs["status"] == "failed"
        assert terminal_kwargs["phase"] == "summary"
        assert terminal_kwargs["issue_number"] == 3010
        # Observability event.
        events = handler.events("summary_ac_infeasible")
        assert events
        assert getattr(events[0], "infeasible_acs_count", None) == 1

    def test_ok_verdict_still_advances_with_no_unmet(self, tmp_path: Path) -> None:
        """Regression guard — the AC_INFEASIBLE branch must not
        short-circuit the OK path."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_summary_helpers(
            d,
            summary_output={
                "verdict": "OK",
                "process_summary_md": "ok",
                "commit_message": "feat: ship",
                "pr_title": "feat: ship",
                "pr_body_md": "summary",
                "unmet_criteria": [],
                "deferred_acs": [],
                "infeasible_acs": [],
            },
        )

        ok = d._run_summary_phase("agent-ok", 3010, worktree)

        assert ok is True
        d._write_failure.assert_not_called()  # type: ignore[union-attr]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        assert d._agent_summary_output is not None
        assert str(d._agent_summary_output.get("verdict")).upper() == "OK"

    def test_ok_with_unmet_takes_needs_review_path_not_ac_infeasible(
        self, tmp_path: Path
    ) -> None:
        """Non-empty `unmet_criteria` continues to route to the draft-PR
        needs_review flow (issue #2856) — it must NOT accidentally land
        on the AC_INFEASIBLE path just because it's a failure mode
        adjacent to it."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_summary_helpers(
            d,
            summary_output={
                "verdict": "OK",
                "process_summary_md": "ok",
                "commit_message": "feat: ship",
                "pr_title": "feat: ship",
                "pr_body_md": "summary",
                "unmet_criteria": ["shape-mismatch AC"],
                "deferred_acs": [],
                "infeasible_acs": [],
            },
        )

        ok = d._run_summary_phase("agent-needs-review", 3010, worktree)

        # OK path returns True and _push_and_open_pr runs elsewhere
        # (the draft-PR flow).
        assert ok is True
        d._write_failure.assert_not_called()  # type: ignore[union-attr]
        assert d._agent_unmet_criteria == ["shape-mismatch AC"]

    def test_build_diagnoser_context_bundles_summary_extras(
        self, tmp_path: Path
    ) -> None:
        """Issue #3010 AC #6 integration-style check — the
        ``summary_ac_infeasible`` context bundle carries the
        summary-specific extras: ``ralph_diff``, ``summary_ac_mapping``,
        ``deferred_acs``, alongside the shared ``infeasible_acs`` +
        ``issue_acceptance_criteria`` fields.

        End-to-end: summary emits AC_INFEASIBLE → daemon writes a
        ``summary_ac_infeasible`` failure row whose details carry the
        full salvage context → supervisor tick lifts the candidate and
        calls ``_build_diagnoser_context`` → JSONB lands on
        ``dispatcher.diagnoses.context``. This test locks the middle
        two steps.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            # agent row: (issue_number, worktree_path, pr_number)
            (3010, "/tmp/wt", None),
        ]
        conn.cursor_instance.fetchall_queue = [
            [],  # phase_transitions
            [],  # prior_failures
        ]
        import subprocess as _sp

        class _FakeResult:
            returncode = 0
            stdout = (
                '{"title": "Test issue 3010", '
                '"body": "## Acceptance criteria\\n'
                "- [ ] AC one\\n"
                "- [ ] AC two\\n"
                '- [ ] AC three (infeasible)\\n"}'
            )

        original_run = _sp.run
        _sp.run = lambda *a, **kw: _FakeResult()  # type: ignore[assignment]

        try:
            candidate = {
                "failure_id": 99,
                "agent_id": "agent-sum",
                "category": daemon.FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
                "tier": 3,
                "issue_number": 3010,
                "details": {
                    "infeasible_acs": [
                        {
                            "index": 3,
                            "evidence": "AC #3 demands a non-existent column",
                        }
                    ],
                    "deferred_acs": [
                        {
                            "index": 2,
                            "reason": "marker",
                            "verify_instruction": "Verify: (post-deploy) curl",
                        }
                    ],
                    "ralph_diff": (
                        "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
                    ),
                    "summary_ac_mapping": [
                        {
                            "index": 1,
                            "criterion": "AC one",
                            "status": "met",
                            "evidence": "file.py::test_one",
                        },
                        {
                            "index": 3,
                            "criterion": "AC three",
                            "status": "infeasible",
                            "evidence": "non-existent column",
                        },
                    ],
                    "agent_id": "agent-sum",
                    "issue_number": 3010,
                },
                "failure_ts": None,
            }
            bundle = d._build_diagnoser_context(candidate)
        finally:
            _sp.run = original_run  # type: ignore[assignment]

        # Shared core fields.
        assert bundle["agent_id"] == "agent-sum"
        assert bundle["failure_id"] == 99
        assert bundle["failure_category"] == (
            daemon.FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE
        )
        assert bundle["tier"] == 3
        # Shared AC-infeasibility fields (same as ralph path).
        assert "infeasible_acs" in bundle
        assert bundle["infeasible_acs"][0]["index"] == 3
        assert "issue_acceptance_criteria" in bundle
        assert len(bundle["issue_acceptance_criteria"]) == 3
        # Summary-specific extras — present only on this category.
        assert "deferred_acs" in bundle
        assert bundle["deferred_acs"][0]["index"] == 2
        assert bundle["deferred_acs"][0]["reason"] == "marker"
        assert "ralph_diff" in bundle
        assert "old" in bundle["ralph_diff"]
        assert "new" in bundle["ralph_diff"]
        assert "summary_ac_mapping" in bundle
        assert len(bundle["summary_ac_mapping"]) == 2
        assert bundle["summary_ac_mapping"][1]["status"] == "infeasible"

    def test_ac_infeasible_without_infeasible_acs_still_routes(
        self, tmp_path: Path
    ) -> None:
        """Defensive — if summary emits verdict=AC_INFEASIBLE but the
        list is empty/missing (skill contract violation), the daemon
        still writes a failure row (empty list) so the diagnoser can
        escalate. Silently dropping the verdict would be worse."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_summary_helpers(
            d,
            summary_output={
                "verdict": "AC_INFEASIBLE",
                "process_summary_md": "…",
                "commit_message": "",
                "pr_title": "",
                "pr_body_md": "",
                # no infeasible_acs key
                "unmet_criteria": [],
            },
        )

        ok = d._run_summary_phase("agent-empty", 3010, worktree)

        assert ok is False
        d._write_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._write_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["infeasible_acs"] == []
