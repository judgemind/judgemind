# ruff: noqa: I001
"""Regression tests for issue #3759.

``_fetch_issue_titles_for_blockers`` previously issued a batched GraphQL
query that aliased ``issue(number: N)`` for each blocker number. GitHub's
GraphQL ``issue`` field only resolves Issue nodes — when a blocker number
refers to a PR (e.g. ``Blocked by #3710`` where #3710 is a PR), the field
returns null AND the GraphQL response carries a top-level error:

    "Could not resolve to an Issue with the number of N"

``gh api graphql`` surfaces partial errors as a non-zero exit code, which
``_subprocess_with_retry`` treats as failure → 3× retry → exhausted.
The whole batch fails on a single PR-numbered blocker, even when other
blockers in the batch are valid issues.

The fix (Option A from the issue): query BOTH ``issue(number:N)`` and
``pullRequest(number:N)`` for each blocker number, with aliased fields
``i<n>_issue`` and ``i<n>_pr``. The parser prefers the issue-typed
result; when it's null, it falls back to the PR-typed result. The query
remains a single batched call.

These tests stub ``_subprocess_with_retry`` to return the new union shape
and assert the function returns the right title regardless of whether the
blocker resolves as an Issue or a PR.
"""

from __future__ import annotations

import json
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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (same shape as test_daemon_enrichment.py).
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


def _make_daemon_with_capture() -> tuple[
    daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler
]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger("dispatcher.test.blocker_title_fetch_pr_numbers")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake-for-tests",
        tick_scheduler_seconds=30,
        tick_supervisor_seconds=120,
        log_level="DEBUG",
        version_sha="deadbee",
        host="test-host",
        pid=4242,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-dev",
        heartbeat_metric_namespace="Judgemind/Dispatcher",
        aws_region="us-west-2",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]  — test stub
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Regression tests for the union-shape GraphQL query (#3759).
# --------------------------------------------------------------------------


class TestBlockerTitleFetchPrNumbers:
    """``_fetch_issue_titles_for_blockers`` resolves PR-numbered blockers.

    These tests assert the union-shape behaviour required by the fix:
    each blocker is queried as both ``issue(number:N)`` and
    ``pullRequest(number:N)``; the parser prefers the issue-typed result
    and falls back to the PR-typed result when the issue side is null.
    """

    def test_mixed_issue_and_pr_blockers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mix of issue and PR blockers — all titles returned."""
        d, _conn, _handler = _make_daemon_with_capture()

        call_log: list[list[str]] = []

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            call_log.append(cmd)
            result = MagicMock()
            result.returncode = 0
            # Union-shape response: each number has BOTH ``i<n>_issue`` and
            # ``i<n>_pr``. PRs return non-null for ``_pr`` only; issues
            # return non-null for ``_issue`` only.
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            # PR — issue side null, PR side resolves.
                            "i3710_issue": None,
                            "i3710_pr": {"number": 3710, "title": "PR title"},
                            # PR — issue side null, PR side resolves.
                            "i3714_issue": None,
                            "i3714_pr": {"number": 3714, "title": "PR title 2"},
                            # Issue — issue side resolves, PR side null.
                            "i3015_issue": {"number": 3015, "title": "Issue title"},
                            "i3015_pr": None,
                        }
                    }
                }
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers({3710, 3714, 3015})

        # Each blocker resolves to its title regardless of issue vs PR.
        assert titles == {
            3710: "PR title",
            3714: "PR title 2",
            3015: "Issue title",
        }

        # Single batched GraphQL call (key correctness invariant — see #3435).
        assert len(call_log) == 1
        # Query string mentions both issue and pullRequest fields per number.
        query_arg = call_log[0][4]
        assert "query=" in query_arg
        for n in [3710, 3714, 3015]:
            assert f"i{n}_issue:" in query_arg
            assert f"i{n}_pr:" in query_arg
        # The query references both top-level GraphQL fields.
        assert "issue(number:" in query_arg
        assert "pullRequest(number:" in query_arg

    def test_all_blockers_are_prs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every blocker number is a PR — no exception, all titles returned."""
        d, _conn, handler = _make_daemon_with_capture()

        call_log: list[list[str]] = []

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            call_log.append(cmd)
            result = MagicMock()
            result.returncode = 0
            # All blockers are PRs — every ``i<n>_issue`` is null, every
            # ``i<n>_pr`` resolves.
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "i3710_issue": None,
                            "i3710_pr": {"number": 3710, "title": "PR #3710 title"},
                            "i3714_issue": None,
                            "i3714_pr": {"number": 3714, "title": "PR #3714 title"},
                            "i3500_issue": None,
                            "i3500_pr": {"number": 3500, "title": "PR #3500 title"},
                        }
                    }
                }
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        # No exception, all PR titles returned.
        titles = d._fetch_issue_titles_for_blockers({3710, 3714, 3500})

        assert titles == {
            3710: "PR #3710 title",
            3714: "PR #3714 title",
            3500: "PR #3500 title",
        }
        assert len(call_log) == 1

        # No ``blocker_title_fetch_exhausted`` warning fired — the call
        # succeeded with the union-shape response (regression: this was
        # the bug surface in #3759 production logs).
        exhausted_warnings = [
            r
            for r in handler.records
            if r.levelno == logging.WARNING
            and getattr(r, "event", None) == "blocker_title_fetch_exhausted"
        ]
        assert exhausted_warnings == []

    def test_pr_side_takes_over_when_issue_side_null(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single PR blocker — parser falls back to the PR-typed result."""
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "i3710_issue": None,
                            "i3710_pr": {"number": 3710, "title": "Fallback PR title"},
                        }
                    }
                }
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers({3710})
        assert titles[3710] == "Fallback PR title"

    def test_issue_side_preferred_when_both_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If both fields resolve (shouldn't happen in practice, but defensive),
        the issue-typed title wins. Verifies parser preference order."""
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "i42_issue": {"number": 42, "title": "Issue-side title"},
                            "i42_pr": {"number": 42, "title": "PR-side title"},
                        }
                    }
                }
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers({42})
        # Parser prefers the issue-typed result.
        assert titles[42] == "Issue-side title"

    def test_both_sides_null_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If both ``i<n>_issue`` and ``i<n>_pr`` are null (e.g. number was
        deleted/transferred), the title is None — no crash."""
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "i9999_issue": None,
                            "i9999_pr": None,
                        }
                    }
                }
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers({9999})
        assert titles == {9999: None}
