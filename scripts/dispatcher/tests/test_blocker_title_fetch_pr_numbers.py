# ruff: noqa: I001
"""Regression tests for ``_fetch_issue_titles_for_blockers``.

Two related bugs are exercised here:

#3759 — the original ``issue(number:N)``-only query failed the whole
batch on a single PR-numbered blocker, because ``gh api graphql``
exits non-zero when GitHub returns a top-level
``Could not resolve to an Issue with the number of N`` error. The fix
union-aliases ``issue(number:N)`` AND ``pullRequest(number:N)`` for
each blocker so the parser can fall back from one to the other.

#3808 — even with the union-aliased query, ``gh api graphql`` still
exits 1 whenever a batch mixes issue-only and PR-only blockers,
because GitHub returns valid ``data.repository`` AND a non-empty
``errors[]`` (NOT_FOUND for the unused alias side of each blocker).
Routing through ``_subprocess_with_retry`` (which treats non-zero
exit as transient failure) discarded the perfectly-good data and
burned three retries on a deterministic outcome — flooding CloudWatch
with ``blocker_title_fetch_exhausted`` warnings every ~30s. The fix
bypasses ``_subprocess_with_retry`` for this one call and parses
``data.repository`` regardless of exit code; the retry envelope is
kept only for true transient failures (timeout / gh missing /
empty stdout / unparseable JSON / ``data.repository`` missing).

These tests stub ``subprocess.run`` so they exercise the real
parser end-to-end, including the partial-error path (#3808).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


def _stub_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: BaseException | None = None,
    call_log: list[list[str]] | None = None,
    sleep_log: list[float] | None = None,
) -> None:
    """Patch ``subprocess.run`` (used inside ``_fetch_issue_titles_for_blockers``)
    and ``time.sleep`` (used in the retry envelope).

    ``raises`` lets a test inject a transient exception path; otherwise the
    call returns a stubbed ``CompletedProcess``-like object.

    Patches ``daemon.subprocess.run`` and ``daemon.time.sleep`` (the names
    ``daemon`` actually resolves at import time) rather than the global
    modules so other dispatcher tests in the same xdist worker stay clean.
    """

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        if call_log is not None:
            call_log.append(list(cmd))
        if raises is not None:
            raise raises
        return SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )

    def fake_sleep(seconds: float) -> None:
        if sleep_log is not None:
            sleep_log.append(seconds)

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)


# --------------------------------------------------------------------------
# #3759 — union-shape parser preserves PR-numbered blockers.
# --------------------------------------------------------------------------


class TestBlockerTitleFetchPrNumbers:
    """``_fetch_issue_titles_for_blockers`` resolves PR-numbered blockers.

    These tests assert the union-shape behaviour required by the #3759
    fix: each blocker is queried as both ``issue(number:N)`` and
    ``pullRequest(number:N)``; the parser prefers the issue-typed
    result and falls back to the PR-typed result when the issue side
    is null.
    """

    def test_mixed_issue_and_pr_blockers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mix of issue and PR blockers — all titles returned."""
        d, _conn, _handler = _make_daemon_with_capture()

        call_log: list[list[str]] = []
        _stub_subprocess_run(
            monkeypatch,
            call_log=call_log,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i3710_issue": None,
                            "i3710_pr": {
                                "number": 3710,
                                "title": "PR title",
                            },
                            "i3714_issue": None,
                            "i3714_pr": {
                                "number": 3714,
                                "title": "PR title 2",
                            },
                            "i3015_issue": {
                                "number": 3015,
                                "title": "Issue title",
                            },
                            "i3015_pr": None,
                        }
                    }
                }
            ),
        )

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
        cmd = call_log[0]
        assert cmd[:3] == ["gh", "api", "graphql"]
        query_arg = cmd[4]
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
        _stub_subprocess_run(
            monkeypatch,
            call_log=call_log,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i3710_issue": None,
                            "i3710_pr": {
                                "number": 3710,
                                "title": "PR #3710 title",
                            },
                            "i3714_issue": None,
                            "i3714_pr": {
                                "number": 3714,
                                "title": "PR #3714 title",
                            },
                            "i3500_issue": None,
                            "i3500_pr": {
                                "number": 3500,
                                "title": "PR #3500 title",
                            },
                        }
                    }
                }
            ),
        )

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
        assert handler.events("blocker_title_fetch_exhausted") == []

    def test_pr_side_takes_over_when_issue_side_null(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single PR blocker — parser falls back to the PR-typed result."""
        d, _conn, _handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i3710_issue": None,
                            "i3710_pr": {
                                "number": 3710,
                                "title": "Fallback PR title",
                            },
                        }
                    }
                }
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({3710})
        assert titles[3710] == "Fallback PR title"

    def test_issue_side_preferred_when_both_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If both fields resolve (shouldn't happen in practice, but defensive),
        the issue-typed title wins. Verifies parser preference order."""
        d, _conn, _handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i42_issue": {
                                "number": 42,
                                "title": "Issue-side title",
                            },
                            "i42_pr": {
                                "number": 42,
                                "title": "PR-side title",
                            },
                        }
                    }
                }
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({42})
        # Parser prefers the issue-typed result.
        assert titles[42] == "Issue-side title"

    def test_both_sides_null_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If both ``i<n>_issue`` and ``i<n>_pr`` are null (e.g. number was
        deleted/transferred), the title is None — no crash."""
        d, _conn, _handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i9999_issue": None,
                            "i9999_pr": None,
                        }
                    }
                }
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({9999})
        assert titles == {9999: None}


# --------------------------------------------------------------------------
# #3808 — partial-error response (exit 1 with valid data) is honored.
# --------------------------------------------------------------------------


class TestBlockerTitleFetchPartialErrors:
    """``_fetch_issue_titles_for_blockers`` parses ``data.repository``
    regardless of subprocess exit code (#3808).

    The seven blockers in the failing production batch right now:
    - ``2595, 2609, 2610, 3373, 3660`` — exist as **issues** only.
    - ``3710, 3714`` — exist as **PRs** only.

    GitHub's GraphQL returns valid ``data.repository`` with the resolved
    alias side of each, AND ``errors[]`` populated with NOT_FOUND for
    the unresolvable side, AND ``gh api graphql`` propagates that as
    exit 1. The fix is to parse ``data.repository`` anyway.
    """

    def test_partial_graphql_errors_returns_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial-error response (valid data + errors[] + exit 1) → titles
        returned. **This test must FAIL against pre-fix code** because the
        old implementation routed through ``_subprocess_with_retry``,
        which treats exit 1 as transient failure and retries 3× before
        returning all-None.
        """
        d, _conn, handler = _make_daemon_with_capture()

        # Mirror the production payload shape: ``data.repository`` is
        # populated AND ``errors[]`` carries NOT_FOUND entries AND
        # ``gh api graphql`` exits 1. Issue #3808 is closed by the
        # parser tolerating this exact shape.
        partial_error_stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "i2595_issue": {
                            "number": 2595,
                            "title": "decision: redesign cockpit refresh",
                        },
                        "i2595_pr": None,
                        "i3710_issue": None,
                        "i3710_pr": {
                            "number": 3710,
                            "title": "chore(api): drop unused columns",
                        },
                    }
                },
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "path": ["repository", "i2595_pr"],
                        "message": (
                            "Could not resolve to a PullRequest "
                            "with the number of 2595."
                        ),
                    },
                    {
                        "type": "NOT_FOUND",
                        "path": ["repository", "i3710_issue"],
                        "message": (
                            "Could not resolve to an Issue with the number of 3710."
                        ),
                    },
                ],
            }
        )
        call_log: list[list[str]] = []
        sleep_log: list[float] = []
        _stub_subprocess_run(
            monkeypatch,
            call_log=call_log,
            sleep_log=sleep_log,
            returncode=1,  # gh exits 1 because errors[] is populated
            stdout=partial_error_stdout,
            stderr=(
                "gh: Could not resolve to a PullRequest with the "
                "number of 2595. (NOT_FOUND)"
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({2595, 3710})

        # Both blockers resolve from ``data.repository`` even though
        # ``gh`` exited 1.
        assert titles == {
            2595: "decision: redesign cockpit refresh",
            3710: "chore(api): drop unused columns",
        }

        # Exactly one subprocess call — partial errors are deterministic,
        # retrying is wasteful (the production bug surface).
        assert len(call_log) == 1
        # No backoff sleeps — happy path returned on attempt 1.
        assert sleep_log == []

        # No ``_exhausted`` warning AND no ``_flake`` warning. The call
        # succeeded with valid data; ``errors[]`` is informational.
        assert handler.events("blocker_title_fetch_exhausted") == []
        assert handler.events("blocker_title_fetch_flake") == []

    def test_seven_production_blockers_all_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seven production blockers from issue #3808 all resolve
        from a single partial-error response. ``2595, 2609, 2610, 3373,
        3660`` are issue-only; ``3710, 3714`` are PR-only.
        """
        d, _conn, handler = _make_daemon_with_capture()

        partial_error_stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        # Issue-only blockers — issue side resolves.
                        "i2595_issue": {
                            "number": 2595,
                            "title": "issue 2595 title",
                        },
                        "i2595_pr": None,
                        "i2609_issue": {
                            "number": 2609,
                            "title": "issue 2609 title",
                        },
                        "i2609_pr": None,
                        "i2610_issue": {
                            "number": 2610,
                            "title": "issue 2610 title",
                        },
                        "i2610_pr": None,
                        "i3373_issue": {
                            "number": 3373,
                            "title": "issue 3373 title",
                        },
                        "i3373_pr": None,
                        "i3660_issue": {
                            "number": 3660,
                            "title": "issue 3660 title",
                        },
                        "i3660_pr": None,
                        # PR-only blockers — PR side resolves.
                        "i3710_issue": None,
                        "i3710_pr": {
                            "number": 3710,
                            "title": "pr 3710 title",
                        },
                        "i3714_issue": None,
                        "i3714_pr": {
                            "number": 3714,
                            "title": "pr 3714 title",
                        },
                    }
                },
                "errors": [
                    # 5× issue-only blockers contribute one NOT_FOUND each
                    # for the PR alias; 2× PR-only blockers contribute
                    # one NOT_FOUND each for the issue alias. 7 total.
                    {"type": "NOT_FOUND", "path": ["repository", "i2595_pr"]},
                    {"type": "NOT_FOUND", "path": ["repository", "i2609_pr"]},
                    {"type": "NOT_FOUND", "path": ["repository", "i2610_pr"]},
                    {"type": "NOT_FOUND", "path": ["repository", "i3373_pr"]},
                    {"type": "NOT_FOUND", "path": ["repository", "i3660_pr"]},
                    {"type": "NOT_FOUND", "path": ["repository", "i3710_issue"]},
                    {"type": "NOT_FOUND", "path": ["repository", "i3714_issue"]},
                ],
            }
        )
        _stub_subprocess_run(
            monkeypatch,
            returncode=1,
            stdout=partial_error_stdout,
        )

        titles = d._fetch_issue_titles_for_blockers(
            {2595, 2609, 2610, 3373, 3660, 3710, 3714}
        )

        assert titles == {
            2595: "issue 2595 title",
            2609: "issue 2609 title",
            2610: "issue 2610 title",
            3373: "issue 3373 title",
            3660: "issue 3660 title",
            3710: "pr 3710 title",
            3714: "pr 3714 title",
        }
        # All warning events stayed silent — production CloudWatch noise
        # disappears once the parser tolerates partial errors.
        assert handler.events("blocker_title_fetch_exhausted") == []
        assert handler.events("blocker_title_fetch_flake") == []

    def test_issue_only_batch_no_errors_returns_titles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch of pure-issue blockers (no PR-only entries) still has
        ``errors[]`` for every ``_pr`` alias. Parser ignores ``errors[]``
        and returns the issue-side title for each blocker."""
        d, _conn, handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=1,  # errors[] populated → gh exit 1
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i100_issue": {"number": 100, "title": "issue 100"},
                            "i100_pr": None,
                            "i200_issue": {"number": 200, "title": "issue 200"},
                            "i200_pr": None,
                        }
                    },
                    "errors": [
                        {"type": "NOT_FOUND", "path": ["repository", "i100_pr"]},
                        {"type": "NOT_FOUND", "path": ["repository", "i200_pr"]},
                    ],
                }
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({100, 200})
        assert titles == {100: "issue 100", 200: "issue 200"}
        assert handler.events("blocker_title_fetch_exhausted") == []

    def test_pr_only_batch_no_errors_returns_titles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch of pure-PR blockers also has ``errors[]`` for every
        ``_issue`` alias. Parser falls back to the ``_pr`` side for each."""
        d, _conn, handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=1,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "i100_issue": None,
                            "i100_pr": {"number": 100, "title": "pr 100"},
                            "i200_issue": None,
                            "i200_pr": {"number": 200, "title": "pr 200"},
                        }
                    },
                    "errors": [
                        {"type": "NOT_FOUND", "path": ["repository", "i100_issue"]},
                        {"type": "NOT_FOUND", "path": ["repository", "i200_issue"]},
                    ],
                }
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({100, 200})
        assert titles == {100: "pr 100", 200: "pr 200"}
        assert handler.events("blocker_title_fetch_exhausted") == []


# --------------------------------------------------------------------------
# #3808 — true transient failures still emit the exhausted log.
# --------------------------------------------------------------------------


class TestBlockerTitleFetchExhaustedPath:
    """Genuine transient failures still hit the 3-attempt retry envelope
    and emit the ``blocker_title_fetch_exhausted`` warning. The fix only
    skips retries for the deterministic partial-error case.
    """

    def test_timeout_returns_all_none_and_emits_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``subprocess.TimeoutExpired`` is transient — retry 3×, then
        return ``{n: None}`` and emit ``_exhausted``."""
        d, _conn, handler = _make_daemon_with_capture()

        sleep_log: list[float] = []
        _stub_subprocess_run(
            monkeypatch,
            sleep_log=sleep_log,
            raises=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        )

        titles = d._fetch_issue_titles_for_blockers({42, 43})
        assert titles == {42: None, 43: None}

        # 3 attempts → 2 sleeps (after attempts 1 and 2).
        assert sleep_log == [1.0, 2.0]

        # Per-attempt flake warnings + final exhausted warning.
        flake_events = handler.events("blocker_title_fetch_flake")
        assert len(flake_events) == 3
        for record in flake_events:
            assert record.reason == "timeout"

        exhausted_events = handler.events("blocker_title_fetch_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason == "timeout"
        assert exhausted_events[0].attempts == 3
        assert exhausted_events[0].blocker_count == 2

    def test_empty_stdout_returns_all_none_and_emits_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty stdout (with either zero or non-zero exit) is treated
        as a transient flake — retry 3×, then return ``{n: None}`` and
        emit ``_exhausted``."""
        d, _conn, handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=0,
            stdout="",
            stderr="connection closed by peer",
        )

        titles = d._fetch_issue_titles_for_blockers({99})
        assert titles == {99: None}

        flake_events = handler.events("blocker_title_fetch_flake")
        assert len(flake_events) == 3
        for record in flake_events:
            assert record.reason == "empty_stdout"

        exhausted_events = handler.events("blocker_title_fetch_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason == "empty_stdout"

    def test_unparseable_json_returns_all_none_and_emits_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed stdout (not JSON) is treated as transient — retry
        3× then return ``{n: None}`` and emit ``_exhausted``."""
        d, _conn, handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=1,
            stdout="<html>504 Gateway Timeout</html>",
        )

        titles = d._fetch_issue_titles_for_blockers({77})
        assert titles == {77: None}

        flake_events = handler.events("blocker_title_fetch_flake")
        assert len(flake_events) == 3
        for record in flake_events:
            assert record.reason == "unparseable_json"

        exhausted_events = handler.events("blocker_title_fetch_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason == "unparseable_json"

    def test_gh_missing_returns_all_none_and_emits_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``FileNotFoundError`` (gh CLI missing on PATH) is transient —
        retry 3×, then return ``{n: None}`` and emit ``_exhausted``."""
        d, _conn, handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            raises=FileNotFoundError("gh: command not found"),
        )

        titles = d._fetch_issue_titles_for_blockers({1})
        assert titles == {1: None}

        flake_events = handler.events("blocker_title_fetch_flake")
        assert len(flake_events) == 3
        for record in flake_events:
            assert record.reason == "gh_missing"

        exhausted_events = handler.events("blocker_title_fetch_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason == "gh_missing"

    def test_missing_data_repository_returns_all_none_and_emits_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Payload parses as JSON but ``data.repository`` is missing
        (e.g., auth scope drop, rate-limit response shape). Treated as
        transient — retry 3×, then return ``{n: None}`` and emit
        ``_exhausted``."""
        d, _conn, handler = _make_daemon_with_capture()

        _stub_subprocess_run(
            monkeypatch,
            returncode=1,
            stdout=json.dumps(
                {
                    # No ``data.repository`` — only top-level errors.
                    "errors": [{"message": "Bad credentials"}],
                }
            ),
        )

        titles = d._fetch_issue_titles_for_blockers({5})
        assert titles == {5: None}

        flake_events = handler.events("blocker_title_fetch_flake")
        assert len(flake_events) == 3
        for record in flake_events:
            assert record.reason == "missing_data"

        exhausted_events = handler.events("blocker_title_fetch_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason == "missing_data"

    def test_empty_input_short_circuits_with_no_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty input → empty output, no subprocess call, no logs."""
        d, _conn, handler = _make_daemon_with_capture()

        call_log: list[list[str]] = []
        _stub_subprocess_run(
            monkeypatch,
            call_log=call_log,
            returncode=0,
            stdout="{}",
        )

        titles = d._fetch_issue_titles_for_blockers(set())
        assert titles == {}
        assert call_log == []
        assert handler.events("blocker_title_fetch_flake") == []
        assert handler.events("blocker_title_fetch_exhausted") == []
