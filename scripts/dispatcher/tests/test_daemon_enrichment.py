"""Unit tests for daemon-side issue enrichment (issue #2820).

Covers:

* ``_normalize_issue_enrichment`` — module helper that projects ``gh
  issue list`` output into the schema stored in
  ``dispatcher.queue_snapshots.issues_json`` and
  ``dispatcher.blocked_snapshots.issues_json``.
* ``_fetch_blocked_issues`` + ``_scan_blocked_and_snapshot`` — the new
  blocked-list scan path.
* ``_latest_queue_snapshot_title_for`` — title lookup that populates
  ``dispatcher.agents.issue_title`` at claim time.
* ``_atomic_claim`` — inserts ``issue_title`` into
  ``dispatcher.agents``.
* ``_should_run_blocked_scan`` — cadence gate (first tick + every N
  ticks).

All external calls (subprocess, psycopg) are mocked. Mirrors the fakes
used by ``test_daemon_phase2.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
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
# Shared fakes (same shape as test_daemon_phase2.py).
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
    logger = logging.getLogger("dispatcher.test.enrichment")
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
# _normalize_issue_enrichment — module-scope helper
# --------------------------------------------------------------------------


class TestNormalizeIssueEnrichment:
    """Projection from ``gh issue list`` shape → persisted JSON shape."""

    def test_happy_path_queue_shape(self) -> None:
        raw = {
            "number": 42,
            "title": "Do the thing",
            "labels": [
                {"name": "priority/p1", "color": "red"},
                {"name": "agent/ready", "color": "green"},
            ],
            "createdAt": "2026-04-18T10:00:00Z",
            "body": "ignored when include_body=False",
        }
        result = daemon._normalize_issue_enrichment(raw)
        assert result == {
            "number": 42,
            "title": "Do the thing",
            "labels": ["priority/p1", "agent/ready"],
            "createdAt": "2026-04-18T10:00:00Z",
        }
        # ``body`` is NOT included when include_body=False — critical
        # because the queue snapshot omits body to keep row size small.
        assert "body" not in result

    def test_includes_body_when_requested(self) -> None:
        raw = {
            "number": 100,
            "title": "Blocked thing",
            "labels": [{"name": "status/blocked"}],
            "createdAt": "2026-04-18T11:00:00Z",
            "body": "Some context.\nBlocked by #42\n",
        }
        result = daemon._normalize_issue_enrichment(raw, include_body=True)
        assert result["body"] == "Some context.\nBlocked by #42\n"

    def test_preserves_null_body_when_requested(self) -> None:
        raw = {
            "number": 100,
            "title": "t",
            "labels": [],
            "createdAt": "",
            "body": None,
        }
        result = daemon._normalize_issue_enrichment(raw, include_body=True)
        assert result["body"] is None

    def test_accepts_string_labels_pass_through(self) -> None:
        raw = {
            "number": 1,
            "title": "t",
            "labels": ["area/api", {"name": "priority/p2"}, 42, None],
            "createdAt": "",
        }
        result = daemon._normalize_issue_enrichment(raw)
        assert result["labels"] == ["area/api", "priority/p2"]

    def test_missing_fields_become_empty_strings(self) -> None:
        result = daemon._normalize_issue_enrichment({"number": 7})
        assert result == {
            "number": 7,
            "title": "",
            "labels": [],
            "createdAt": "",
        }


# --------------------------------------------------------------------------
# _fetch_blocked_issues — subprocess wrapper
# --------------------------------------------------------------------------


class TestFetchBlockedIssues:
    """``gh issue list --label status/blocked --json number,title,labels,createdAt,body``."""

    def test_happy_path_returns_issues_with_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(
            cmd: list[str],
            capture_output: bool = True,
            text: bool = True,
            timeout: int = 10,
            check: bool = False,
        ) -> Any:
            assert cmd[0] == "gh"
            assert "--label" in cmd and "status/blocked" in cmd
            assert "--state" in cmd and "open" in cmd
            # body must be requested — the API parses Blocked by #N
            # without hitting GitHub.
            json_arg = cmd[cmd.index("--json") + 1]
            assert "body" in json_arg
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                [
                    {
                        "number": 500,
                        "title": "Blocked A",
                        "labels": [{"name": "status/blocked"}],
                        "createdAt": "2026-04-18T12:00:00Z",
                        "body": "Blocked by #42\n",
                    },
                ]
            )
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        issues = d._fetch_blocked_issues()
        assert [i["number"] for i in issues] == [500]
        assert issues[0]["body"] == "Blocked by #42\n"

    def test_non_zero_exit_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(*_a: Any, **_kw: Any) -> Any:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "HTTP 401\n"
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as excinfo:
            d._fetch_blocked_issues()
        assert "exit=1" in str(excinfo.value)


# --------------------------------------------------------------------------
# _scan_blocked_and_snapshot — DB INSERT + structured log
# --------------------------------------------------------------------------


class TestScanBlockedAndSnapshot:
    """INSERT into dispatcher.blocked_snapshots; survive failures."""

    def test_writes_snapshot_row_and_logs(self) -> None:
        d, conn, handler = _make_daemon_with_capture()

        d._fetch_blocked_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 500,
                "title": "Blocked A",
                "labels": [{"name": "status/blocked"}],
                "createdAt": "2026-04-18T12:00:00Z",
                "body": "Blocked by #42\n",
            },
            {
                "number": 501,
                "title": "Blocked B",
                "labels": [{"name": "status/blocked"}, {"name": "area/api"}],
                "createdAt": "2026-04-18T13:00:00Z",
                "body": None,
            },
        ]
        # Issue #2989: mock title lookup so the test is deterministic.
        d._fetch_issue_titles_for_blockers = lambda numbers: {  # type: ignore[method-assign]
            n: f"Title for #{n}" for n in numbers
        }

        depth = d._scan_blocked_and_snapshot()

        assert depth == 2
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.blocked_snapshots" in e[0]
        ]
        assert len(inserts) == 1
        sql, params = inserts[0]
        assert "issues_json" in sql
        assert params[0] == 2
        assert params[1] == [500, 501]
        enriched = json.loads(params[2])
        assert len(enriched) == 2
        assert enriched[0] == {
            "number": 500,
            "title": "Blocked A",
            "labels": ["status/blocked"],
            "createdAt": "2026-04-18T12:00:00Z",
            "body": "Blocked by #42\n",
            "blockedBy": [{"number": 42, "title": "Title for #42"}],
        }
        assert enriched[1]["body"] is None
        assert enriched[1]["blockedBy"] == []  # no blocked-by lines in body=None
        assert params[3] == "test-run-id"
        assert conn.commits == 1

        scans = handler.events("blocked_scan")
        assert len(scans) == 1
        assert scans[0].count == 2

    def test_fetch_failure_returns_sentinel_no_insert(self) -> None:
        d, conn, handler = _make_daemon_with_capture()

        def boom() -> Any:
            raise RuntimeError("gh exit=1: HTTP 401")

        d._fetch_blocked_issues = boom  # type: ignore[method-assign]

        depth = d._scan_blocked_and_snapshot()
        assert depth == -1

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.blocked_snapshots" in e[0]
        ]
        assert inserts == []
        fails = handler.events("blocked_scan_failed")
        assert len(fails) == 1

    def test_db_failure_returns_sentinel_rolls_back(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        d._fetch_blocked_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 1,
                "title": "t",
                "labels": [],
                "createdAt": "",
                "body": None,
            },
        ]
        # body=None → empty blocker set → no gh calls needed; still mock for safety.
        d._fetch_issue_titles_for_blockers = lambda numbers: {}  # type: ignore[method-assign]

        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.blocked_snapshots" in sql:
                raise RuntimeError("connection lost")
            original_execute(sql, params)

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        depth = d._scan_blocked_and_snapshot()
        assert depth == -1
        assert conn.rollbacks >= 1


# --------------------------------------------------------------------------
# _should_run_blocked_scan — cadence gate
# --------------------------------------------------------------------------


class TestShouldRunBlockedScan:
    """Blocked scan fires on tick 1 and then every N ticks."""

    def test_fires_on_first_tick(self) -> None:
        d, _conn, _handler = _make_daemon_with_capture()
        d._scheduler_ticks = 0
        assert d._should_run_blocked_scan() is True

    def test_does_not_fire_on_subsequent_ticks_before_cadence(self) -> None:
        d, _conn, _handler = _make_daemon_with_capture()
        # The counter reads the number of _completed_ ticks on entry.
        # With BLOCKED_SCAN_EVERY_N_TICKS = 4 the cadence fires on
        # ticks 1, 4, 8, 12… (counter 0, 3, 7, 11 on entry).
        d._scheduler_ticks = 1  # tick 2
        assert d._should_run_blocked_scan() is False
        d._scheduler_ticks = 2  # tick 3
        assert d._should_run_blocked_scan() is False

    def test_fires_on_cadence_tick(self) -> None:
        d, _conn, _handler = _make_daemon_with_capture()
        d._scheduler_ticks = 3  # tick 4 — cadence hit
        assert d._should_run_blocked_scan() is True
        d._scheduler_ticks = 7  # tick 8 — cadence hit
        assert d._should_run_blocked_scan() is True


# --------------------------------------------------------------------------
# _latest_queue_snapshot_title_for — title lookup from issues_json
# --------------------------------------------------------------------------


class TestLatestQueueSnapshotTitleFor:
    """Pick out the title for a specific issue number from the snapshot."""

    def test_returns_title_when_present(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        # Simulate psycopg returning jsonb as a parsed python list of
        # dicts (the default behaviour).
        conn.cursor_instance.fetch_queue = [
            (
                [
                    {
                        "number": 42,
                        "title": "Do the thing",
                        "labels": ["agent/ready"],
                        "createdAt": "2026-04-18T00:00:00Z",
                    },
                    {
                        "number": 99,
                        "title": "Other thing",
                        "labels": ["agent/ready"],
                        "createdAt": "2026-04-18T00:01:00Z",
                    },
                ],
            )
        ]
        title = d._latest_queue_snapshot_title_for(42)
        assert title == "Do the thing"

    def test_returns_none_when_issue_absent(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [
            (
                [
                    {
                        "number": 99,
                        "title": "Other thing",
                        "labels": [],
                        "createdAt": "",
                    }
                ],
            )
        ]
        assert d._latest_queue_snapshot_title_for(42) is None

    def test_returns_none_when_no_snapshot(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = []  # fetchone → None
        assert d._latest_queue_snapshot_title_for(42) is None

    def test_accepts_json_string_fallback(self) -> None:
        """Defensive: some drivers return jsonb as a JSON string."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [
            (
                json.dumps(
                    [
                        {
                            "number": 42,
                            "title": "From string",
                            "labels": [],
                            "createdAt": "",
                        }
                    ]
                ),
            )
        ]
        assert d._latest_queue_snapshot_title_for(42) == "From string"

    def test_empty_title_returns_none(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [
            (
                [
                    {
                        "number": 42,
                        "title": "",
                        "labels": [],
                        "createdAt": "",
                    }
                ],
            )
        ]
        assert d._latest_queue_snapshot_title_for(42) is None


# --------------------------------------------------------------------------
# _atomic_claim — INSERTs dispatcher.agents.issue_title
# --------------------------------------------------------------------------


class TestAtomicClaimWithTitle:
    """``_atomic_claim`` accepts and persists ``issue_title``."""

    def test_insert_includes_issue_title(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        ok = d._atomic_claim(
            issue_number=42,
            agent_id="uuid-1",
            worktree_path="/tmp/worktree",
            issue_title="Do the thing",
        )
        assert ok is True
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        assert len(inserts) == 1
        sql, params = inserts[0]
        assert "issue_title" in sql
        # Params: (agent_id, run_id, issue_number, issue_title, worktree_path).
        assert params[3] == "Do the thing"

    def test_insert_tolerates_null_title(self) -> None:
        """Title missing from the snapshot → insert NULL."""
        d, conn, _handler = _make_daemon_with_capture()
        ok = d._atomic_claim(
            issue_number=42,
            agent_id="uuid-1",
            worktree_path="/tmp/worktree",
            issue_title=None,
        )
        assert ok is True
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        assert len(inserts) == 1
        _sql, params = inserts[0]
        assert params[3] is None


# --------------------------------------------------------------------------
# scheduler_tick wiring — blocked scan runs on first tick + cadence
# --------------------------------------------------------------------------


class TestSchedulerTickBlockedWiring:
    """First-tick + cadence behaviour for the blocked-list scan."""

    def test_first_tick_runs_blocked_scan(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [(0,)]  # concurrency_cap
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._fetch_blocked_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 500,
                "title": "Blocked",
                "labels": [{"name": "status/blocked"}],
                "createdAt": "",
                "body": None,
            },
        ]
        # body=None → empty blocker set → no gh calls; mock for determinism (#2989).
        d._fetch_issue_titles_for_blockers = lambda numbers: {}  # type: ignore[method-assign]

        summary = d.scheduler_tick()
        assert summary["blocked_depth"] == 1

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.blocked_snapshots" in e[0]
        ]
        assert len(inserts) == 1
        scans = handler.events("blocked_scan")
        assert len(scans) == 1

    def test_second_tick_skips_blocked_scan(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        # After one completed tick, the counter is 1. The next tick
        # (tick 2) should skip the blocked scan (cadence is every 4).
        d._scheduler_ticks = 1
        conn.cursor_instance.fetch_queue = [(0,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        fetched = []
        d._fetch_blocked_issues = lambda: (fetched.append(1), [])[  # type: ignore[method-assign]
            1
        ]

        summary = d.scheduler_tick()
        assert summary["blocked_depth"] == -1  # scan not attempted
        assert fetched == []
        # Sentinel is emitted in the scheduler_tick log.
        ticks = handler.events("scheduler_tick")
        assert ticks[0].blocked_depth == -1


# --------------------------------------------------------------------------
# _fetch_issue_titles_for_blockers — new helper (issue #2989)
# --------------------------------------------------------------------------


class TestFetchIssueTitlesForBlockers:
    """New helper that batch-fetches blocker titles from ``gh issue view``."""

    def test_returns_title_for_each_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
            number_str = cmd[cmd.index("view") + 1]
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {"number": int(number_str), "title": f"Title for #{number_str}"}
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers({42, 100})

        assert titles[42] == "Title for #42"
        assert titles[100] == "Title for #100"
        # One gh call per unique number.
        assert len(call_log) == 2

    def test_returns_none_on_subprocess_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, handler = _make_daemon_with_capture()

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            return {
                "ok": False,
                "reason": "nonzero_exit",
                "stderr_tail": "404",
                "exit_code": 1,
                "attempts": 3,
            }

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers({99})
        assert titles[99] is None
        # Warning logged at WARNING level.
        warn_events = [
            r
            for r in handler.records
            if r.levelno == logging.WARNING
            and getattr(r, "event", None) == "blocker_title_fetch_exhausted"
        ]
        assert len(warn_events) == 1

    def test_empty_set_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()
        calls: list = []

        def fake_retry(*_a: object, **_kw: object) -> dict:  # pragma: no cover
            calls.append(1)
            return {"ok": True, "result": MagicMock(), "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        titles = d._fetch_issue_titles_for_blockers(set())
        assert titles == {}
        assert calls == []

    def test_deduplicates_blocker_numbers_within_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One gh issue view per distinct blocker number across the whole tick."""
        d, _conn, _handler = _make_daemon_with_capture()

        call_log: list[str] = []

        def fake_retry(
            cmd: list[str],
            *,
            event_name: str,
            timeout: float,
            extra_log_fields: dict | None = None,
        ) -> dict:
            number_str = cmd[cmd.index("view") + 1]
            call_log.append(number_str)
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {"number": int(number_str), "title": f"T#{number_str}"}
            )
            return {"ok": True, "result": result, "attempts": 1}

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_retry)

        # Pass a set — duplicates are already deduplicated by set semantics.
        # This verifies we only make one call even when the same number
        # appears in multiple blocked issues' bodies.
        titles = d._fetch_issue_titles_for_blockers({42, 42, 100})
        # Set deduplicated to {42, 100} — only 2 calls.
        assert len(call_log) == 2
        assert sorted(call_log) == ["100", "42"]
        assert titles[42] == "T#42"


# --------------------------------------------------------------------------
# _normalize_issue_enrichment with blocker_title_lookup (issue #2989)
# --------------------------------------------------------------------------


class TestNormalizeIssueEnrichmentWithBlockers:
    """blockedBy field is populated from the lookup dict when include_body=True."""

    def test_populate_blocker_refs_from_lookup(self) -> None:
        raw = {
            "number": 500,
            "title": "Blocked A",
            "labels": [{"name": "status/blocked"}],
            "createdAt": "2026-04-18T12:00:00Z",
            "body": "Some context.\nBlocked by #42\nBlocked by #100\n",
        }
        lookup = {42: "The forty-two issue", 100: None}
        result = daemon._normalize_issue_enrichment(
            raw, include_body=True, blocker_title_lookup=lookup
        )
        assert result["blockedBy"] == [
            {"number": 42, "title": "The forty-two issue"},
            {"number": 100, "title": None},
        ]

    def test_no_lookup_omits_blockedby(self) -> None:
        """Without a lookup the field is absent (backward-compat path)."""
        raw = {
            "number": 500,
            "title": "Blocked A",
            "labels": [],
            "createdAt": "",
            "body": "Blocked by #42\n",
        }
        result = daemon._normalize_issue_enrichment(raw, include_body=True)
        assert "blockedBy" not in result

    def test_blockedby_absent_when_body_has_no_blocked_lines(self) -> None:
        raw = {
            "number": 501,
            "title": "Blocked B",
            "labels": [],
            "createdAt": "",
            "body": "No blocked lines here.",
        }
        lookup: dict[int, str | None] = {}
        result = daemon._normalize_issue_enrichment(
            raw, include_body=True, blocker_title_lookup=lookup
        )
        assert result["blockedBy"] == []

    def test_blockedby_not_present_when_include_body_false(self) -> None:
        raw = {
            "number": 501,
            "title": "Ready issue",
            "labels": [],
            "createdAt": "",
        }
        lookup = {42: "Title"}
        result = daemon._normalize_issue_enrichment(
            raw, include_body=False, blocker_title_lookup=lookup
        )
        # include_body=False → no blockedBy even if a lookup was passed.
        assert "blockedBy" not in result


# --------------------------------------------------------------------------
# _scan_blocked_and_snapshot with blocker-title enrichment (issue #2989)
# --------------------------------------------------------------------------


class TestScanBlockedAndSnapshotWithTitles:
    """Verifies dedup and None-fallthrough in the full scan path."""

    def test_blocker_title_fetch_dedupes_within_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1: one gh issue view per distinct blocker number, regardless of
        how many blocked issues share that blocker."""
        d, conn, _handler = _make_daemon_with_capture()

        d._fetch_blocked_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 500,
                "title": "Blocked A",
                "labels": [{"name": "status/blocked"}],
                "createdAt": "2026-04-18T12:00:00Z",
                "body": "Blocked by #42\n",
            },
            {
                "number": 501,
                "title": "Blocked B",
                "labels": [{"name": "status/blocked"}],
                "createdAt": "2026-04-18T13:00:00Z",
                "body": "Blocked by #42\nBlocked by #100\n",
            },
        ]

        fetch_calls: list[int] = []

        def fake_fetch_titles(numbers: set[int]) -> dict[int, str | None]:
            fetch_calls.extend(sorted(numbers))
            return {n: f"Title for #{n}" for n in numbers}

        monkeypatch.setattr(d, "_fetch_issue_titles_for_blockers", fake_fetch_titles)

        depth = d._scan_blocked_and_snapshot()
        assert depth == 2

        # The deduped set {42, 100} was passed to the fetcher exactly once.
        assert sorted(fetch_calls) == [42, 100]

        # Check persisted JSON includes blockedBy.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.blocked_snapshots" in e[0]
        ]
        assert len(inserts) == 1
        enriched = json.loads(inserts[0][1][2])
        # Issue 500: only blocked by #42.
        assert enriched[0]["blockedBy"] == [{"number": 42, "title": "Title for #42"}]
        # Issue 501: blocked by #42 and #100.
        assert enriched[1]["blockedBy"] == [
            {"number": 42, "title": "Title for #42"},
            {"number": 100, "title": "Title for #100"},
        ]

    def test_null_title_fallthrough_on_subprocess_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: when the title fetch fails for a blocker, title=None, no crash."""
        d, conn, _handler = _make_daemon_with_capture()

        d._fetch_blocked_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 500,
                "title": "Blocked A",
                "labels": [],
                "createdAt": "",
                "body": "Blocked by #42\n",
            },
        ]

        def fake_fetch_titles(numbers: set[int]) -> dict[int, str | None]:
            # Simulate failure → title is None.
            return {n: None for n in numbers}

        monkeypatch.setattr(d, "_fetch_issue_titles_for_blockers", fake_fetch_titles)

        depth = d._scan_blocked_and_snapshot()
        assert depth == 1

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.blocked_snapshots" in e[0]
        ]
        enriched = json.loads(inserts[0][1][2])
        assert enriched[0]["blockedBy"] == [{"number": 42, "title": None}]

    def test_blocker_title_fetch_exception_falls_back_to_empty_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception in _fetch_issue_titles_for_blockers must not propagate;
        _scan_blocked_and_snapshot should still succeed with title=None."""
        d, conn, _handler = _make_daemon_with_capture()

        d._fetch_blocked_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 500,
                "title": "Blocked A",
                "labels": [],
                "createdAt": "",
                "body": "Blocked by #42\n",
            },
        ]

        def raising_fetch(numbers: set[int]) -> dict[int, str | None]:
            raise RuntimeError("unexpected internal error")

        monkeypatch.setattr(d, "_fetch_issue_titles_for_blockers", raising_fetch)

        depth = d._scan_blocked_and_snapshot()
        assert depth == 1

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.blocked_snapshots" in e[0]
        ]
        enriched = json.loads(inserts[0][1][2])
        # title is None because the lookup fell back to {}
        assert enriched[0]["blockedBy"] == [{"number": 42, "title": None}]


class TestFetchIssuesTitleCap:
    """MAX_BLOCKER_TITLE_FETCH cap bounds the number of gh calls."""

    def test_cap_limits_gh_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When >MAX_BLOCKER_TITLE_FETCH unique blockers are passed, only the
        first MAX_BLOCKER_TITLE_FETCH are fetched; the rest are absent."""
        from scripts.dispatcher.daemon import MAX_BLOCKER_TITLE_FETCH

        d, _conn, _handler = _make_daemon_with_capture()

        call_count = 0

        def fake_subprocess_with_retry(
            cmd: list[str],
            event_name: str,
            timeout: int = 30,
            extra_log_fields: dict | None = None,
        ) -> dict:
            nonlocal call_count
            call_count += 1
            # cmd = ["gh", "issue", "view", "<N>", "--repo", ...]
            number = int(cmd[3])
            return {
                "ok": True,
                "result": type(
                    "CP",
                    (),
                    {"stdout": f'{{"number": {number}, "title": "T{number}"}}'},
                )(),
            }

        # Build a set with MAX_BLOCKER_TITLE_FETCH + 5 unique numbers.
        big_set: set[int] = set(range(1, MAX_BLOCKER_TITLE_FETCH + 6))

        monkeypatch.setattr(d, "_subprocess_with_retry", fake_subprocess_with_retry)
        result = d._fetch_issue_titles_for_blockers(big_set)

        # Only MAX_BLOCKER_TITLE_FETCH calls should have been made.
        assert call_count == MAX_BLOCKER_TITLE_FETCH
        # All fetched numbers have a title.
        assert len(result) == MAX_BLOCKER_TITLE_FETCH
        assert all(v is not None for v in result.values())
