"""Unit tests for the ``Closes #N`` PR-body guarantee + post-merge issue
cleanup added by issue #3411.

Two surfaces are covered:

1. ``DispatcherDaemon._ensure_closes_keyword`` — pure-functional helper
   that appends ``Closes #<issue_number>`` to a PR body when no closing
   keyword is already present for that issue. Idempotent.

2. ``DispatcherDaemon._close_issue_post_merge`` — best-effort post-merge
   cleanup called from ``_write_merged_at``. Closes the issue + strips
   ``agent/ready`` if the issue is still OPEN; no-ops if already
   CLOSED. All gh I/O is mocked.

Why it matters: PRs #3393, #3391, #3390, and others were merged by the
daemon without a ``Closes #N`` keyword in the body, so GitHub didn't
auto-close their issues. The daemon then re-evaluated those stale
``agent/ready`` issues every supervisor tick, wasting candidate-eval
cost and masking the true ready-queue size. These tests pin down the
fix shape so a future refactor doesn't quietly drop the guarantee.
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

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# _ensure_closes_keyword — pure-functional PR-body guarantee
# --------------------------------------------------------------------------


class TestEnsureClosesKeyword:
    """Acceptance criteria from issue #3411 — minimum test set."""

    def test_appends_when_keyword_missing(self) -> None:
        """The required behavior — body without Closes keyword gets it."""
        body = "## Summary\n\nFixes a thing.\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        assert "Closes #3411" in result
        # Original content preserved verbatim.
        assert "## Summary" in result
        assert "Fixes a thing." in result

    def test_idempotent_when_keyword_present(self) -> None:
        """Body with ``Closes #<N>`` already present is unchanged."""
        body = "## Summary\n\nFixes a thing.\n\nCloses #3411\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        assert result == body

    def test_recognizes_fixes_keyword(self) -> None:
        """``Fixes #N`` is the GitHub equivalent — should not double-append."""
        body = "## Summary\n\nFixes #3411\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        assert result == body
        # And critically — only one instance.
        assert result.count("3411") == 1

    def test_recognizes_resolves_keyword(self) -> None:
        """``Resolves #N`` is a GitHub-recognized closing keyword."""
        body = "## Summary\n\nResolves #3411\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        assert result == body

    def test_recognizes_closed_past_tense(self) -> None:
        """``Closed #N`` (past tense) is a recognized keyword too."""
        body = "## Summary\n\nClosed #3411\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        assert result == body

    def test_case_insensitive_keyword_match(self) -> None:
        """``CLOSES #N`` / ``closes #N`` should both count as present."""
        for variant in ("CLOSES #3411", "closes #3411", "Closes #3411"):
            body = f"## Summary\n\n{variant}\n"
            result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
            assert result == body, f"variant {variant!r} should be recognized"

    def test_appends_when_keyword_targets_other_issue(self) -> None:
        """``Closes #other`` does NOT count as our issue — append our keyword."""
        body = "## Summary\n\nCloses #999\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        # Both keywords present — the surplus close is rare in practice
        # (would only happen on a copy-paste regression) and operators
        # can correct it manually. Critical invariant: OUR issue gets
        # closed.
        assert "Closes #3411" in result
        assert "Closes #999" in result

    def test_word_boundary_prevents_substring_match(self) -> None:
        """``Closes #34110`` must NOT be recognized as ``Closes #3411``."""
        body = "## Summary\n\nCloses #34110\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        # Our keyword must be appended — the existing ``Closes #34110``
        # is a different issue.
        assert "Closes #3411\n" in result or result.endswith("Closes #3411\n")

    def test_empty_body_gets_keyword(self) -> None:
        """Empty / None body still produces a valid Closes-only body."""
        result = daemon.DispatcherDaemon._ensure_closes_keyword("", 3411)
        assert "Closes #3411" in result
        # And does not have a leading blank-line tail.
        assert not result.startswith("\n\n\n")

    def test_no_excessive_blank_lines_on_append(self) -> None:
        """Trailing whitespace stripped before append — no `\\n\\n\\n` tail."""
        body = "## Summary\n\nFixes a thing.\n\n\n\n"
        result = daemon.DispatcherDaemon._ensure_closes_keyword(body, 3411)
        # The append separator is exactly two newlines.
        assert "Fixes a thing.\n\nCloses #3411\n" in result
        # No 3+-newline run anywhere in the result.
        assert "\n\n\n" not in result


# --------------------------------------------------------------------------
# _close_issue_post_merge — best-effort post-merge cleanup
# --------------------------------------------------------------------------


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
) -> tuple[daemon.DispatcherDaemon, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.closes.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._run_id = "test-run-id"
    return d, handler


class TestClosePostMerge:
    def test_skips_when_issue_already_closed(self, tmp_path: Path) -> None:
        """If the issue is CLOSED, no gh close / label-strip is invoked."""
        d, handler = _make_daemon(tmp_path)
        d._gh_issue_is_open = MagicMock(return_value=False)  # type: ignore[method-assign]
        d._gh_issue_close = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        d._close_issue_post_merge(issue_number=42, pr_number=100)

        d._gh_issue_close.assert_not_called()
        d._gh_issue_remove_labels.assert_not_called()
        skipped = handler.events("post_merge_cleanup_skipped")
        assert skipped, "expected post_merge_cleanup_skipped log event"
        assert getattr(skipped[0], "reason", None) == "already_closed"

    def test_closes_and_strips_label_when_open(self, tmp_path: Path) -> None:
        """If the issue is OPEN, both close + agent/ready strip are invoked."""
        d, _handler = _make_daemon(tmp_path)
        d._gh_issue_is_open = MagicMock(return_value=True)  # type: ignore[method-assign]
        d._gh_issue_close = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        d._close_issue_post_merge(issue_number=42, pr_number=100)

        d._gh_issue_close.assert_called_once()
        # close kwargs: issue_number positional, comment + reason kw.
        call_args = d._gh_issue_close.call_args
        assert call_args.args[0] == 42
        assert call_args.kwargs["reason"] == "completed"
        # The comment names the PR # for operator legibility AND
        # cites #3411 so the audit trail is self-explanatory.
        assert "PR #100" in call_args.kwargs["comment"]
        assert "#3411" in call_args.kwargs["comment"]
        d._gh_issue_remove_labels.assert_called_once_with(42, ["agent/ready"])

    def test_close_comment_handles_missing_pr_number(self, tmp_path: Path) -> None:
        """pr_number=None still produces a coherent close comment."""
        d, _handler = _make_daemon(tmp_path)
        d._gh_issue_is_open = MagicMock(return_value=True)  # type: ignore[method-assign]
        d._gh_issue_close = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        d._close_issue_post_merge(issue_number=42, pr_number=None)

        call_args = d._gh_issue_close.call_args
        # No literal "PR #None" in the comment.
        assert "PR #None" not in call_args.kwargs["comment"]
        # Still cites #3411.
        assert "#3411" in call_args.kwargs["comment"]
        d._gh_issue_remove_labels.assert_called_once_with(42, ["agent/ready"])


# --------------------------------------------------------------------------
# Integration: _write_merged_at calls _close_issue_post_merge after the
# label release.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TestWriteMergedAtIntegration:
    def test_calls_close_issue_post_merge_after_label_release(
        self, tmp_path: Path
    ) -> None:
        """_write_merged_at should call _close_issue_post_merge with the
        same issue number + pr number it received, AFTER the
        status/in-progress label release."""
        d, _handler = _make_daemon(tmp_path)
        d._conn = _FakeConnection()  # type: ignore[assignment]
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]

        # Track the order of calls to label release vs post-merge cleanup.
        call_order: list[str] = []
        d._gh_issue_remove_labels = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda issue, labels: call_order.append(
                f"remove_labels:{issue}:{labels}"
            )
        )
        d._close_issue_post_merge = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda issue_number, pr_number: call_order.append(
                f"close_post_merge:{issue_number}:{pr_number}"
            )
        )

        d._write_merged_at("aaaa-bbbb", pr_number=123, issue_number=42)

        d._close_issue_post_merge.assert_called_once_with(42, 123)
        # Order: status/in-progress release first, then post-merge close.
        assert call_order == [
            "remove_labels:42:['status/in-progress']",
            "close_post_merge:42:123",
        ]

    def test_close_failure_does_not_propagate(self, tmp_path: Path) -> None:
        """A raise inside _close_issue_post_merge is caught and logged."""
        d, handler = _make_daemon(tmp_path)
        d._conn = _FakeConnection()  # type: ignore[assignment]
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]
        d._close_issue_post_merge = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("gh down")
        )

        # Does NOT raise.
        d._write_merged_at("aaaa-bbbb", pr_number=123, issue_number=42)

        failures = handler.events("close_issue_post_merge_failed")
        assert failures, "expected failure event when cleanup raises"

    def test_skips_close_when_issue_number_is_none(self, tmp_path: Path) -> None:
        """No issue_number means no cleanup attempted (no crash on int())."""
        d, _handler = _make_daemon(tmp_path)
        d._conn = _FakeConnection()  # type: ignore[assignment]
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]
        d._close_issue_post_merge = MagicMock()  # type: ignore[method-assign]

        d._write_merged_at("aaaa-bbbb", pr_number=123)

        d._close_issue_post_merge.assert_not_called()


# --------------------------------------------------------------------------
# Integration: _push_and_open_pr appends Closes #N before writing pr_body.md
# --------------------------------------------------------------------------
#
# The full _push_and_open_pr is too large to drive end-to-end here without
# extensive mocking; the integration assertion is delegated to the
# bash-side test for the entrypoint (test_agent_runner_entrypoint.sh) and
# the dedicated unit tests for ``_ensure_closes_keyword`` above. The call-
# site wiring is verified by the static check below.


class TestEnsureClosesKeywordCallSite:
    def test_push_and_open_pr_invokes_ensure_closes_keyword(self) -> None:
        """The call site exists in the daemon and references issue_number.

        Static-source check: PR-body validation must remain wired into
        ``_push_and_open_pr``. Refactors that move the validation to a
        different call site MUST update this test to point at the new
        call site — the goal is to keep someone from silently dropping
        the guarantee. (This is a lightweight alternative to driving
        the full ~600-line _push_and_open_pr through a mocked harness.)
        """
        source = Path(daemon.__file__).read_text(encoding="utf-8")
        # The wiring must call _ensure_closes_keyword somewhere AND that
        # somewhere must be inside the same module that calls
        # ``gh pr create``.
        assert "_ensure_closes_keyword" in source
        # Confirm it's called with an issue_number (defensive — guards
        # against a refactor that drops the second arg).
        assert "self._ensure_closes_keyword(pr_body_md," in source
