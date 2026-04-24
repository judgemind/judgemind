"""Unit tests for the Phase 3A orchestration path in ``scripts.dispatcher.daemon``.

Issue #2783. Covers the seven behaviors the issue's acceptance criteria
call out:

* ``_claim_and_orchestrate_one`` picks the highest-priority trusted
  candidate from the latest ``queue_snapshots`` row.
* Atomic claim via INSERT catches ``psycopg.errors.UniqueViolation``
  as the "lost the race" signal.
* Per-agent worktree created at ``.claude/worktrees/agent-<short-uuid>``
  from ``origin/main`` (subprocess mocked).
* ``/task-v2-{plan,ralph,summary}`` invoked sequentially via
  ``claude -p`` subprocess (subprocess mocked). Phase output JSON
  written to ``dispatcher.phase_outputs`` after each;
  ``dispatcher.phase_transitions`` row per phase change.
* On all-phases-success, daemon pushes + creates PR via ``gh pr
  create`` (subprocess mocked); PR number recorded in
  ``dispatcher.agents.pr_number``.
* Phase failures (subprocess non-zero exit, ralph BLOCKED, plan
  go=false with block_reason) set ``agents.status='failed'`` and stop
  orchestration without crashing the daemon.
* Concurrency cap honored: if ``concurrency_cap=0``, no claims
  attempted (Phase 2 behavior preserved).

All external calls — subprocess (``claude -p``, ``gh``, ``git``,
``scripts/check-issue-author.sh``) and psycopg — are mocked. The
orchestration path does not exercise ``claude`` or ``gh`` binaries on
the test runner.

Fakes & fixtures
----------------
``_FakeCursor`` is the shared DB cursor stub used throughout this file.

**Preferred pattern — dict-keyed responses (issue #2793):**
Set ``cursor.fetch_responses`` to a ``dict[str, Any]`` that maps SQL
fragment strings to the value ``fetchone()`` should return.  On each
``fetchone()`` call the cursor inspects the last executed SQL
(``self.executed[-1][0]``) and returns the value whose key is a
substring of that SQL (insertion-order, first match wins).  This is
robust against positional reordering of DB calls.

Example::

    conn.cursor_instance.fetch_responses = {
        "status = 'retrying'": None,
        "FROM dispatcher.queue_snapshots": (None, [42]),
        "FROM dispatcher.agents WHERE issue_number": None,
    }

**Legacy pattern — positional queue (kept for existing tests):**
Set ``cursor.fetch_queue`` to a list; each ``fetchone()`` call pops
the front element.  Tests that haven't migrated still work unchanged —
when ``fetch_responses`` is empty (the default) ``fetchone()`` falls
back to the legacy queue exactly as before.  New tests should prefer
``fetch_responses``; the queue is a last resort.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid as uuid_mod
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import psycopg  # noqa: E402  — stub installed by conftest.py

from dispatcher import daemon  # noqa: E402  — sys.path mutation above
from dispatcher.tests._popen_fake import make_popen_factory  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
        self.fetch_responses: dict[str, Any] = {}
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if self.fetch_responses and self.executed:
            last_sql = self.executed[-1][0]
            for fragment, value in self.fetch_responses.items():
                if fragment in last_sql:
                    return value
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
    logger = logging.getLogger(f"dispatcher.test.phase3a.{id(tmp_path)}")
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
# _FakeCursor dispatcher unit tests (issue #2793)
# --------------------------------------------------------------------------


class TestFakeCursorDispatcher:
    """Unit tests for the ``fetch_responses`` dict-keyed dispatcher on ``_FakeCursor``.

    These tests exercise the new dispatcher mechanism directly — not only
    via the integrated orchestration test — so regressions in the helper
    are caught at the lowest level.
    """

    def test_fetch_responses_substring_match_returns_mapped_value(self) -> None:
        """A key that is a substring of the last executed SQL returns its value."""
        cur = _FakeCursor()
        cur.fetch_responses = {"FROM dispatcher.queue_snapshots": (None, [42])}
        cur.execute(
            "SELECT snapshot FROM dispatcher.queue_snapshots ORDER BY id DESC LIMIT 1"
        )
        assert cur.fetchone() == (None, [42])

    def test_fetch_responses_first_matching_key_wins(self) -> None:
        """When multiple keys match, insertion order determines the winner."""
        cur = _FakeCursor()
        cur.fetch_responses = {
            "dispatcher": "first",
            "queue_snapshots": "second",
        }
        cur.execute("SELECT * FROM dispatcher.queue_snapshots")
        # "dispatcher" comes first in insertion order — it wins.
        assert cur.fetchone() == "first"

    def test_fetch_responses_no_match_falls_back_to_fetch_queue(self) -> None:
        """When no key matches, the legacy positional queue is used."""
        cur = _FakeCursor()
        cur.fetch_responses = {"no_such_fragment": "should_not_return"}
        cur.fetch_queue = ["from_queue"]
        cur.execute("SELECT 1")
        assert cur.fetchone() == "from_queue"

    def test_fetch_responses_no_match_empty_queue_returns_none(self) -> None:
        """When no key matches and the queue is empty, None is returned."""
        cur = _FakeCursor()
        cur.fetch_responses = {"no_such_fragment": "something"}
        cur.execute("SELECT 1")
        assert cur.fetchone() is None

    def test_fetch_responses_does_not_consume_fetch_queue_on_match(self) -> None:
        """A successful dict match leaves the positional queue untouched."""
        cur = _FakeCursor()
        cur.fetch_responses = {"SELECT 1": "matched"}
        cur.fetch_queue = ["queue_item"]
        cur.execute("SELECT 1")
        cur.fetchone()  # consumes the dict match
        # fetch_queue must still have its item intact.
        assert cur.fetch_queue == ["queue_item"]

    def test_empty_fetch_responses_is_pure_passthrough_to_fetch_queue(self) -> None:
        """When ``fetch_responses`` is empty (the default), behaviour is unchanged.

        This guards AC2: every existing test that never sets
        ``fetch_responses`` continues to work identically to before.
        """
        cur = _FakeCursor()
        # fetch_responses is intentionally left at its default empty dict.
        cur.fetch_queue = ["a", "b", "c"]
        cur.execute("SELECT 1")
        assert cur.fetchone() == "a"
        cur.execute("SELECT 2")
        assert cur.fetchone() == "b"
        cur.execute("SELECT 3")
        assert cur.fetchone() == "c"
        cur.execute("SELECT 4")
        assert cur.fetchone() is None


# --------------------------------------------------------------------------
# _priority_rank (issue #2835)
# --------------------------------------------------------------------------


class TestPriorityRank:
    """``_priority_rank`` maps label lists to sort ranks (p0 → 0, ...)."""

    def test_p0_returns_zero(self) -> None:
        assert daemon._priority_rank(["priority/p0"]) == 0

    def test_p1_returns_one(self) -> None:
        assert daemon._priority_rank(["priority/p1"]) == 1

    def test_p2_returns_two(self) -> None:
        assert daemon._priority_rank(["priority/p2"]) == 2

    def test_p3_returns_three(self) -> None:
        assert daemon._priority_rank(["priority/p3"]) == 3

    def test_no_priority_label_returns_floor(self) -> None:
        assert (
            daemon._priority_rank(["area/devops", "type/bug"])
            == daemon._PRIORITY_RANK_NO_LABEL
        )

    def test_empty_labels_returns_floor(self) -> None:
        assert daemon._priority_rank([]) == daemon._PRIORITY_RANK_NO_LABEL

    def test_non_list_returns_floor(self) -> None:
        """Malformed ``labels`` (None, str, dict, etc.) cannot crash the sort."""
        assert daemon._priority_rank(None) == daemon._PRIORITY_RANK_NO_LABEL
        assert daemon._priority_rank("priority/p0") == daemon._PRIORITY_RANK_NO_LABEL
        assert (
            daemon._priority_rank({"priority/p0": 1}) == daemon._PRIORITY_RANK_NO_LABEL
        )

    def test_multiple_priority_labels_picks_lowest_rank(self) -> None:
        """If both p0 and p2 are attached, p0 wins (most-urgent interpretation)."""
        assert daemon._priority_rank(["priority/p2", "priority/p0"]) == 0
        assert daemon._priority_rank(["priority/p3", "priority/p1"]) == 1

    def test_non_string_label_entries_are_ignored(self) -> None:
        assert daemon._priority_rank([{"name": "priority/p0"}, 42, None]) == (
            daemon._PRIORITY_RANK_NO_LABEL
        )

    def test_rank_ordering_is_p0_lt_p1_lt_p2_lt_p3_lt_floor(self) -> None:
        """Document the end-to-end ordering contract in one assertion."""
        ranks = [
            daemon._priority_rank(["priority/p0"]),
            daemon._priority_rank(["priority/p1"]),
            daemon._priority_rank(["priority/p2"]),
            daemon._priority_rank(["priority/p3"]),
            daemon._priority_rank([]),
        ]
        assert ranks == sorted(ranks)
        assert ranks == [0, 1, 2, 3, daemon._PRIORITY_RANK_NO_LABEL]


# --------------------------------------------------------------------------
# _extract_priority (issue #2899)
# --------------------------------------------------------------------------


class TestExtractPriority:
    """``_extract_priority`` maps label lists to the stored priority value.

    Mirrors :class:`TestPriorityRank` but returns the string value
    (``'p0'`` | ... | None) instead of a numeric sort rank. Used at
    claim time to populate ``dispatcher.agents.priority``.
    """

    def test_p0_returns_p0(self) -> None:
        assert daemon._extract_priority(["priority/p0"]) == "p0"

    def test_p1_returns_p1(self) -> None:
        assert daemon._extract_priority(["priority/p1"]) == "p1"

    def test_p2_returns_p2(self) -> None:
        assert daemon._extract_priority(["priority/p2"]) == "p2"

    def test_p3_returns_p3(self) -> None:
        assert daemon._extract_priority(["priority/p3"]) == "p3"

    def test_no_priority_label_returns_none(self) -> None:
        assert daemon._extract_priority(["area/devops", "type/bug"]) is None

    def test_empty_labels_returns_none(self) -> None:
        assert daemon._extract_priority([]) is None

    def test_non_list_returns_none(self) -> None:
        assert daemon._extract_priority(None) is None
        assert daemon._extract_priority("priority/p0") is None
        assert daemon._extract_priority({"priority/p0": 1}) is None

    def test_multiple_priority_labels_picks_most_urgent(self) -> None:
        """Matches ``_priority_rank`` — p0 wins over p2, p1 over p3, etc."""
        assert daemon._extract_priority(["priority/p2", "priority/p0"]) == "p0"
        assert daemon._extract_priority(["priority/p3", "priority/p1"]) == "p1"

    def test_non_string_entries_are_ignored(self) -> None:
        """Defensive: malformed JSONB rows cannot crash the claim path."""
        assert daemon._extract_priority([{"name": "priority/p0"}, 42, None]) is None


# --------------------------------------------------------------------------
# Gate in scheduler_tick: orchestration only runs when cap>0 and no agent
# --------------------------------------------------------------------------


class TestSchedulerGate:
    """``_maybe_spawn_orchestration_thread`` runs only when the gate allows.

    After #2847 the actual orchestration work runs on a background
    thread spawned by :meth:`DispatcherDaemon._maybe_spawn_orchestration_thread`.
    These tests cover the gate logic on the scheduler tick itself —
    cap=0 / cap-missing / active-agent / orchestration-in-flight — by
    mocking the spawn helper directly. The thread-side behavior is
    covered by :class:`TestOrchestrationWorkerThread` in this file.
    """

    def test_does_not_claim_when_concurrency_cap_zero(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called with cap=0")
        )
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 0
        assert d._maybe_spawn_orchestration_thread.call_count == 0  # type: ignore[attr-defined]

    def test_does_not_claim_when_concurrency_cap_missing(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock()  # type: ignore[method-assign]
        d.scheduler_tick()
        assert d._maybe_spawn_orchestration_thread.call_count == 0  # type: ignore[attr-defined]

    def test_claims_when_cap_nonzero_and_no_active_agent(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # 1) config read returns 1; 2) _is_paused SELECT returns None (not paused);
        # 3) _has_active_agent SELECT returns None (no active agent).
        conn.cursor_instance.fetch_queue = [(1,), None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=True,
        )
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 1
        assert d._maybe_spawn_orchestration_thread.call_count == 1  # type: ignore[attr-defined]

    def test_does_not_claim_when_active_agent_exists(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # config=1; _is_paused returns None (not paused); _has_active_agent
        # returns (1,) meaning active row exists.
        conn.cursor_instance.fetch_queue = [(1,), None, (1,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called when agent active")
        )
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 0

    def test_orchestration_exception_does_not_crash_tick(self, tmp_path: Path) -> None:
        """A spawn-path exception must not crash ``scheduler_tick`` (#2847).

        After #2847 the orchestration work runs on a worker thread, so
        an exception raised inside :meth:`_claim_and_orchestrate_one`
        surfaces as ``orchestration_worker_failed`` from the thread
        (not ``orchestration_failed`` from the main tick). This test
        asserts the spawn path itself does not raise — the actual
        thread-side failure is covered by
        :class:`TestOrchestrationWorkerThread`.
        """
        d, conn, handler = _make_daemon(tmp_path)
        # config=1; _is_paused returns None (not paused); _has_active_agent
        # returns None (no active agent) — spawn path fires but throws.
        conn.cursor_instance.fetch_queue = [(1,), None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        # Make the spawn path itself raise — covers the
        # ``orchestration_spawn_failed`` branch in the tick.
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )
        # The tick must still return a summary, not raise.
        summary = d.scheduler_tick()
        assert handler.events("orchestration_spawn_failed") != []
        # Spawn failed → no thread was actually started.
        assert summary["orchestration_attempted"] == 0


# --------------------------------------------------------------------------
# _has_active_agent
# --------------------------------------------------------------------------


class TestHasActiveAgent:
    def test_no_running_row_returns_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._has_active_agent() is False

    def test_running_row_returns_true(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        assert d._has_active_agent() is True

    def test_db_error_returns_true_fail_closed(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("connection lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        # Fail-closed: treat DB error as "active" so we skip this tick.
        assert d._has_active_agent() is True
        assert conn.rollbacks >= 1

    def test_query_has_no_kind_filter(self, tmp_path: Path) -> None:
        """Post-#2927 regression: ``_has_active_agent`` has no kind filter.

        The /task skill stopped writing to ``dispatcher.agents``
        (label-only coordination), so every ``status='running'`` row
        is daemon-owned by construction. The #2908 ``kind='task'``
        carve-out was reverted — the SELECT is back to its pre-#2866
        shape. This test locks in that shape so the carve-out can't
        silently come back.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        d._has_active_agent()

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT 1 FROM dispatcher.agents" in e[0]
        ]
        assert selects, "expected _has_active_agent to issue the SELECT"
        sql, _params = selects[0]
        assert "status = 'running'" in sql
        assert "kind = 'task'" not in sql, (
            "_has_active_agent should not carry a kind filter post-#2927 — "
            "every row is daemon-owned. Actual SQL: " + sql
        )


# --------------------------------------------------------------------------
# _latest_queue_snapshot_issues
# --------------------------------------------------------------------------


class TestLatestQueueSnapshot:
    def test_returns_issue_numbers_from_most_recent_row(self, tmp_path: Path) -> None:
        """Pre-#2820 fallback path: empty ``issues_json`` → raw ``issue_numbers`` order."""
        d, conn, _handler = _make_daemon(tmp_path)
        # (issues_json, issue_numbers) — issues_json None triggers the
        # fallback to the raw issue_numbers column.
        conn.cursor_instance.fetch_queue = [(None, [100, 200, 300])]
        assert d._latest_queue_snapshot_issues() == [100, 200, 300]

    def test_returns_empty_when_no_snapshots(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._latest_queue_snapshot_issues() == []

    def test_returns_empty_on_db_error(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._latest_queue_snapshot_issues() == []
        assert conn.rollbacks >= 1

    # Issue #2835 — priority-based ordering when issues_json is populated.

    def test_sorts_by_priority_p0_before_p1_before_p2(self, tmp_path: Path) -> None:
        """Mixed priorities: p0 wins, then p1, then p2."""
        d, conn, _handler = _make_daemon(tmp_path)
        issues_json = [
            {
                "number": 100,
                "labels": ["priority/p2", "area/devops"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
            {
                "number": 200,
                "labels": ["priority/p0"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
            {
                "number": 300,
                "labels": ["priority/p1"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
        ]
        conn.cursor_instance.fetch_queue = [(issues_json, [100, 200, 300])]
        assert d._latest_queue_snapshot_issues() == [200, 300, 100]

    def test_created_at_asc_is_tiebreaker_within_priority(self, tmp_path: Path) -> None:
        """Two p1s with different ``createdAt`` → older (asc) picked first."""
        d, conn, _handler = _make_daemon(tmp_path)
        issues_json = [
            {
                "number": 100,
                "labels": ["priority/p1"],
                "createdAt": "2026-04-19T00:00:00Z",  # newer
            },
            {
                "number": 200,
                "labels": ["priority/p1"],
                "createdAt": "2026-03-01T00:00:00Z",  # older — wins
            },
        ]
        conn.cursor_instance.fetch_queue = [(issues_json, [100, 200])]
        assert d._latest_queue_snapshot_issues() == [200, 100]

    def test_no_priority_label_is_lowest_rank(self, tmp_path: Path) -> None:
        """Unlabeled issues sit below every priority/p* — picked only last."""
        d, conn, _handler = _make_daemon(tmp_path)
        issues_json = [
            {
                "number": 100,
                "labels": ["area/devops"],  # no priority/* label
                "createdAt": "2026-01-01T00:00:00Z",  # oldest — doesn't help
            },
            {
                "number": 200,
                "labels": ["priority/p3"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
            {
                "number": 300,
                "labels": ["priority/p2"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
        ]
        conn.cursor_instance.fetch_queue = [(issues_json, [100, 200, 300])]
        # 300 (p2) < 200 (p3) < 100 (no priority).
        assert d._latest_queue_snapshot_issues() == [300, 200, 100]

    def test_handles_jsonb_returned_as_string(self, tmp_path: Path) -> None:
        """Defensive: psycopg returns jsonb parsed, but tests / edge paths may stub strings."""
        d, conn, _handler = _make_daemon(tmp_path)
        issues_json_str = json.dumps(
            [
                {
                    "number": 100,
                    "labels": ["priority/p2"],
                    "createdAt": "2026-04-19T00:00:00Z",
                },
                {
                    "number": 200,
                    "labels": ["priority/p0"],
                    "createdAt": "2026-04-19T00:00:00Z",
                },
            ]
        )
        conn.cursor_instance.fetch_queue = [(issues_json_str, [100, 200])]
        assert d._latest_queue_snapshot_issues() == [200, 100]

    def test_falls_back_to_issue_numbers_when_issues_json_is_empty_list(
        self, tmp_path: Path
    ) -> None:
        """Empty ``issues_json`` → raw ``issue_numbers`` (unsorted fallback)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([], [100, 200, 300])]
        assert d._latest_queue_snapshot_issues() == [100, 200, 300]

    def test_falls_back_when_issues_json_is_malformed_string(
        self, tmp_path: Path
    ) -> None:
        """Malformed JSON string → fallback to ``issue_numbers``."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("not-json{", [100, 200, 300])]
        assert d._latest_queue_snapshot_issues() == [100, 200, 300]

    def test_skips_non_dict_entries_in_issues_json(self, tmp_path: Path) -> None:
        """Defensive: stray non-dict entries in ``issues_json`` are ignored."""
        d, conn, _handler = _make_daemon(tmp_path)
        issues_json = [
            "not-a-dict",
            {
                "number": 100,
                "labels": ["priority/p1"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
            42,
            {
                "number": 200,
                "labels": ["priority/p0"],
                "createdAt": "2026-04-19T00:00:00Z",
            },
        ]
        conn.cursor_instance.fetch_queue = [(issues_json, [100, 200])]
        assert d._latest_queue_snapshot_issues() == [200, 100]


# --------------------------------------------------------------------------
# _issue_already_attempted
# --------------------------------------------------------------------------


class TestIssueAlreadyAttempted:
    def test_no_row_returns_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # SQL function returns (False,) when no active agent row exists.
        conn.cursor_instance.fetch_queue = [(False,)]
        assert d._issue_already_attempted(42) is False

    def test_running_row_returns_true(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # SQL function returns (True,) when an active agent row exists.
        conn.cursor_instance.fetch_queue = [(True,)]
        assert d._issue_already_attempted(42) is True
        # Confirm the SQL delegates to the SQL function (migration 37).
        select_calls = [
            e
            for e in conn.cursor_instance.executed
            if "dispatcher.issue_has_active_agent" in e[0]
        ]
        assert select_calls
        assert select_calls[0][1][0] == 42

    def test_db_error_returns_true_fail_closed(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._issue_already_attempted(42) is True

    def test_needs_review_row_blocks_repickup(self, tmp_path: Path) -> None:
        """#2856: prior ``needs_review`` row keeps the issue out of the pickup loop.

        When summary_unmet_criteria fires, the daemon opens a DRAFT PR
        and terminates the agent as ``needs_review``. The issue stays
        labelled ``agent/ready`` (the issue body still describes
        requested work), so the next scheduler tick would otherwise
        re-pick it and race two agent branches against the open draft.
        The SQL function ``dispatcher.issue_has_active_agent`` (migration 37)
        includes ``needs_review`` in its active-status list for exactly
        this reason — the picker treats the prior row as "issue has an
        in-flight artifact" until the operator merges, closes, or edits.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        # Return (True,) as if the SQL function found a needs_review row.
        conn.cursor_instance.fetch_queue = [(True,)]
        assert d._issue_already_attempted(42) is True
        # Confirm the SELECT delegates to the SQL function (migration 37).
        select_calls = [
            e
            for e in conn.cursor_instance.executed
            if "dispatcher.issue_has_active_agent" in e[0]
        ]
        assert select_calls
        # The function receives (issue_number,) as its sole parameter.
        params = select_calls[0][1]
        assert params is not None
        assert params[0] == 42


# --------------------------------------------------------------------------
# _issue_author_trusted (wraps scripts/check-issue-author.sh)
# --------------------------------------------------------------------------


class TestIssueAuthorTrusted:
    def test_exit_zero_returns_true(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            assert cmd[0].endswith("check-issue-author.sh")
            assert cmd[1] == "42"
            r = MagicMock()
            r.returncode = 0
            r.stdout = "TRUSTED: ...\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is True

    def test_exit_nonzero_returns_false(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = "UNTRUSTED: ...\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is False
        rejects = handler.events("trust_check_rejected")
        assert len(rejects) == 1

    def test_timeout_returns_false(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is False
        assert handler.events("trust_check_timeout") != []

    def test_script_missing_returns_false(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError(2, "nope", cmd[0])

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is False
        assert handler.events("trust_check_missing") != []


# --------------------------------------------------------------------------
# _pick_candidate_issue
# --------------------------------------------------------------------------


class TestPickCandidate:
    def test_returns_first_eligible_candidate(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        # Mark #1 as already attempted, #2 as untrusted, #3 as eligible.
        attempted = {1}
        untrusted = {2}

        d._issue_already_attempted = lambda n: n in attempted  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: n not in untrusted  # type: ignore[method-assign]

        assert d._pick_candidate_issue([1, 2, 3, 4]) == 3

    def test_returns_none_when_all_skipped(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: True  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: False  # type: ignore[method-assign]
        assert d._pick_candidate_issue([1, 2, 3]) is None
        # Three skip events logged (all "already attempted" since that's
        # checked first).
        assert len(handler.events("candidate_skipped")) == 3


# --------------------------------------------------------------------------
# _atomic_claim
# --------------------------------------------------------------------------


class TestAtomicClaim:
    def test_happy_path_inserts_and_returns_true(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is True
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        assert len(inserts) == 1
        # #2899 — priority column is now part of every INSERT.
        insert_sql = inserts[0][0]
        assert "priority" in insert_sql
        # Single atomic claim: exactly one commit.
        assert conn.commits == 1
        assert handler.events("claim_succeeded") != []

    def test_priority_parameter_is_passed_to_insert(self, tmp_path: Path) -> None:
        """Issue #2899 — the daemon pipes the priority through to the
        INSERT so the admin cockpit can render the priority badge in
        the active-agents and recently-completed panels.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        ok = d._atomic_claim(42, "agent-uuid", "/path", priority="p0")
        assert ok is True
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        # Priority is the second-to-last bound parameter; the last is
        # the execution_mode (migration 41, issue #3091).
        _sql, params = inserts[0]
        assert params[-2] == "p0"

    def test_priority_default_none_passes_null(self, tmp_path: Path) -> None:
        """Priority is optional; omitting it stores NULL (pre-migration-33
        fallback behaviour).
        """
        d, conn, _handler = _make_daemon(tmp_path)
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is True
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        _sql, params = inserts[0]
        # Priority = NULL (second-to-last); execution_mode = 'subprocess'
        # (last, migration 41 / #3091 default).
        assert params[-2] is None
        assert params[-1] == "subprocess"

    def test_unique_violation_returns_false(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        assert handler.events("claim_lost") != []
        assert conn.rollbacks >= 1

    def test_unexpected_error_returns_false(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("other")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        assert handler.events("claim_failed") != []

    # ── #2866 claim-interlock tests ──────────────────────────────────

    def test_happy_path_adds_status_in_progress_label(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Successful claim adds ``status/in-progress`` via ``gh issue edit``.

        Issue #2866 — the label is the human-visible half of the
        claim interlock. Add happens in :meth:`_atomic_claim` on
        happy-path success AFTER the DB commit.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        gh_edit_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "edit"]:
                gh_edit_calls.append(cmd)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is True
        # Exactly one add-label call with the in-progress label.
        assert len(gh_edit_calls) == 1
        assert "--add-label" in gh_edit_calls[0]
        assert daemon.STATUS_IN_PROGRESS_LABEL in gh_edit_calls[0]

    def test_unique_violation_with_task_skill_owner_logs_already_claimed_by_task(
        self, tmp_path: Path
    ) -> None:
        """UniqueViolation + owner.kind='task-skill' → ``candidate_skipped`` with ``already_claimed_by_task``.

        Issue #2866 — daemon queue scan observes that a /task subagent
        has already claimed the issue and logs a distinguishing reason
        so CloudWatch Logs Insights queries can count collisions
        separately from daemon↔daemon races.
        """
        d, conn, handler = _make_daemon(tmp_path)

        def insert_raises_select_returns_owner(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")
            # owner-lookup SELECT executes and returns ('task-skill',)

        conn.cursor_instance.execute = (  # type: ignore[method-assign]
            insert_raises_select_returns_owner
        )
        conn.cursor_instance.fetch_queue = [("task-skill",)]

        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        # Distinguishing event: ``candidate_skipped`` reason matches
        # ``already_claimed_by_task``. No generic ``claim_lost``.
        skipped = handler.events("candidate_skipped")
        assert skipped, "expected candidate_skipped event"
        assert skipped[0].__dict__.get("reason") == "already_claimed_by_task"
        assert handler.events("claim_lost") == []

    def test_unique_violation_with_task_owner_logs_generic_claim_lost(
        self, tmp_path: Path
    ) -> None:
        """UniqueViolation + owner.kind='task' (daemon↔daemon) → ``claim_lost``.

        Issue #2866 — preserves pre-existing semantics for the common
        daemon-on-daemon race case. Owner kind is surfaced on the log
        envelope so operators can still see who won.
        """
        d, conn, handler = _make_daemon(tmp_path)

        def insert_raises_select_returns_owner(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")

        conn.cursor_instance.execute = (  # type: ignore[method-assign]
            insert_raises_select_returns_owner
        )
        # Owner is another daemon-spawned agent (kind='task').
        conn.cursor_instance.fetch_queue = [("task",)]

        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        assert handler.events("claim_lost") != []
        assert handler.events("candidate_skipped") == []

    def test_unique_violation_with_no_owner_row_still_logs_claim_lost(
        self, tmp_path: Path
    ) -> None:
        """Edge case: partial index released between INSERT and SELECT.

        Owner lookup returns None because the row already completed +
        was indexed out. We still log ``claim_lost`` so the operator
        sees the race happened, but ``owner_kind`` is None in the
        envelope.
        """
        d, conn, handler = _make_daemon(tmp_path)

        def insert_raises(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")

        conn.cursor_instance.execute = insert_raises  # type: ignore[method-assign]
        # Empty fetch_queue → fetchone returns None → owner_kind=None.

        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        lost = handler.events("claim_lost")
        assert lost, "expected claim_lost event"
        assert lost[0].__dict__.get("owner_kind") is None


# --------------------------------------------------------------------------
# _create_worktree / git subprocess
# --------------------------------------------------------------------------


class TestCreateWorktree:
    def test_happy_path_calls_git_worktree_add(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Use a canonical uuid so the short_id is deterministic.
        agent_id = "aabbccdd-eeff-0011-2233-445566778899"

        wt = d._create_worktree(agent_id)
        # The defensive branch-delete (see #2821) runs first, then
        # ``git worktree add``.
        worktree_add = calls[-1]
        assert "git" == worktree_add[0]
        assert "worktree" in worktree_add
        assert "add" in worktree_add
        # short id = first 8 of hex-collapsed uuid = "aabbccdd".
        assert str(wt).endswith(".claude/worktrees/agent-aabbccdd")
        assert "-b" in worktree_add
        branch_idx = worktree_add.index("-b") + 1
        assert worktree_add[branch_idx] == "agent/aabbccdd"
        assert handler.events("worktree_created") != []

    def test_defensive_branch_delete_precedes_worktree_add(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Retry-path branch collision fix (#2821).

        The tier-1 retry path re-runs ``_create_worktree`` with the same
        ``agent_id``. Without a defensive ``git branch -D`` first, the
        second ``worktree add -b agent/<short_id>`` fails with ``fatal:
        a branch named 'agent/<short_id>' already exists``. This test
        asserts ``branch -D`` is called before ``worktree add``, with
        the same branch name.
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            # ``branch -D`` can legitimately return 1 when the branch
            # doesn't exist (first-attempt happy path) — the daemon
            # must ignore the exit code. Simulate that here.
            if "branch" in cmd and "-D" in cmd:
                r.returncode = 1
                r.stderr = "error: branch 'agent/aabbccdd' not found.\n"
            else:
                r.returncode = 0
                r.stderr = ""
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent_id = "aabbccdd-eeff-0011-2233-445566778899"

        # Should NOT raise despite branch -D returning 1.
        d._create_worktree(agent_id)

        # Ordering: branch -D, then worktree add.
        branch_delete_idx = next(
            (i for i, c in enumerate(calls) if "branch" in c and "-D" in c), None
        )
        worktree_add_idx = next(
            (i for i, c in enumerate(calls) if "worktree" in c and "add" in c), None
        )
        assert branch_delete_idx is not None, "no branch -D call was made"
        assert worktree_add_idx is not None, "no worktree add call was made"
        assert branch_delete_idx < worktree_add_idx
        # Branch name matches the same convention the worktree-add uses.
        branch_delete_cmd = calls[branch_delete_idx]
        assert branch_delete_cmd[-1] == "agent/aabbccdd"

    def test_git_failure_raises(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            # Only the worktree-add failure should raise — the defensive
            # branch-delete's exit code is intentionally ignored.
            if "worktree" in cmd and "add" in cmd:
                r.returncode = 128
                r.stderr = "fatal: branch already exists\n"
            else:
                r.returncode = 1
                r.stderr = ""
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        import pytest

        with pytest.raises(RuntimeError) as excinfo:
            d._create_worktree("agent-uuid-0000-0000-0000-000000000000")
        assert "exit=128" in str(excinfo.value)


# --------------------------------------------------------------------------
# _fetch_issue_bundle
# --------------------------------------------------------------------------


class TestFetchIssueBundle:
    def test_parses_gh_issue_view_output(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            assert cmd[0] == "gh" and cmd[1] == "issue" and cmd[2] == "view"
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps(
                {
                    "number": 42,
                    "title": "Title",
                    "body": "Body\nParent: #99\nBlocked by #7\n",
                    "labels": [{"name": "priority/p1"}],
                    "comments": [
                        {
                            "author": {"login": "drewthaler"},
                            "authorAssociation": "MEMBER",
                            "createdAt": "2026-04-18T00:00:00Z",
                            "body": "hi",
                        },
                        {
                            "author": {"login": "github-actions[bot]"},
                            "authorAssociation": "NONE",
                            "createdAt": "2026-04-18T00:01:00Z",
                            "body": "bot noise",
                        },
                    ],
                }
            )
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        bundle = d._fetch_issue_bundle(42)
        assert bundle["issue_number"] == 42
        assert bundle["issue_title"] == "Title"
        assert bundle["issue_labels"] == ["priority/p1"]
        assert bundle["parent_issue"] == 99
        assert bundle["blocked_by"] == [7]
        # Bot comment filtered out.
        assert len(bundle["issue_comments"]) == 1
        assert bundle["issue_comments"][0]["author"] == "drewthaler"

    def test_gh_failure_raises(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "HTTP 404\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        import pytest

        with pytest.raises(RuntimeError):
            d._fetch_issue_bundle(42)


class TestParseBlockedBy:
    def test_extracts_blocked_by_refs(self) -> None:
        body = "Intro\nBlocked by #42\nOther\nblocked by #99\n"
        assert daemon.DispatcherDaemon._parse_blocked_by(body) == [42, 99]

    def test_returns_empty_when_absent(self) -> None:
        assert daemon.DispatcherDaemon._parse_blocked_by("no refs") == []


class TestParseParentIssue:
    def test_extracts_parent(self) -> None:
        assert daemon.DispatcherDaemon._parse_parent_issue("Parent: #2782") == 2782

    def test_returns_none_when_absent(self) -> None:
        assert daemon.DispatcherDaemon._parse_parent_issue("nope") is None


class TestParsePrNumber:
    def test_parses_gh_pr_create_stdout(self) -> None:
        out = (
            "Creating pull request for head into main in judgemind/judgemind\n"
            "https://github.com/judgemind/judgemind/pull/1234\n"
        )
        assert daemon.DispatcherDaemon._parse_pr_number(out) == 1234

    def test_returns_none_when_unparseable(self) -> None:
        assert daemon.DispatcherDaemon._parse_pr_number("nope") is None


# --------------------------------------------------------------------------
# _write_phase_input / _read_phase_output
# --------------------------------------------------------------------------


class TestPhaseInputOutput:
    def test_roundtrip(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        payload = {"agent_id": "a", "issue_number": 1}
        d._write_phase_input(tmp_path, "plan", payload)
        input_path = tmp_path / "tmp" / "dispatcher-input" / "plan.json"
        assert json.loads(input_path.read_text()) == payload

        # Round-trip through _read_phase_output.
        output_dir = tmp_path / "tmp" / "dispatcher-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "plan.json").write_text(json.dumps({"go": True}))
        assert d._read_phase_output(tmp_path, "plan") == {"go": True}

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._read_phase_output(tmp_path, "plan") is None

    def test_read_malformed_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        output_dir = tmp_path / "tmp" / "dispatcher-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "plan.json").write_text("not json")
        assert d._read_phase_output(tmp_path, "plan") is None


# --------------------------------------------------------------------------
# _persist_phase_output
# --------------------------------------------------------------------------


class TestPersistPhaseOutput:
    def test_writes_phase_outputs_and_transitions(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # #2872 migration 30: persist now reads retries_used first so it
        # can pass the attempt number to the INSERT. Mock a retries_used=0
        # fetch for the pre-INSERT SELECT.
        conn.cursor_instance.fetch_queue = [(0,)]
        d._persist_phase_output("a", "plan", {"go": True})
        # Both INSERTs ran.
        insert_outputs = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        insert_transitions = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert len(insert_outputs) == 1
        assert len(insert_transitions) == 1
        # Attempt=0 passed as the 5th INSERT parameter.
        assert insert_outputs[0][1][4] == 0
        # 2 commits: one for retries_used read, one for the INSERT batch.
        assert conn.commits == 2


# --------------------------------------------------------------------------
# _spawn_phase_subprocess — builds the correct claude -p command
# --------------------------------------------------------------------------


class TestSpawnPhaseSubprocess:
    def test_builds_command_with_correct_model_and_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")

        monkeypatch.setattr(subprocess, "Popen", make_popen_factory(on_start=on_start))

        exit_code, duration = d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")
        assert exit_code == 0
        assert duration >= 0.0
        assert captured["cmd"][0] == "claude"
        assert "-p" in captured["cmd"]
        assert "/task-v2-plan agent-uuid" in captured["cmd"]
        # --cwd is NOT a claude CLI flag; the worktree goes through
        # subprocess.Popen's cwd= kwarg instead (#2821).
        assert "--cwd" not in captured["cmd"]
        assert captured["cwd"] == str(tmp_path)
        assert "--max-turns" in captured["cmd"]
        assert "500" in captured["cmd"]  # plan — 10× bumped in #2885
        assert "--model" in captured["cmd"]
        assert "opus" in captured["cmd"]
        assert handler.events("phase_started") != []
        log_path = tmp_path / "tmp" / "claude-p-plan.log"
        assert log_path.exists()

    def test_ralph_uses_sonnet_and_5000_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Ralph's max_turns was 10× bumped to 5000 in #2885."""
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], _kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd

        monkeypatch.setattr(subprocess, "Popen", make_popen_factory(on_start=on_start))
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")
        assert "5000" in captured["cmd"]
        assert "sonnet" in captured["cmd"]

    def test_summary_uses_haiku_and_300_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Summary's max_turns was 10× bumped to 300 in #2885."""
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], _kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd

        monkeypatch.setattr(subprocess, "Popen", make_popen_factory(on_start=on_start))
        d._spawn_phase_subprocess("summary", tmp_path, "agent-uuid")
        assert "300" in captured["cmd"]
        assert "haiku" in captured["cmd"]

    def test_command_includes_dangerously_skip_permissions_flag(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Issue #2982 — every phase spawn must pass
        ``--dangerously-skip-permissions`` so the in-container subagent's
        Bash-tool permission policy does not block paths like
        ``.githooks/pre-push`` that are outside the default allowlist.

        This is the fix for the #2960 regression where ralph's Step 2.5
        pre-push gate got permission-denied on six different invocation
        variants, causing four consecutive identical test failures."""
        d, _conn, _handler = _make_daemon(tmp_path)

        for phase in ("plan", "ralph", "summary", "verify", "fix-ci", "retro"):
            captured: dict[str, Any] = {}

            def on_start(
                cmd: list[str], _kwargs: dict[str, Any], _c: dict[str, Any] = captured
            ) -> None:
                _c["cmd"] = cmd

            monkeypatch.setattr(
                subprocess, "Popen", make_popen_factory(on_start=on_start)
            )
            d._spawn_phase_subprocess(phase, tmp_path, f"agent-{phase}")
            assert "--dangerously-skip-permissions" in captured["cmd"], (
                f"phase={phase} cmd missing --dangerously-skip-permissions: {captured['cmd']}"
            )


# --------------------------------------------------------------------------
# _run_subprocess_or_fail — timeout, non-zero exit, missing claude
# --------------------------------------------------------------------------


class TestRunSubprocessOrFail:
    def test_happy_path_returns_zero(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_spawn_phase_subprocess", lambda *a, **k: (0, 1.2))
        assert d._run_subprocess_or_fail("a", "plan", tmp_path) == 0

    def test_timeout_marks_failed_returns_none(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=10)

        monkeypatch.setattr(d, "_spawn_phase_subprocess", boom)
        result = d._run_subprocess_or_fail("a", "plan", tmp_path)
        assert result is None
        fails = handler.events("subprocess_failed")
        assert fails and fails[0].reason == "timeout"
        # Agent marked failed — UPDATE on dispatcher.agents ran.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert updates

    def test_claude_missing_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise FileNotFoundError(2, "nope", "claude")

        monkeypatch.setattr(d, "_spawn_phase_subprocess", boom)
        assert d._run_subprocess_or_fail("a", "plan", tmp_path) is None
        fails = handler.events("subprocess_failed")
        assert fails and fails[0].reason == "claude_not_on_path"

    def test_nonzero_exit_marks_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_spawn_phase_subprocess", lambda *a, **k: (1, 0.1))
        assert d._run_subprocess_or_fail("a", "plan", tmp_path) is None
        fails = handler.events("subprocess_failed")
        assert fails and fails[0].exit_code == 1


# --------------------------------------------------------------------------
# Full happy-path orchestration — plan → ralph → summary → push → PR
# --------------------------------------------------------------------------


def _fixture_plan_output() -> dict[str, Any]:
    return {
        "agent_id": "a",
        "issue_number": 42,
        "go": True,
        "block_reason": None,
        "plan_text": "plan",
        "acceptance_criteria": ["AC1", "AC2"],
        "scope_check": [],
        "relevant_files": [],
        "relevant_docs": [],
        "change_type": "scraper",
        "dependencies_to_install": ["scraper-framework"],
    }


def _fixture_ralph_output() -> dict[str, Any]:
    return {
        "agent_id": "a",
        "issue_number": 42,
        "verdict": "SHIP",
        "iterations_used": 1,
        "block_reason": None,
        "changed_files": ["packages/scraper-framework/src/thing.py"],
        "summary": "Fixed a thing",
        "ralph_done_path": None,
        "review_log_path": None,
    }


def _fixture_summary_output() -> dict[str, Any]:
    return {
        "agent_id": "a",
        "issue_number": 42,
        "process_summary_md": "## Process Summary\n...",
        "commit_message": "fix(scraping): fix thing (#42)",
        "pr_title": "fix(scraping): fix thing (#42)",
        "pr_body_md": "## Summary\n\nFix.\n\nCloses #42\n",
        "unmet_criteria": [],
        "pre_pr_check_notes": "",
    }


class TestHappyPathOrchestration:
    """End-to-end: _claim_and_orchestrate_one through to gh pr create."""

    def test_claims_runs_phases_pushes_and_opens_pr(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Named fetch_responses dispatcher (issue #2793) — robust against
        # positional reordering of DB calls.
        # (1) Phase 3C resume-retry SELECT — no retrying agent → None.
        # (2) Latest queue_snapshot row: issues_json=None triggers the
        #     pre-#2820 fallback that uses the raw issue_numbers array
        #     (issue #2835).
        # (3) _issue_already_attempted SELECT — not attempted → None.
        conn.cursor_instance.fetch_responses = {
            "status = 'retrying'": None,
            "FROM dispatcher.queue_snapshots": (None, [42]),
            "FROM dispatcher.agents WHERE issue_number": None,
        }

        # Trust check passes.
        def fake_check_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "TRUSTED: ...\n"
            r.stderr = ""
            return r

        # Deterministic agent_id for short-id verification.
        fixed_uuid = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(uuid_mod, "uuid4", lambda: fixed_uuid)
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed_uuid)

        # Repo root = tmp_path so worktrees land under it.
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        # Track subprocess calls to assert the sequence.
        call_log: list[list[str]] = []

        # _spawn_phase_subprocess is mocked below to write the fixture
        # phase output file and return exit=0.

        def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            # Trust check path.
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            # gh issue view path (bundle fetch — called twice: plan + summary).
            if (
                len(cmd) >= 3
                and cmd[0] == "gh"
                and cmd[1] == "issue"
                and cmd[2] == "view"
            ):
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "Title",
                        "body": "body",
                        "labels": [{"name": "priority/p1"}],
                        "comments": [],
                    }
                )
                return r
            # git worktree add — pretend it succeeded. Create the dir
            # so later _write_phase_input / _read_phase_output work.
            if "worktree" in cmd and "add" in cmd:
                # Find the path in cmd (it's right after "add").
                add_idx = cmd.index("add")
                wt = Path(cmd[add_idx + 1])
                wt.mkdir(parents=True, exist_ok=True)
                return r
            # gh pr create — return URL stdout.
            if cmd[:3] == ["gh", "pr", "create"]:
                r.stdout = "https://github.com/judgemind/judgemind/pull/9001\n"
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

        # Mock the three phase subprocesses — each writes the expected
        # output file and returns (0, duration).
        phase_outputs = {
            "plan": _fixture_plan_output(),
            "ralph": _fixture_ralph_output(),
            "summary": _fixture_summary_output(),
        }

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            output_dir_local = worktree / "tmp" / "dispatcher-output"
            output_dir_local.mkdir(parents=True, exist_ok=True)
            (output_dir_local / f"{phase}.json").write_text(
                json.dumps(phase_outputs[phase])
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        # Run orchestration.
        d._claim_and_orchestrate_one()

        # --- Assertions ---

        # Candidate picked + claim succeeded.
        assert handler.events("candidate_picked") != []
        assert handler.events("claim_succeeded") != []

        # Worktree created.
        assert handler.events("worktree_created") != []

        # Three phase_succeeded events (plan, ralph, summary).
        phases_succeeded = [r.phase for r in handler.events("phase_succeeded")]
        assert phases_succeeded == ["plan", "ralph", "summary"]

        # PR opened event captured with PR number.
        pr_opened = handler.events("pr_opened")
        assert pr_opened and pr_opened[0].pr_number == 9001

        # dispatcher.agents INSERT (claim) happened once.
        agent_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        assert len(agent_inserts) == 1

        # dispatcher.phase_outputs INSERTs: 3 (one per phase).
        phase_output_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(phase_output_inserts) == 3

        # dispatcher.phase_transitions INSERTs: 3.
        phase_transition_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert len(phase_transition_inserts) == 3

        # Final UPDATE: status='running', phase='awaiting_ci', pr_number=9001.
        final_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "running" in e[1]
            and "awaiting_ci" in e[1]
        ]
        assert final_updates, (
            f"expected final UPDATE with (running, awaiting_ci, pr=9001); "
            f"got: {[e[1] for e in conn.cursor_instance.executed if 'UPDATE dispatcher.agents' in e[0]]}"
        )

        # git commands: worktree add, commit --amend, push. All use
        # ``git -C <repo-or-worktree> <verb>`` shape, so just check the
        # verb appears anywhere in each command.
        #
        # Issue #2971: the happy-path sequence no longer runs ``git add
        # -A``. Ralph's Step 2.5 committed its work with the placeholder
        # message; the daemon's commit step is ``git commit --amend -F
        # <file>`` which rewrites the message without touching the index.
        git_cmds = [c for c in call_log if c and c[0] == "git"]
        flat = [" ".join(c) for c in git_cmds]
        assert any("worktree add" in s for s in flat)
        assert any("commit --amend" in s for s in flat), (
            f"Expected ``git commit --amend`` after #2971; got: {flat}"
        )
        assert any(" push " in s for s in flat)
        # Guard: no stray ``git add -A`` call (would be a regression
        # toward the pre-#2971 flow that swallowed ralph's diff on an
        # incomplete undo).
        assert not any("add -A" in s for s in flat), (
            f"Unexpected ``git add -A`` call after #2971; got: {flat}"
        )


# --------------------------------------------------------------------------
# Branch: plan.go=false stops orchestration cleanly
# --------------------------------------------------------------------------


class TestPlanGoFalse:
    def test_plan_go_false_with_block_reason_marks_plan_blocked(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """plan.go=false + populated block_reason → status='plan_blocked' (#2857).

        Previously this path set status='failed'. #2857 introduced the
        distinct terminal ``plan_blocked`` state for "plan correctly
        declined to proceed" — reserved ``failed`` for real
        infrastructure/subprocess failures.
        """
        d, conn, handler = _make_daemon(tmp_path)
        # Fetches in order: (1) Phase 3C resume-retry SELECT — no
        # retrying agent; (2) latest queue_snapshot issue_numbers;
        # (3) _issue_already_attempted SELECT — not attempted.
        # Queue snapshot read now returns (issues_json, issue_numbers) —
        # issues_json=None triggers the pre-#2820 fallback which uses
        # the raw issue_numbers array (issue #2835).
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]

        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        plan_blocked = dict(_fixture_plan_output())
        plan_blocked["go"] = False
        plan_blocked["block_reason"] = "acceptance criteria need sharpening"

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(plan_blocked)
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # Plan executed; ralph/summary did NOT.
        phases_succeeded = [r.phase for r in handler.events("phase_succeeded")]
        assert phases_succeeded == ["plan"]
        plan_go_false_events = handler.events("plan_go_false")
        assert plan_go_false_events != []
        assert plan_go_false_events[0].terminal_status == "plan_blocked"
        # Final UPDATE sets status='plan_blocked' with phase='planning'.
        plan_blocked_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "plan_blocked" in e[1]
        ]
        assert plan_blocked_updates
        # Side effects: comment posted, labels swapped.
        assert handler.events("plan_blocked_comment_posted") != []
        assert handler.events("plan_blocked_labels_swapped") != []

    def test_plan_go_false_no_block_reason_marks_succeeded(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """plan.go=false with empty block_reason → 'no work needed' = succeeded.

        Preserves the pre-#2857 no-work path: when plan returns
        go=false WITHOUT a block_reason, the interpretation is "there
        is no work for this issue" (e.g. already fixed, duplicate), and
        the agent terminates as ``succeeded``. No comment is posted
        and no labels are swapped — the agent simply exits cleanly.
        """
        d, conn, handler = _make_daemon(tmp_path)
        # Fetches in order: (1) Phase 3C resume-retry SELECT — no
        # retrying agent; (2) latest queue_snapshot issue_numbers;
        # (3) _issue_already_attempted SELECT — not attempted.
        # Queue snapshot read now returns (issues_json, issue_numbers) —
        # issues_json=None triggers the pre-#2820 fallback which uses
        # the raw issue_numbers array (issue #2835).
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        plan_nowork = dict(_fixture_plan_output())
        plan_nowork["go"] = False
        plan_nowork["block_reason"] = None  # no work needed

        gh_comment_calls: list[list[str]] = []
        gh_edit_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                gh_comment_calls.append(cmd)
                return r
            if cmd[:3] == ["gh", "issue", "edit"]:
                gh_edit_calls.append(cmd)
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(plan_nowork)
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # Final UPDATE sets status='succeeded'.
        succeeded_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "succeeded" in e[1]
        ]
        assert succeeded_updates
        # No plan_blocked side effects on the no-work path.
        assert gh_comment_calls == []
        # #2866 claim-interlock label calls ARE expected here:
        # the ``status/in-progress`` label is added on claim and removed
        # on terminal. Filter them out before asserting "no plan_blocked
        # label swap happened".
        non_interlock_edits = [
            call for call in gh_edit_calls if "status/in-progress" not in call
        ]
        assert non_interlock_edits == []
        assert handler.events("plan_blocked_comment_posted") == []
        assert handler.events("plan_blocked_labels_swapped") == []


# --------------------------------------------------------------------------
# #2857: plan_blocked side effects (comment + label swap) —
# direct tests of the helper methods, independent of the full orchestrator
# path. Exercises the comment body template, idempotence via sentinel,
# and the "each side effect wrapped independently" contract.
# --------------------------------------------------------------------------


class TestPlanBlockedHandler:
    """Direct tests for ``_handle_plan_blocked`` + its subordinate helpers.

    The orchestrator-level ``TestPlanGoFalse`` above exercises the
    wiring through ``_claim_and_orchestrate_one``. These tests focus on
    the helper methods in isolation so each side effect (comment post,
    label swap) can be asserted cleanly without dragging in the whole
    queue-snapshot / worktree-add / phase-spawn machinery.
    """

    def _make_handler_daemon(
        self, tmp_path: Path
    ) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
        """Make a daemon with the .tmp directory layout _post_plan_blocked_comment expects."""
        return _make_daemon(tmp_path)

    def test_render_plan_blocked_comment_has_sentinel_on_line_one(
        self, tmp_path: Path
    ) -> None:
        """AC2: sentinel MUST be line 1 so a future operator tool can dedupe."""
        d, _conn, _handler = self._make_handler_daemon(tmp_path)
        body = d._render_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", "reason"
        )
        first_line = body.splitlines()[0]
        assert first_line == "<!-- dispatcher-plan-blocked -->"

    def test_render_plan_blocked_comment_includes_block_reason_as_blockquote(
        self, tmp_path: Path
    ) -> None:
        """Multi-line block_reason rendered as a valid markdown blockquote."""
        d, _conn, _handler = self._make_handler_daemon(tmp_path)
        reason = "Issue bundles three distinct tracks.\n\nEach needs its own issue."
        body = d._render_plan_blocked_comment("abcdef0012345678", reason)
        # First line of the reason gets "> " prefix, blank line becomes ">"
        # (keeps blockquote valid across blank lines).
        assert "> Issue bundles three distinct tracks." in body
        assert ">\n" in body
        assert "> Each needs its own issue." in body

    def test_render_plan_blocked_comment_includes_short_agent_id(
        self, tmp_path: Path
    ) -> None:
        """Operators can correlate the comment with a CloudWatch / DB row."""
        d, _conn, _handler = self._make_handler_daemon(tmp_path)
        body = d._render_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", "reason"
        )
        # Short id is first 8 chars of the UUID with dashes removed.
        assert "`aabbccdd`" in body

    def test_post_plan_blocked_comment_skips_when_sentinel_already_present(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Idempotence: pre-existing sentinel → no comment post, returns True."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        gh_comment_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "comments": [
                            {"body": "<!-- dispatcher-plan-blocked -->\n## Prior\n"}
                        ]
                    }
                )
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                gh_comment_calls.append(cmd)
                r.stdout = ""
                return r
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", 42, "reason", worktree
        )

        assert ok is True
        assert gh_comment_calls == []
        assert handler.events("plan_blocked_comment_skipped_idempotent") != []

    def test_post_plan_blocked_comment_posts_when_sentinel_absent(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Fresh issue: sentinel absent → comment posted with --body-file."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        gh_comment_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps({"comments": []})
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                gh_comment_calls.append(cmd)
                r.stdout = ""
                return r
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", 42, "block_reason", worktree
        )

        assert ok is True
        assert len(gh_comment_calls) == 1
        assert "--body-file" in gh_comment_calls[0]
        # Body file is written and contains the sentinel.
        body_idx = gh_comment_calls[0].index("--body-file") + 1
        body_path = Path(gh_comment_calls[0][body_idx])
        body = body_path.read_text(encoding="utf-8")
        assert body.startswith("<!-- dispatcher-plan-blocked -->")
        assert "> block_reason" in body
        assert handler.events("plan_blocked_comment_posted") != []

    def test_post_plan_blocked_comment_returns_false_on_gh_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """gh issue comment non-zero → returns False, error logged."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.stdout = ""
            r.stderr = "api error"
            if cmd[:3] == ["gh", "issue", "view"]:
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                r.returncode = 1
                return r
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", 42, "reason", worktree
        )
        assert ok is False
        assert handler.events("plan_blocked_comment_failed") != []

    def test_swap_plan_blocked_labels_calls_gh_issue_edit(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Happy path: gh issue edit --remove-label agent/ready --add-label status/triage."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "edit"]:
                calls.append(cmd)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        ok = d._swap_plan_blocked_labels("aabbccdd-eeff-0011-2233-445566778899", 42)

        assert ok is True
        assert len(calls) == 1
        assert "--remove-label" in calls[0]
        assert "agent/ready" in calls[0]
        assert "--add-label" in calls[0]
        assert "status/triage" in calls[0]
        assert handler.events("plan_blocked_labels_swapped") != []

    def test_swap_plan_blocked_labels_returns_false_on_gh_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """gh issue edit non-zero → False, error logged."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "api error"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        ok = d._swap_plan_blocked_labels("aabbccdd-eeff-0011-2233-445566778899", 42)
        assert ok is False
        assert handler.events("plan_blocked_labels_failed") != []

    def test_plan_blocked_sentinel_check_treats_gh_error_as_unknown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``gh issue view`` non-zero → sentinel check returns None.

        Fail-open: on a GitHub outage the cost of a duplicate comment
        is lower than the cost of silently dropping the operator
        signal. The caller proceeds with the post attempt.
        """
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.stdout = ""
            r.stderr = "api error"
            r.returncode = 1
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._plan_blocked_comment_already_posted(42)
        assert result is None
        assert handler.events("plan_blocked_sentinel_check_failed") != []

    def test_plan_blocked_sentinel_check_treats_timeout_as_unknown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """subprocess TimeoutExpired on sentinel check → returns None."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._plan_blocked_comment_already_posted(42)
        assert result is None
        assert handler.events("plan_blocked_sentinel_check_failed") != []

    def test_plan_blocked_sentinel_check_treats_malformed_json_as_unknown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Malformed JSON from ``gh issue view`` → returns None."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "not-json"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._plan_blocked_comment_already_posted(42)
        assert result is None
        assert handler.events("plan_blocked_sentinel_check_failed") != []

    def test_plan_blocked_sentinel_check_skips_non_dict_comments(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Non-dict entries in the ``comments`` array are skipped safely."""
        d, _conn, _handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps(
                {
                    "comments": [
                        "not-a-dict",
                        42,
                        None,
                        {"body": "ordinary comment"},
                    ]
                }
            )
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Sentinel not present among the valid dict entry → False.
        result = d._plan_blocked_comment_already_posted(42)
        assert result is False

    def test_post_plan_blocked_comment_handles_timeout(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """subprocess TimeoutExpired on ``gh issue comment`` → returns False, logs."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[:3] == ["gh", "issue", "view"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                r.stderr = ""
                return r
            # gh issue comment → timeout
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", 42, "reason", worktree
        )
        assert ok is False
        assert handler.events("plan_blocked_comment_failed") != []

    def test_swap_plan_blocked_labels_handles_timeout(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """subprocess TimeoutExpired on ``gh issue edit`` → False, logs."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(subprocess, "run", fake_run)

        ok = d._swap_plan_blocked_labels("aabbccdd-eeff-0011-2233-445566778899", 42)
        assert ok is False
        assert handler.events("plan_blocked_labels_failed") != []

    def test_render_plan_blocked_comment_handles_empty_reason(
        self, tmp_path: Path
    ) -> None:
        """Edge case: empty block_reason still produces valid markdown.

        The orchestrator path never calls this with an empty reason
        (that branch routes to ``succeeded``), but defensive tests
        prevent the helper from regressing into crash-on-empty.
        """
        d, _conn, _handler = self._make_handler_daemon(tmp_path)
        body = d._render_plan_blocked_comment("aabbccdd" + "0" * 24, "")
        # Sentinel still on line 1, blockquote still present.
        assert body.startswith("<!-- dispatcher-plan-blocked -->")
        assert "`aabbccdd`" in body

    def test_post_plan_blocked_comment_handles_body_write_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """OSError writing the body file → returns False, logs the error.

        Exercises the mkdir/write_text error-handling arm so
        filesystem-level failures (disk full, permission denied) don't
        crash the orchestration.
        """
        d, _conn, handler = self._make_handler_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            # Sentinel check returns "not present".
            if cmd[:3] == ["gh", "issue", "view"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                r.stderr = ""
                return r
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Pass a worktree whose ``tmp`` path already exists as a file —
        # ``mkdir(parents=True, exist_ok=True)`` raises NotADirectoryError
        # (an OSError subclass) because it cannot create a directory
        # whose parent is a non-directory file.
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create a regular file where the ``tmp`` directory would be.
        (worktree / "tmp").write_text("not-a-dir")

        ok = d._post_plan_blocked_comment(
            "aabbccdd-eeff-0011-2233-445566778899", 42, "reason", worktree
        )
        assert ok is False
        assert handler.events("plan_blocked_comment_failed") != []

    def test_handle_plan_blocked_comment_failure_does_not_block_label_swap(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Each side effect is independently wrapped: comment fail → labels still run."""
        d, _conn, handler = self._make_handler_daemon(tmp_path)
        label_edit_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.stdout = ""
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "view"]:
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                r.returncode = 1
                return r
            if cmd[:3] == ["gh", "issue", "edit"]:
                r.returncode = 0
                label_edit_calls.append(cmd)
                return r
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Handler returns None, does not raise.
        result = d._handle_plan_blocked(
            "aabbccdd-eeff-0011-2233-445566778899", 42, "reason", worktree
        )
        assert result is None
        # Even though the comment failed, the label swap still ran.
        assert len(label_edit_calls) == 1
        assert handler.events("plan_blocked_comment_failed") != []
        assert handler.events("plan_blocked_labels_swapped") != []

    def test_plan_go_false_label_swap_fails_but_status_still_runs(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Orchestrator path: label swap fails → DB still writes plan_blocked.

        The DB update is the authoritative terminal-status write. A
        label-swap failure (e.g. GitHub outage, token expired) must not
        prevent ``dispatcher.agents.status='plan_blocked'`` — otherwise
        the agent row would be stuck in ``phase=planning`` forever and
        the cooldown loop would not trip the stale-agent cleaner.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]

        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        plan_blocked = dict(_fixture_plan_output())
        plan_blocked["go"] = False
        plan_blocked["block_reason"] = "reason"

        issue_view_call_count = 0

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            nonlocal issue_view_call_count
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                # First call is the issue-bundle fetch (plan input);
                # subsequent calls are the sentinel check. Both return
                # empty comments.
                issue_view_call_count += 1
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if cmd[:3] == ["gh", "issue", "edit"]:
                # Label swap: fail.
                r.returncode = 1
                r.stderr = "api error"
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(plan_blocked)
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # DB still got the terminal-status update.
        plan_blocked_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "plan_blocked" in e[1]
        ]
        assert plan_blocked_updates
        # Label swap failure was logged.
        assert handler.events("plan_blocked_labels_failed") != []
        # Comment still got posted.
        assert handler.events("plan_blocked_comment_posted") != []


# --------------------------------------------------------------------------
# Branch: ralph verdict=BLOCKED stops orchestration cleanly
# --------------------------------------------------------------------------


class TestRalphBlocked:
    def test_ralph_blocked_marks_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Fetches in order: (1) Phase 3C resume-retry SELECT — no
        # retrying agent; (2) latest queue_snapshot issue_numbers;
        # (3) _issue_already_attempted SELECT — not attempted.
        # Queue snapshot read now returns (issues_json, issue_numbers) —
        # issues_json=None triggers the pre-#2820 fallback which uses
        # the raw issue_numbers array (issue #2835).
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        ralph_blocked = dict(_fixture_ralph_output())
        ralph_blocked["verdict"] = "BLOCKED"
        ralph_blocked["block_reason"] = "max_iterations reached"

        outputs = {
            "plan": _fixture_plan_output(),
            "ralph": ralph_blocked,
        }

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(outputs[phase])
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # plan + ralph succeeded (phase subprocess exited 0) but ralph
        # returned verdict=BLOCKED so summary was not invoked.
        phases_succeeded = [r.phase for r in handler.events("phase_succeeded")]
        assert phases_succeeded == ["plan", "ralph"]
        assert handler.events("ralph_not_ship") != []
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates


# --------------------------------------------------------------------------
# Subprocess non-zero exit marks failed cleanly without crashing
# --------------------------------------------------------------------------


class TestSubprocessNonZeroExit:
    def test_plan_subprocess_nonzero_exit_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Fetches in order: (1) Phase 3C resume-retry SELECT — no
        # retrying agent; (2) latest queue_snapshot issue_numbers;
        # (3) _issue_already_attempted SELECT — not attempted.
        # Queue snapshot read now returns (issues_json, issue_numbers) —
        # issues_json=None triggers the pre-#2820 fallback which uses
        # the raw issue_numbers array (issue #2835).
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            # Simulate infra failure (claude-p crash).
            return 1, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # Subprocess failure logged; no plan phase_output persisted;
        # agent marked failed.
        assert handler.events("subprocess_failed") != []
        phase_output_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert phase_output_inserts == []


# --------------------------------------------------------------------------
# Race-lost path: _atomic_claim unique violation stops this tick's attempt
# --------------------------------------------------------------------------


class TestClaimRace:
    def test_unique_violation_skips_this_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Snapshot has one issue, not-attempted check returns None.
        # Fetches in order: (1) Phase 3C resume-retry SELECT — no
        # retrying agent; (2) latest queue_snapshot issue_numbers;
        # (3) _issue_already_attempted SELECT — not attempted.
        # Queue snapshot read now returns (issues_json, issue_numbers) —
        # issues_json=None triggers the pre-#2820 fallback which uses
        # the raw issue_numbers array (issue #2835).
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        # Trust check passes.
        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "TRUSTED: ...\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Make the INSERT raise UniqueViolation — another daemon won.
        original_execute = conn.cursor_instance.execute

        def execute_with_uv(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")
            original_execute(sql, params)

        conn.cursor_instance.execute = execute_with_uv  # type: ignore[method-assign]

        # Also make sure we don't try to create a worktree on lost-race.
        monkeypatch.setattr(
            d,
            "_create_worktree",
            MagicMock(side_effect=AssertionError("should not create worktree")),
        )

        d._claim_and_orchestrate_one()

        assert handler.events("claim_lost") != []
        # No worktree created, no phase output persisted.
        phase_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert phase_inserts == []


# --------------------------------------------------------------------------
# #2856: summary_unmet_criteria opens a DRAFT PR + needs_review terminal
#
# Previously (pre-#2856) summary_unmet_criteria discarded all ralph work
# by marking the agent failed with no PR. Now the daemon preserves
# ralph's reviewer-approved output by opening a DRAFT PR with an
# "Unmet acceptance criteria" section appended to the body, posts an
# issue comment linking the draft, and marks the agent
# ``status='needs_review'`` (a new terminal distinct from ``failed``).
#
# Tests cover: helper rendering (AC3: section text + sentinel); orchestration
# path (AC1: draft flag, AC2: PR body section, AC4: status='needs_review',
# AC5: issue comment with PR link). Parallel structure to the
# plan_blocked tests above (#2857).
# --------------------------------------------------------------------------


class TestNeedsReviewHelpers:
    """Direct tests for the needs_review rendering helpers (#2856)."""

    def test_render_unmet_criteria_pr_section_includes_warning_heading(
        self, tmp_path: Path
    ) -> None:
        """AC2: PR body section must include the ⚠️ Unmet AC heading."""
        d, _conn, _handler = _make_daemon(tmp_path)
        section = d._render_unmet_criteria_pr_section(["AC1 — foo", "AC2 — bar"])
        # "\u26a0\ufe0f" is the warning-sign emoji (U+26A0 U+FE0F).
        assert "\u26a0\ufe0f Unmet acceptance criteria" in section
        # Each criterion rendered as a bullet verbatim.
        assert "- AC1 \u2014 foo" in section
        assert "- AC2 \u2014 bar" in section

    def test_render_unmet_criteria_pr_section_drops_empty_entries(
        self, tmp_path: Path
    ) -> None:
        """Whitespace-only entries are dropped so the rendered list stays clean."""
        d, _conn, _handler = _make_daemon(tmp_path)
        section = d._render_unmet_criteria_pr_section(["real AC", "   ", ""])
        assert "- real AC" in section
        # Blank bullets must not appear.
        assert "- \n" not in section
        assert "- \n- " not in section

    def test_render_unmet_criteria_pr_section_handles_all_empty(
        self, tmp_path: Path
    ) -> None:
        """Degenerate list → still renders the heading so the PR body is valid md."""
        d, _conn, _handler = _make_daemon(tmp_path)
        section = d._render_unmet_criteria_pr_section(["", "   "])
        # Defensive fallback — should never happen via the normal path
        # (the caller only invokes on non-empty) but keeps the helper total.
        assert "\u26a0\ufe0f Unmet acceptance criteria" in section
        assert "(none recorded)" in section

    def test_render_needs_review_comment_has_sentinel_on_line_one(
        self, tmp_path: Path
    ) -> None:
        """AC2/AC3: sentinel MUST be line 1 so a future dedupe can detect."""
        d, _conn, _handler = _make_daemon(tmp_path)
        body = d._render_needs_review_comment(
            "aabbccdd-eeff-0011-2233-445566778899",
            1234,
            "https://github.com/judgemind/judgemind/pull/1234",
            ["AC1 missing"],
        )
        first_line = body.splitlines()[0]
        assert first_line == "<!-- dispatcher-needs-review -->"

    def test_render_needs_review_comment_includes_pr_link(self, tmp_path: Path) -> None:
        """AC4: the issue comment MUST link the draft PR."""
        d, _conn, _handler = _make_daemon(tmp_path)
        body = d._render_needs_review_comment(
            "aabbccdd-eeff-0011-2233-445566778899",
            1234,
            "https://github.com/judgemind/judgemind/pull/1234",
            ["AC1 missing"],
        )
        # GitHub autolinks the raw URL.
        assert "https://github.com/judgemind/judgemind/pull/1234" in body

    def test_render_needs_review_comment_falls_back_to_pr_number_on_missing_url(
        self, tmp_path: Path
    ) -> None:
        """If ``gh pr create`` stdout parsing fails, fall back to #<num>."""
        d, _conn, _handler = _make_daemon(tmp_path)
        body = d._render_needs_review_comment(
            "aabbccdd-eeff-0011-2233-445566778899",
            1234,
            "",
            ["AC1 missing"],
        )
        assert "#1234" in body

    def test_render_needs_review_comment_includes_unmet_bullets(
        self, tmp_path: Path
    ) -> None:
        """Each unmet criterion rendered as its own bullet in the comment."""
        d, _conn, _handler = _make_daemon(tmp_path)
        body = d._render_needs_review_comment(
            "aabbccdd-eeff-0011-2233-445566778899",
            1234,
            "https://github.com/judgemind/judgemind/pull/1234",
            ["AC1 real fixture missing", "AC2 no deploy evidence"],
        )
        assert "- AC1 real fixture missing" in body
        assert "- AC2 no deploy evidence" in body

    def test_render_needs_review_comment_includes_short_agent_id(
        self, tmp_path: Path
    ) -> None:
        """Operators can correlate comment with CloudWatch / DB row."""
        d, _conn, _handler = _make_daemon(tmp_path)
        body = d._render_needs_review_comment(
            "aabbccdd-eeff-0011-2233-445566778899",
            1234,
            "https://github.com/judgemind/judgemind/pull/1234",
            ["AC1"],
        )
        # First 8 hex chars of the UUID with dashes stripped.
        assert "`aabbccdd`" in body


class TestNeedsReviewCommentHandler:
    """Direct tests for the needs_review comment-posting helpers (#2856)."""

    def test_post_needs_review_comment_skips_when_sentinel_already_present(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Idempotence: pre-existing sentinel → no comment post, returns True."""
        d, _conn, handler = _make_daemon(tmp_path)

        gh_comment_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "comments": [
                            {"body": "<!-- dispatcher-needs-review -->\n## Prior\n"}
                        ]
                    }
                )
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                gh_comment_calls.append(cmd)
                r.stdout = ""
                return r
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_needs_review_comment(
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            issue_number=42,
            pr_number=1234,
            pr_url="https://github.com/judgemind/judgemind/pull/1234",
            unmet_criteria=["AC1"],
            worktree=worktree,
        )

        assert ok is True
        assert gh_comment_calls == []
        assert handler.events("needs_review_comment_skipped_idempotent") != []

    def test_post_needs_review_comment_posts_when_sentinel_absent(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Fresh issue: sentinel absent → comment posted with --body-file."""
        d, _conn, handler = _make_daemon(tmp_path)
        gh_comment_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps({"comments": []})
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                gh_comment_calls.append(cmd)
                r.stdout = ""
                return r
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_needs_review_comment(
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            issue_number=42,
            pr_number=1234,
            pr_url="https://github.com/judgemind/judgemind/pull/1234",
            unmet_criteria=["AC1 missing"],
            worktree=worktree,
        )

        assert ok is True
        assert len(gh_comment_calls) == 1
        assert "--body-file" in gh_comment_calls[0]
        body_idx = gh_comment_calls[0].index("--body-file") + 1
        body_path = Path(gh_comment_calls[0][body_idx])
        body = body_path.read_text(encoding="utf-8")
        # Sentinel on line 1, PR link present, bullet present.
        assert body.startswith("<!-- dispatcher-needs-review -->")
        assert "https://github.com/judgemind/judgemind/pull/1234" in body
        assert "- AC1 missing" in body
        assert handler.events("needs_review_comment_posted") != []

    def test_post_needs_review_comment_returns_false_on_gh_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """gh issue comment non-zero → returns False, error logged."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.stdout = ""
            r.stderr = "api error"
            if cmd[:3] == ["gh", "issue", "view"]:
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                r.returncode = 1
                return r
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_needs_review_comment(
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            issue_number=42,
            pr_number=1234,
            pr_url="https://github.com/judgemind/judgemind/pull/1234",
            unmet_criteria=["AC1"],
            worktree=worktree,
        )
        assert ok is False
        assert handler.events("needs_review_comment_failed") != []

    def test_needs_review_sentinel_check_treats_gh_error_as_unknown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``gh issue view`` non-zero → sentinel check returns None (fail-open)."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "api error"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._needs_review_comment_already_posted(42)
        assert result is None
        assert handler.events("needs_review_sentinel_check_failed") != []

    def test_needs_review_sentinel_check_treats_timeout_as_unknown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """subprocess TimeoutExpired on sentinel check → returns None."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._needs_review_comment_already_posted(42)
        assert result is None
        assert handler.events("needs_review_sentinel_check_failed") != []

    def test_needs_review_sentinel_check_treats_malformed_json_as_unknown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Malformed JSON from ``gh issue view`` → returns None."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "not-json"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = d._needs_review_comment_already_posted(42)
        assert result is None
        assert handler.events("needs_review_sentinel_check_failed") != []

    def test_needs_review_sentinel_check_skips_non_dict_comments(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Non-dict entries in the comments array are skipped safely."""
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps(
                {
                    "comments": [
                        "not-a-dict",
                        42,
                        None,
                        {"body": "ordinary comment"},
                    ]
                }
            )
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert d._needs_review_comment_already_posted(42) is False

    def test_post_needs_review_comment_handles_timeout(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """subprocess TimeoutExpired on ``gh issue comment`` → returns False, logs."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[:3] == ["gh", "issue", "view"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                r.stderr = ""
                return r
            # gh issue comment → timeout
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        ok = d._post_needs_review_comment(
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            issue_number=42,
            pr_number=1234,
            pr_url="https://github.com/judgemind/judgemind/pull/1234",
            unmet_criteria=["AC1"],
            worktree=worktree,
        )
        assert ok is False
        assert handler.events("needs_review_comment_failed") != []

    def test_post_needs_review_comment_handles_body_write_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """OSError writing the body file → returns False, logs error."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[:3] == ["gh", "issue", "view"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = json.dumps({"comments": []})
                r.stderr = ""
                return r
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Block the tmp/ directory creation by planting a file at that path.
        (worktree / "tmp").write_text("not-a-dir")

        ok = d._post_needs_review_comment(
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            issue_number=42,
            pr_number=1234,
            pr_url="https://github.com/judgemind/judgemind/pull/1234",
            unmet_criteria=["AC1"],
            worktree=worktree,
        )
        assert ok is False
        assert handler.events("needs_review_comment_failed") != []


class TestNeedsReviewOrchestration:
    """End-to-end: summary with ``unmet_criteria`` → draft PR + needs_review (#2856)."""

    def test_summary_unmet_opens_draft_pr_and_marks_needs_review(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC1/AC3/AC4: draft flag in gh create, status='needs_review', phase='needs_review'.

        Previously summary_unmet_criteria hard-failed with no PR.
        This test locks in the new contract: ralph's output is
        preserved as a draft PR, agent terminates ``needs_review``,
        and the PR body contains the ⚠️ Unmet AC section.
        """
        d, conn, handler = _make_daemon(tmp_path)
        # (1) Phase 3C resume — no retrying; (2) queue snapshot; (3) not-attempted.
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]

        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        summary_unmet = dict(_fixture_summary_output())
        summary_unmet["unmet_criteria"] = [
            "AC1 — real fixture must be committed",
            "AC2 — deploy evidence posted",
        ]

        phase_outputs = {
            "plan": _fixture_plan_output(),
            "ralph": _fixture_ralph_output(),
            "summary": summary_unmet,
        }

        gh_pr_create_calls: list[list[str]] = []
        gh_issue_comment_calls: list[list[str]] = []
        gh_issue_view_count = 0

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            nonlocal gh_issue_view_count
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                gh_issue_view_count += 1
                # All gh issue view calls (plan bundle + summary bundle +
                # needs_review sentinel check) return an empty comments
                # list so the sentinel check falls through to the post.
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "Title",
                        "body": "body",
                        "labels": [{"name": "priority/p1"}],
                        "comments": [],
                    }
                )
                return r
            if cmd[:3] == ["gh", "pr", "create"]:
                gh_pr_create_calls.append(cmd)
                r.stdout = "https://github.com/judgemind/judgemind/pull/9001\n"
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                gh_issue_comment_calls.append(cmd)
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            output_dir = worktree / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{phase}.json").write_text(json.dumps(phase_outputs[phase]))
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # --- AC1: draft flag present on gh pr create ---
        assert len(gh_pr_create_calls) == 1
        assert "--draft" in gh_pr_create_calls[0]

        # --- AC2: PR body section present with unmet criteria ---
        body_idx = gh_pr_create_calls[0].index("--body-file") + 1
        body_path = Path(gh_pr_create_calls[0][body_idx])
        body_text = body_path.read_text(encoding="utf-8")
        assert "\u26a0\ufe0f Unmet acceptance criteria" in body_text
        assert "- AC1 \u2014 real fixture must be committed" in body_text
        assert "- AC2 \u2014 deploy evidence posted" in body_text

        # --- AC5: issue comment posted linking the draft PR + listing unmet ---
        assert len(gh_issue_comment_calls) == 1
        assert "--body-file" in gh_issue_comment_calls[0]
        comment_body_idx = gh_issue_comment_calls[0].index("--body-file") + 1
        comment_body_path = Path(gh_issue_comment_calls[0][comment_body_idx])
        comment_body = comment_body_path.read_text(encoding="utf-8")
        assert "<!-- dispatcher-needs-review -->" in comment_body
        assert "https://github.com/judgemind/judgemind/pull/9001" in comment_body
        assert "- AC1 \u2014 real fixture must be committed" in comment_body

        # --- AC4: terminal UPDATE sets status='needs_review' (not 'failed'). ---
        needs_review_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "needs_review" in e[1]
        ]
        assert needs_review_updates, (
            "expected UPDATE with status='needs_review'; got: "
            f"{[e[1] for e in conn.cursor_instance.executed if 'UPDATE dispatcher.agents' in e[0]]}"
        )

        # summary_unmet_criteria event logged.
        unmet_events = handler.events("summary_unmet_criteria")
        assert unmet_events != []
        assert unmet_events[0].terminal_status == "needs_review"

        # pr_opened event carries is_draft=True so CloudWatch can filter.
        pr_opened = handler.events("pr_opened")
        assert pr_opened
        assert pr_opened[0].pr_number == 9001

        # Side-effect: comment posted event logged.
        assert handler.events("needs_review_comment_posted") != []

        # Ensure the happy-path ``running+awaiting_ci`` transition did
        # NOT fire — the supervisor tick must not pick this up for
        # auto-merge.
        running_awaiting_ci_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "awaiting_ci" in e[1]
            and "running" in e[1]
        ]
        assert running_awaiting_ci_updates == []

    def test_summary_unmet_comment_failure_does_not_block_db_update(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Issue-comment failure must NOT prevent status='needs_review' DB write.

        DB update is the authoritative terminal-status write. A
        GitHub outage on the issue-comment path must leave the agent
        as ``needs_review`` (not stuck in ``push_and_pr``) so the
        supervisor + admin cockpit see it consistently.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None, (None, [42]), None]

        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        summary_unmet = dict(_fixture_summary_output())
        summary_unmet["unmet_criteria"] = ["AC1 missing"]

        phase_outputs = {
            "plan": _fixture_plan_output(),
            "ralph": _fixture_ralph_output(),
            "summary": summary_unmet,
        }

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if cmd[:3] == ["gh", "pr", "create"]:
                r.stdout = "https://github.com/judgemind/judgemind/pull/9001\n"
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                # Comment post fails — DB update must still run.
                r.returncode = 1
                r.stderr = "api error"
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            output_dir = worktree / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{phase}.json").write_text(json.dumps(phase_outputs[phase]))
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # DB terminal-status update ran.
        needs_review_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "needs_review" in e[1]
        ]
        assert needs_review_updates
        # Failure event logged.
        assert handler.events("needs_review_comment_failed") != []


# --------------------------------------------------------------------------
# Claim interlock: ``status/in-progress`` label lifecycle + daemon
# queue-scan filter. Post-#2927 the /task skill uses label-only
# coordination (no DB row), but the daemon still writes the label
# on claim so the UI + queue-scan filter observe in-progress state.
# --------------------------------------------------------------------------


class TestClaimInterlockLabel:
    """Status label add on claim / remove on terminal (#2866)."""

    def test_mark_agent_terminal_removes_label_when_issue_number_provided(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Terminal transition with ``issue_number`` → ``gh issue edit --remove-label``.

        The explicit-opt-in contract: callers thread issue_number
        through when they know it; otherwise the teardown is skipped.
        This test covers the "thread through" path.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        gh_edit_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "edit"]:
                gh_edit_calls.append(cmd)
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        d._mark_agent_terminal(
            "agent-uuid",
            status="succeeded",
            phase="done",
            exit_code=0,
            issue_number=42,
        )
        assert len(gh_edit_calls) == 1
        assert "--remove-label" in gh_edit_calls[0]
        assert daemon.STATUS_IN_PROGRESS_LABEL in gh_edit_calls[0]

    def test_mark_agent_terminal_skips_label_when_issue_number_omitted(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Terminal transition without ``issue_number`` → no gh call.

        Protects call sites that don't know the issue number
        (supervisor retry paths, generic diagnoser hand-offs) from
        making an expensive subprocess call that would pin the event
        loop on a slow GitHub response.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        gh_edit_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            if cmd[:3] == ["gh", "issue", "edit"]:
                gh_edit_calls.append(cmd)
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        d._mark_agent_terminal(
            "agent-uuid",
            status="failed",
            phase="awaiting_ci",
            exit_code=None,
            # issue_number omitted
        )
        assert gh_edit_calls == []

    def test_mark_agent_terminal_non_terminal_status_does_not_remove_label(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Non-terminal transitions (e.g. awaiting_ci hand-off) keep the label.

        The label only clears when the agent is genuinely done; the
        Phase 3A post-PR hand-off is still "running" with
        ``phase='awaiting_ci'`` so the label must persist until the
        final success/failed/crashed transition.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        gh_edit_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            if cmd[:3] == ["gh", "issue", "edit"]:
                gh_edit_calls.append(cmd)
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        d._mark_agent_terminal(
            "agent-uuid",
            status="running",
            phase="awaiting_ci",
            exit_code=None,
            issue_number=42,
        )
        assert gh_edit_calls == []


class TestQueueScanExcludesInProgressLabel:
    """``_fetch_agent_ready_issues`` filters out ``status/in-progress`` (#2866)."""

    def test_in_progress_label_excludes_issue_from_candidate_list(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Issues carrying ``status/in-progress`` alongside ``agent/ready`` drop.

        Belt-and-suspenders with the DB-side partial UNIQUE INDEX: the
        label filter is redundant if the DB write is atomic, but a
        ``gh`` output that races a /task skill's label add protects
        against the daemon picking up an issue in the few-second
        window before the DB row lands.
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = json.dumps(
                [
                    {
                        "number": 1,
                        "labels": [{"name": "agent/ready"}],
                        "title": "ok",
                    },
                    {
                        "number": 2,
                        "labels": [
                            {"name": "agent/ready"},
                            {"name": daemon.STATUS_IN_PROGRESS_LABEL},
                        ],
                        "title": "in-progress",
                    },
                    {
                        "number": 3,
                        "labels": [{"name": "agent/ready"}],
                        "title": "ok2",
                    },
                ]
            )
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        issues = d._fetch_agent_ready_issues()
        # #2 drops; #1 + #3 remain.
        assert [i["number"] for i in issues] == [1, 3]


class TestGhIssueRemoveLabelsHelper:
    """Thin helper tests for :meth:`_gh_issue_remove_labels` (#2866)."""

    def test_empty_labels_is_a_noop(self, monkeypatch: Any, tmp_path: Path) -> None:
        """No labels → no subprocess call."""
        d, _conn, _handler = _make_daemon(tmp_path)
        called: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_kw: called.append(cmd) or MagicMock(returncode=0),
        )
        d._gh_issue_remove_labels(42, [])
        assert called == []

    def test_happy_path_shells_out_to_gh_issue_edit_remove_label(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """One label → one ``gh issue edit --remove-label LABEL`` subprocess call."""
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            captured.append(cmd)
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._gh_issue_remove_labels(42, [daemon.STATUS_IN_PROGRESS_LABEL])
        assert len(captured) == 1
        assert captured[0][:3] == ["gh", "issue", "edit"]
        assert "--remove-label" in captured[0]
        assert daemon.STATUS_IN_PROGRESS_LABEL in captured[0]

    def test_nonzero_exit_is_logged_not_raised(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``gh`` returning non-zero must not raise — DB write is authoritative."""
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "label not found\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Must not raise.
        d._gh_issue_remove_labels(42, [daemon.STATUS_IN_PROGRESS_LABEL])
        assert handler.events("label_remove_nonzero") != []
