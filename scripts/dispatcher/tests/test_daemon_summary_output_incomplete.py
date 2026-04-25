"""Unit tests for summary_output_incomplete routing (#3062).

Covers the audit follow-up fix found during the #3062 audit: the
``summary_output_incomplete`` terminal in ``_push_and_open_pr``
previously called :meth:`_mark_agent_terminal` directly without writing
a ``dispatcher.failures`` row, bypassing the #3032 unified failure
routing. The audit fix routes through :meth:`_handle_agent_failure`
with ``category='phase_output_missing'`` — the subprocess exited 0 but
the JSON envelope lacked required fields, qualitatively identical to
the ``phase_output_missing`` case handled by existing code.

Mirrors ``test_daemon_ralph_not_ship.py`` style — same helper shape so
a future #3032-class audit finding can copy-paste the pattern.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes
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
    logger = logging.getLogger(
        f"dispatcher.test.summary_output_incomplete.{id(tmp_path)}"
    )
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


def _patch_push_and_open_pr_helpers(
    d: daemon.DispatcherDaemon,
    *,
    summary_output: dict[str, Any],
    is_noop: bool = False,
) -> None:
    """Patch helpers called by ``_push_and_open_pr`` so the test doesn't
    need real subprocesses, DB, or filesystem state.

    Stubs out enough that the summary_output_incomplete branch can fire
    without the surrounding machinery running. ``_handle_agent_failure``
    is the new routing path under test.
    """
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._is_noop_ship = MagicMock(return_value=is_noop)  # type: ignore[method-assign]
    d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    # Stash the summary output the caller wants ``_push_and_open_pr`` to
    # see (normally written by ``_run_summary_phase``).
    d._agent_summary_output = summary_output
    d._agent_unmet_criteria = []


# --------------------------------------------------------------------------
# summary_output_incomplete routes through _handle_agent_failure
# --------------------------------------------------------------------------


class TestSummaryOutputIncompleteRouting:
    def test_missing_commit_message_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """Summary returned with ``commit_message=""`` triggers the
        unified-failure handler, writing a ``phase_output_missing``
        failure row so the diagnoser picks it up.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "",  # MISSING — triggers the terminal
                "pr_title": "feat(foo): bar (#3062)",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3062",
            },
        )

        d._push_and_open_pr("agent-missing-commit", 3062, worktree)

        # _handle_agent_failure was called exactly once with the right shape.
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-missing-commit"
        assert kwargs["phase"] == "push_and_pr"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_PHASE_OUTPUT_MISSING
        assert kwargs["issue_number"] == 3062
        details = kwargs["details"]
        assert details["missing_phase_output"] == "summary"
        assert details["has_commit"] is False
        assert details["has_pr_title"] is True
        assert details["has_pr_body"] is True
        assert details["issue_number"] == 3062

        # Direct _mark_agent_terminal was NOT called — the unified
        # handler owns that (pre-#3062 bypass fixed).
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_missing_pr_title_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """Summary returned with ``pr_title=""`` also routes."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "feat(foo): bar (#3062)",
                "pr_title": "",  # MISSING
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3062",
            },
        )

        d._push_and_open_pr("agent-missing-title", 3062, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._handle_agent_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["has_commit"] is True
        assert details["has_pr_title"] is False
        assert details["has_pr_body"] is True

        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_missing_pr_body_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """Summary returned with ``pr_body_md=""`` also routes."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "feat(foo): bar (#3062)",
                "pr_title": "feat(foo): bar (#3062)",
                "pr_body_md": "",  # MISSING
            },
        )

        d._push_and_open_pr("agent-missing-body", 3062, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._handle_agent_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["has_commit"] is True
        assert details["has_pr_title"] is True
        assert details["has_pr_body"] is False

        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_all_three_missing_still_routes_cleanly(self, tmp_path: Path) -> None:
        """Defensive parsing — summary returned with every required
        field empty must route without crashing; the diagnoser can
        still escalate from the ``has_*`` flags alone.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={},  # every field missing
        )

        d._push_and_open_pr("agent-empty", 3062, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._handle_agent_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["has_commit"] is False
        assert details["has_pr_title"] is False
        assert details["has_pr_body"] is False

        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# Regression guards
# --------------------------------------------------------------------------


class TestRegressionGuards:
    def test_phase_output_missing_category_is_tier2_first_occurrence(self) -> None:
        """The reused category must diagnose on first occurrence — the
        whole point of routing summary_output_incomplete this way is
        to get the diagnoser involved immediately.
        """
        assert (
            daemon.FAILURE_CATEGORY_PHASE_OUTPUT_MISSING
            in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_noop_ship_still_marks_succeeded_directly(self, tmp_path: Path) -> None:
        """The early ``_is_noop_ship`` branch must not be affected — a
        clean no-op SHIP still terminates as ``succeeded`` via the
        direct ``_mark_agent_terminal`` call (correct-outcome
        terminal, not a failure path).
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "",
                "pr_title": "",
                "pr_body_md": "",
            },
            is_noop=True,
        )

        d._push_and_open_pr("agent-noop", 3062, worktree)

        # No-op path → _handle_agent_failure was NOT called; the direct
        # _mark_agent_terminal(status='succeeded') is correct.
        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
        d._mark_agent_terminal.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = d._mark_agent_terminal.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["status"] == "succeeded"


# --------------------------------------------------------------------------
# WIP-title guard (Issue #3303)
# --------------------------------------------------------------------------


class _SentinelError(BaseException):
    """Raised by a monkeypatched stub to short-circuit execution past the guard.

    Inherits from ``BaseException`` (not ``Exception``) so it propagates
    through ``except Exception`` blocks in ``_push_and_open_pr`` without
    being swallowed by the git-commit exception handler.
    """


class TestWipTitleGuard:
    def test_wip_prefix_title_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """``pr_title="WIP: ralph output"`` with valid commit_message and
        pr_body_md must route to ``_handle_agent_failure`` with
        ``category=FAILURE_CATEGORY_PHASE_OUTPUT_MISSING``,
        ``details["sub_reason"]=="wip_title"``, and
        ``details["title_is_wip"] is True``.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "feat(foo): bar (#3303)",
                "pr_title": "WIP: ralph output",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3303",
            },
        )

        d._push_and_open_pr("agent-wip-title", 3303, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-wip-title"
        assert kwargs["phase"] == "push_and_pr"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_PHASE_OUTPUT_MISSING
        assert kwargs["issue_number"] == 3303
        details = kwargs["details"]
        assert details["sub_reason"] == "wip_title"
        assert details["title_is_wip"] is True

        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_wip_lowercase_title_also_routes(self, tmp_path: Path) -> None:
        """``pr_title="wip: foo"`` (lower-case) must also be caught."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "feat(foo): bar (#3303)",
                "pr_title": "wip: foo",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3303",
            },
        )

        d._push_and_open_pr("agent-wip-lower", 3303, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._handle_agent_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["sub_reason"] == "wip_title"
        assert details["title_is_wip"] is True

        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_wip_with_leading_whitespace_routes(self, tmp_path: Path) -> None:
        """``pr_title="  WIP draft"`` (leading whitespace) must also be caught."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "feat(foo): bar (#3303)",
                "pr_title": "  WIP draft",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3303",
            },
        )

        d._push_and_open_pr("agent-wip-whitespace", 3303, worktree)

        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._handle_agent_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["sub_reason"] == "wip_title"
        assert details["title_is_wip"] is True

        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]

    def test_substring_wip_does_not_trigger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pr_title="feat(web): wipe stale token"`` must NOT be caught by
        the WIP guard (substring match, not a prefix). The test
        short-circuits execution past the guard by patching
        ``daemon.subprocess.run`` to raise ``_SentinelError`` — the absence
        of ``_handle_agent_failure`` being called is the assertion.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_push_and_open_pr_helpers(
            d,
            summary_output={
                "commit_message": "feat(web): wipe stale token (#3303)",
                "pr_title": "feat(web): wipe stale token (#3303)",
                "pr_body_md": "## Summary\n\nBaz.\n\nCloses #3303",
            },
        )

        # Short-circuit past the guard so the test doesn't need real git/gh.
        # The first real operation after the guard is subprocess.run for
        # ``git commit --amend`` — raise a sentinel so the test terminates
        # without needing a real git worktree.
        monkeypatch.setattr(
            daemon.subprocess,
            "run",
            lambda *_a, **_kw: (_ for _ in ()).throw(_SentinelError("short-circuit")),
        )

        with pytest.raises(_SentinelError):
            d._push_and_open_pr("agent-substring-wip", 3303, worktree)

        # The guard did NOT fire — _handle_agent_failure was not called.
        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
