"""Unit tests for ralph-originated ``AC_INFEASIBLE`` verdict handling (#3010).

Covers:

* :meth:`daemon.DispatcherDaemon._run_ralph_phase` writes a
  ``dispatcher.failures(category='ralph_ac_infeasible')`` row with
  ``details={infeasible_acs, agent_id, issue_number}`` when ralph's
  ``verdict == 'AC_INFEASIBLE'``.
* The summary phase is NOT advanced in that case (method returns False).
* A SHIP verdict still advances to summary (regression guard so the
  AC_INFEASIBLE branch doesn't accidentally short-circuit the happy
  path).
* ``ralph_ac_infeasible`` is in :data:`daemon.TIER_3_CATEGORIES` so the
  diagnoser candidate scan picks it up on first occurrence with no
  mechanical retry.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_prior_attempts.py)
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
    logger = logging.getLogger(f"dispatcher.test.ralph_ac_infeasible.{id(tmp_path)}")
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


def _patch_ralph_helpers(
    d: daemon.DispatcherDaemon, *, ralph_output: dict[str, Any]
) -> None:
    """Patch the helpers _run_ralph_phase calls so the test doesn't need
    real subprocesses, DB, or filesystem state."""
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._write_phase_input = MagicMock()  # type: ignore[method-assign]
    d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._read_phase_output = MagicMock(return_value=ralph_output)  # type: ignore[method-assign]
    d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
    d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
    d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._write_failure = MagicMock()  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# Tier-3 registration
# --------------------------------------------------------------------------


class TestTierRegistration:
    def test_ralph_ac_infeasible_is_tier_3(self) -> None:
        """``ralph_ac_infeasible`` diagnoses on first occurrence — no retry."""
        assert daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE in daemon.TIER_3_CATEGORIES

    def test_ralph_ac_infeasible_not_in_auto_retry(self) -> None:
        """Ralph-AC-infeasible is NOT auto-retried — a mechanical retry
        will hit the same structural wall. The diagnoser must decide
        reissue vs. escalate vs. close."""
        assert (
            daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE
            not in daemon.AUTO_RETRY_CATEGORIES
        )


# --------------------------------------------------------------------------
# _run_ralph_phase behavior on verdict=AC_INFEASIBLE
# --------------------------------------------------------------------------


class TestRunRalphPhaseAcInfeasible:
    def test_ac_infeasible_writes_failure_row(self, tmp_path: Path) -> None:
        """ralph's AC_INFEASIBLE verdict writes
        ``dispatcher.failures(category='ralph_ac_infeasible')`` with
        the full infeasible_acs list in details."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "AC_INFEASIBLE",
                "iterations_used": 2,
                "block_reason": None,
                "changed_files": [],
                "infeasible_acs": [
                    {
                        "index": 1,
                        "evidence": (
                            "AC #1 references the `--court` flag on "
                            "scripts/rebuild_db.py but `grep -n` returns no "
                            "match and the PR diff does not add it."
                        ),
                    },
                    {
                        "index": 4,
                        "evidence": (
                            "AC #4 expects a `derived.enrichment_v2` table "
                            "but the schema migration owning that work is "
                            "sibling open issue #9999."
                        ),
                    },
                ],
                "summary": "ralph declined to SHIP — 2 ACs structurally infeasible.",
            },
        )

        ok = d._run_ralph_phase("agent-ac-inf", 3010, worktree)

        # Ralph short-circuits — summary must not be advanced to.
        assert ok is False
        # _write_failure was called with the right category + details.
        d._write_failure.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = d._write_failure.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["agent_id"] == "agent-ac-inf"
        assert call_kwargs["category"] == daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE
        assert call_kwargs["detected_by"] == "ralph_output_parse"
        details = call_kwargs["details"]
        assert details["agent_id"] == "agent-ac-inf"
        assert details["issue_number"] == 3010
        assert len(details["infeasible_acs"]) == 2
        assert details["infeasible_acs"][0]["index"] == 1
        assert "rebuild_db" in details["infeasible_acs"][0]["evidence"]
        assert details["infeasible_acs"][1]["index"] == 4
        # Agent marked failed at the ralph phase.
        d._mark_agent_terminal.assert_called_once()  # type: ignore[union-attr]
        terminal_kwargs = d._mark_agent_terminal.call_args.kwargs  # type: ignore[union-attr]
        assert terminal_kwargs["status"] == "failed"
        assert terminal_kwargs["phase"] == "ralph"
        assert terminal_kwargs["issue_number"] == 3010
        # Observability — ralph_ac_infeasible event logged.
        events = handler.events("ralph_ac_infeasible")
        assert events
        assert getattr(events[0], "infeasible_acs_count", None) == 2

    def test_ship_verdict_still_advances_to_summary(self, tmp_path: Path) -> None:
        """Regression guard — the AC_INFEASIBLE branch must not
        short-circuit the SHIP path. A SHIP verdict advances to summary
        exactly like today."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "SHIP",
                "iterations_used": 1,
                "block_reason": None,
                "changed_files": ["docs/example.md"],
                "infeasible_acs": [],
                "summary": "Implemented cleanly.",
            },
        )

        ok = d._run_ralph_phase("agent-ship", 3010, worktree)

        assert ok is True
        # No failure row written, no terminal mark.
        d._write_failure.assert_not_called()  # type: ignore[union-attr]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        # Ralph output stashed for summary phase.
        assert d._agent_ralph_output is not None
        assert d._agent_ralph_output.get("verdict") == "SHIP"

    def test_blocked_verdict_does_not_write_ac_infeasible_failure(
        self, tmp_path: Path
    ) -> None:
        """BLOCKED is a different failure mode — not AC_INFEASIBLE.
        The ``ralph_not_ship`` routing (issue #3054) owns it. This test
        locks down that the AC_INFEASIBLE branch doesn't accidentally
        swallow BLOCKED verdicts — the failure row, if any, must carry
        ``category='ralph_not_ship'``, never ``'ralph_ac_infeasible'``.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "BLOCKED",
                "iterations_used": 5,
                "block_reason": "max_iterations reached without SHIP",
                "changed_files": [],
                "summary": "ralph hit max iterations.",
            },
        )

        ok = d._run_ralph_phase("agent-blocked", 3010, worktree)

        assert ok is False
        # BLOCKED flows through the unified _handle_agent_failure path
        # with category='ralph_not_ship' (#3054). Under the hood that
        # calls _write_failure, but with a DIFFERENT category than the
        # AC_INFEASIBLE path — assert category, not call-count.
        assert d._write_failure.call_count <= 1  # type: ignore[union-attr]
        for call in d._write_failure.call_args_list:  # type: ignore[union-attr]
            assert (
                call.kwargs.get("category")
                != daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE
            )
        # Agent still marked failed (the not-SHIP path) — _handle_agent_failure
        # calls _mark_agent_terminal internally.
        d._mark_agent_terminal.assert_called_once()  # type: ignore[union-attr]

    def test_malformed_infeasible_acs_list_is_sanitized(self, tmp_path: Path) -> None:
        """Defensive parsing — a malformed `infeasible_acs` entry (e.g.
        a plain string instead of a dict) is skipped so the downstream
        failure row / diagnoser bundle always has a clean shape."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "AC_INFEASIBLE",
                "iterations_used": 1,
                "block_reason": None,
                "changed_files": [],
                "infeasible_acs": [
                    "a stray string, not a dict",  # malformed
                    {"index": "not-an-int", "evidence": "bad index shape"},
                    {"index": 2, "evidence": "genuine entry"},
                ],
                "summary": "mixed shape",
            },
        )

        ok = d._run_ralph_phase("agent-mix", 3010, worktree)

        assert ok is False
        call_kwargs = d._write_failure.call_args.kwargs  # type: ignore[union-attr]
        entries = call_kwargs["details"]["infeasible_acs"]
        # Stray string dropped; "not-an-int" coerced to None; genuine
        # entry preserved.
        assert len(entries) == 2
        assert entries[0]["index"] is None
        assert entries[1]["index"] == 2
        assert entries[1]["evidence"] == "genuine entry"

    def test_build_diagnoser_context_bundles_infeasible_acs(
        self, tmp_path: Path
    ) -> None:
        """Integration-ish test of the diagnoser context bundle for
        ``ralph_ac_infeasible``. Exercises
        :meth:`daemon.DispatcherDaemon._build_diagnoser_context` against
        a candidate dict mirroring what the post-exit parse path
        produces — the bundle must carry ``infeasible_acs`` +
        ``issue_acceptance_criteria`` so the diagnoser skill can
        correlate index → criterion text without a re-fetch.

        This is the issue #3010 AC #6 "full ralph_ac_infeasible flow
        through the DB" verification — rather than spinning up pg, we
        drive :meth:`_build_diagnoser_context` (the function that
        materializes the context JSONB) with a candidate that matches
        what :meth:`_run_ralph_phase` writes via :meth:`_write_failure`.
        End-to-end: ralph emits AC_INFEASIBLE → daemon writes failure
        row with ``details.infeasible_acs`` → supervisor tick lifts the
        candidate and calls _build_diagnoser_context → JSONB lands on
        ``dispatcher.diagnoses.context`` → diagnoser skill reads it.
        This test locks the middle two steps.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        # Stub DB-read helpers the real context builder uses so we don't
        # need a live pg. The test verifies the bundle-assembly logic,
        # not the DB plumbing (covered by test_daemon_phase3d.py).
        conn.cursor_instance.fetch_queue = [
            # agent row: (issue_number, worktree_path, pr_number)
            (3010, "/tmp/wt", None),
        ]
        conn.cursor_instance.fetchall_queue = [
            [],  # phase_transitions
            [],  # prior_failures
        ]
        # Subprocess call for issue body — short-circuit to empty.
        import subprocess as _sp

        class _FakeResult:
            returncode = 0
            stdout = (
                '{"title": "Test issue 3010", '
                '"body": "## Acceptance criteria\\n'
                "- [ ] First AC\\n"
                "- [ ] Second AC\\n"
                '- [ ] Third AC\\n"}'
            )

        original_run = _sp.run

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            return _FakeResult()

        _sp.run = fake_run  # type: ignore[assignment]

        try:
            candidate = {
                "failure_id": 42,
                "agent_id": "agent-test",
                "category": daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
                "tier": 3,
                "issue_number": 3010,
                "details": {
                    "infeasible_acs": [
                        {
                            "index": 2,
                            "evidence": (
                                "AC #2 references a `--court` flag that "
                                "does not exist on scripts/rebuild_db.py."
                            ),
                        }
                    ],
                    "agent_id": "agent-test",
                    "issue_number": 3010,
                },
                "failure_ts": None,
            }
            bundle = d._build_diagnoser_context(candidate)
        finally:
            _sp.run = original_run  # type: ignore[assignment]

        # Core envelope — same fields every diagnosis has.
        assert bundle["agent_id"] == "agent-test"
        assert bundle["failure_id"] == 42
        assert bundle["failure_category"] == (
            daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE
        )
        assert bundle["tier"] == 3
        assert bundle["issue_number"] == 3010
        # #3010-specific — infeasible_acs carried through.
        assert "infeasible_acs" in bundle
        assert len(bundle["infeasible_acs"]) == 1
        assert bundle["infeasible_acs"][0]["index"] == 2
        assert "rebuild_db" in bundle["infeasible_acs"][0]["evidence"]
        # #3010-specific — issue_acceptance_criteria threaded through
        # so the diagnoser can correlate index → criterion text.
        assert "issue_acceptance_criteria" in bundle
        assert bundle["issue_acceptance_criteria"] == [
            "First AC",
            "Second AC",
            "Third AC",
        ]
        # Summary-specific fields are NOT present on the ralph path —
        # keeps the bundle shape minimal for each category.
        assert "ralph_diff" not in bundle
        assert "summary_ac_mapping" not in bundle
        assert "deferred_acs" not in bundle

    def test_ac_infeasible_missing_list_still_writes_failure(
        self, tmp_path: Path
    ) -> None:
        """If verdict is AC_INFEASIBLE but the list is missing (skill
        contract violation), the daemon still writes a failure row with
        an empty ``infeasible_acs``. The diagnoser can escalate from
        there; silently dropping the signal would be worse."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "AC_INFEASIBLE",
                "iterations_used": 3,
                "block_reason": None,
                "changed_files": [],
                # no infeasible_acs key at all
                "summary": "oops — skill forgot the list",
            },
        )

        ok = d._run_ralph_phase("agent-empty", 3010, worktree)

        assert ok is False
        d._write_failure.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = d._write_failure.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["category"] == daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE
        assert call_kwargs["details"]["infeasible_acs"] == []
