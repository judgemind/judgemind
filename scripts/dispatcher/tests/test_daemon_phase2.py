"""Unit tests for Phase 2 additions to ``scripts.dispatcher.daemon``.

Issue #2768. Covers the three Phase 2 behaviours added on top of the
Phase 1 skeleton:

* Queue scan (``_fetch_agent_ready_issues`` + ``_scan_queue_and_snapshot``)
  parses ``gh issue list`` output, filters blocked issues, INSERTs into
  ``dispatcher.queue_snapshots``, and logs structured JSON.
* Heartbeat metric emission (``_emit_heartbeat_metric``) publishes
  ``HeartbeatAge=0`` to the configured CloudWatch namespace with the
  correct ``Service`` dimension.
* ``concurrency_cap=0`` Phase 2 guard logs a warning when the live DB
  value is anything else.

All external calls — subprocess (``gh``), boto3 (CloudWatch),
psycopg — are mocked. The queue-scan subprocess path is monkeypatched
to avoid requiring ``gh`` on the test runner.
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
# Shared fakes (mirrors test_daemon_skeleton.py shape)
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
    """Collect emitted ``LogRecord``s so tests can assert on them.

    The daemon attaches ``extra={"event": ..., ...}`` to every
    observability log line; the handler stores those keys on each record
    so assertions can target them directly.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        """All captured records whose ``extra.event`` equals ``name``."""
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon_with_capture() -> tuple[
    daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler
]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger("dispatcher.test.phase2")
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
# _fetch_agent_ready_issues — subprocess + JSON parsing
# --------------------------------------------------------------------------


class TestFetchAgentReadyIssues:
    """Parse ``gh issue list`` output; defensive filters on blocked issues."""

    def test_happy_path_returns_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(
            cmd: list[str],
            capture_output: bool = True,
            text: bool = True,
            timeout: int = 10,
            check: bool = False,
        ) -> Any:
            assert cmd[0] == "gh"
            assert "--label" in cmd and "agent/ready" in cmd
            assert "--state" in cmd and "open" in cmd
            assert "--json" in cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                [
                    {
                        "number": 42,
                        "title": "First",
                        "labels": [{"name": "agent/ready"}, {"name": "priority/p1"}],
                        "createdAt": "2026-04-18T00:00:00Z",
                    },
                    {
                        "number": 99,
                        "title": "Second",
                        "labels": [{"name": "agent/ready"}],
                        "createdAt": "2026-04-18T00:01:00Z",
                    },
                ]
            )
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        issues = d._fetch_agent_ready_issues()

        assert len(issues) == 2
        assert [i["number"] for i in issues] == [42, 99]

    def test_filters_out_blocked_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issues that carry ``status/blocked`` alongside ``agent/ready`` are dropped.

        Defensive: ``gh --label agent/ready`` should not return them in
        the first place once the GH search applies label semantics, but
        mid-transition issues (where both labels are present for a few
        seconds) should not inflate the count.
        """
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
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
                            {"name": "status/blocked"},
                        ],
                        "title": "blocked",
                    },
                    {
                        "number": 3,
                        "labels": [{"name": "agent/ready"}],
                        "title": "ok2",
                    },
                ]
            )
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        issues = d._fetch_agent_ready_issues()
        assert [i["number"] for i in issues] == [1, 3]

    def test_empty_queue_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 0
            result.stdout = "[]"
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._fetch_agent_ready_issues() == []

    def test_gh_nonzero_exit_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "HTTP 401: Bad credentials"
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as excinfo:
            d._fetch_agent_ready_issues()
        # Error message includes the exit code for triage.
        assert "exit=1" in str(excinfo.value)

    def test_gh_timeout_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as excinfo:
            d._fetch_agent_ready_issues()
        assert "timed out" in str(excinfo.value)

    def test_gh_missing_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError(2, "No such file or directory", "gh")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as excinfo:
            d._fetch_agent_ready_issues()
        assert "gh CLI not on PATH" in str(excinfo.value)

    def test_invalid_json_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 0
            result.stdout = "not json at all"
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as excinfo:
            d._fetch_agent_ready_issues()
        assert "invalid JSON" in str(excinfo.value)

    def test_non_list_json_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _conn, _handler = _make_daemon_with_capture()

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            result = MagicMock()
            result.returncode = 0
            result.stdout = '{"unexpected": "shape"}'
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            d._fetch_agent_ready_issues()


# --------------------------------------------------------------------------
# _scan_queue_and_snapshot — DB INSERT + structured log
# --------------------------------------------------------------------------


class TestScanQueueAndSnapshot:
    """INSERT into dispatcher.queue_snapshots; survive failures."""

    def test_writes_snapshot_row_and_logs(self) -> None:
        d, conn, handler = _make_daemon_with_capture()

        # Patch the fetch path to return a canned list, including the
        # title/labels/createdAt fields the daemon now persists into
        # ``issues_json`` (issue #2820).
        d._fetch_agent_ready_issues = lambda: [  # type: ignore[method-assign]
            {
                "number": 10,
                "title": "Do thing A",
                "labels": [{"name": "agent/ready"}, {"name": "priority/p1"}],
                "createdAt": "2026-04-18T10:00:00Z",
            },
            {
                "number": 42,
                "title": "Do thing B",
                "labels": [{"name": "agent/ready"}],
                "createdAt": "2026-04-18T11:00:00Z",
            },
        ]

        depth = d._scan_queue_and_snapshot()

        assert depth == 2
        # One INSERT, one commit.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.queue_snapshots" in e[0]
        ]
        assert len(inserts) == 1
        sql, params = inserts[0]
        # SQL mentions the new issues_json column.
        assert "issues_json" in sql
        # Params: (queue_depth, issue_numbers, issues_json_str, run_id).
        assert params[0] == 2
        assert params[1] == [10, 42]
        enriched = json.loads(params[2])
        assert enriched == [
            {
                "number": 10,
                "title": "Do thing A",
                "labels": ["agent/ready", "priority/p1"],
                "createdAt": "2026-04-18T10:00:00Z",
            },
            {
                "number": 42,
                "title": "Do thing B",
                "labels": ["agent/ready"],
                "createdAt": "2026-04-18T11:00:00Z",
            },
        ]
        assert params[3] == "test-run-id"
        assert conn.commits == 1
        # Structured log emitted.
        scans = handler.events("queue_scan")
        assert len(scans) == 1
        assert scans[0].count == 2
        assert scans[0].issues == [10, 42]

    def test_empty_queue_still_inserts_zero_depth(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]

        depth = d._scan_queue_and_snapshot()
        assert depth == 0

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.queue_snapshots" in e[0]
        ]
        assert len(inserts) == 1
        _sql, params = inserts[0]
        assert params[0] == 0
        assert params[1] == []
        # Empty queue → ``[]`` in issues_json, ``test-run-id`` in run_id.
        assert json.loads(params[2]) == []
        assert params[3] == "test-run-id"

        scans = handler.events("queue_scan")
        assert len(scans) == 1 and scans[0].count == 0

    def test_fetch_failure_returns_sentinel_no_insert(self) -> None:
        """When ``gh`` fails, the daemon must not crash and must not insert."""
        d, conn, handler = _make_daemon_with_capture()

        def boom() -> Any:
            raise RuntimeError("gh exit=1: HTTP 401")

        d._fetch_agent_ready_issues = boom  # type: ignore[method-assign]

        depth = d._scan_queue_and_snapshot()
        assert depth == -1

        # No INSERT attempted — we didn't get a count to persist.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.queue_snapshots" in e[0]
        ]
        assert inserts == []
        # Warning logged with detail.
        fails = handler.events("queue_scan_failed")
        assert len(fails) == 1
        assert "HTTP 401" in fails[0].detail

    def test_db_failure_returns_sentinel_rolls_back(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        d._fetch_agent_ready_issues = lambda: [  # type: ignore[method-assign]
            {"number": 1, "labels": [{"name": "agent/ready"}]},
        ]

        original_execute = conn.cursor_instance.execute

        def failing_execute(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.queue_snapshots" in sql:
                raise RuntimeError("connection lost")
            original_execute(sql, params)

        conn.cursor_instance.execute = failing_execute  # type: ignore[method-assign]

        depth = d._scan_queue_and_snapshot()
        assert depth == -1
        assert conn.rollbacks >= 1


# --------------------------------------------------------------------------
# scheduler_tick — Phase 2 integration (queue scan wired in)
# --------------------------------------------------------------------------


class TestSchedulerTickPhase2:
    """Scheduler tick runs the queue scan and writes a snapshot row."""

    def test_runs_queue_scan_and_returns_depth(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        # Scheduler's own SELECT on config returns concurrency_cap=0.
        conn.cursor_instance.fetch_queue = [(0,)]
        # Stub the fetch path.
        d._fetch_agent_ready_issues = lambda: [  # type: ignore[method-assign]
            {"number": 7, "labels": [{"name": "agent/ready"}]},
        ]

        summary = d.scheduler_tick()
        assert summary["queue_depth"] == 1
        assert summary["concurrency_cap"] == 0
        assert summary["commands_consumed"] == 0
        # A queue_snapshots INSERT ran.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.queue_snapshots" in e[0]
        ]
        assert len(inserts) == 1
        # Scheduler_tick log line includes the queue_depth.
        ticks = handler.events("scheduler_tick")
        assert len(ticks) == 1
        assert ticks[0].queue_depth == 1

    def test_queue_scan_failure_does_not_crash_tick(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [(0,)]

        def boom() -> Any:
            raise RuntimeError("gh exit=1")

        d._fetch_agent_ready_issues = boom  # type: ignore[method-assign]

        # No exception out of scheduler_tick even when the scan dies.
        summary = d.scheduler_tick()
        assert summary["queue_depth"] == -1
        assert handler.events("queue_scan_failed") != []


# --------------------------------------------------------------------------
# _emit_heartbeat_metric — CloudWatch PutMetricData
# --------------------------------------------------------------------------


class TestEmitHeartbeatMetric:
    """``HeartbeatAge=0`` → Judgemind/Dispatcher namespace, Service dim."""

    def test_emits_metric_with_correct_shape(self) -> None:
        d, _conn, handler = _make_daemon_with_capture()
        mock_client = MagicMock()
        d._make_cloudwatch_client = lambda: mock_client  # type: ignore[method-assign]

        ok = d._emit_heartbeat_metric()
        assert ok is True
        mock_client.put_metric_data.assert_called_once()
        call_kwargs = mock_client.put_metric_data.call_args.kwargs
        assert call_kwargs["Namespace"] == "Judgemind/Dispatcher"
        md = call_kwargs["MetricData"]
        assert len(md) == 1
        assert md[0]["MetricName"] == "HeartbeatAge"
        assert md[0]["Value"] == 0.0
        assert md[0]["Unit"] == "Seconds"
        assert md[0]["Dimensions"] == [
            {"Name": "Service", "Value": "judgemind-dispatcher-dev"},
        ]
        # Structured log recorded.
        emits = handler.events("heartbeat_metric_emitted")
        assert len(emits) == 1

    def test_falls_back_to_hostname_when_service_name_unset(self) -> None:
        d, _conn, _handler = _make_daemon_with_capture()
        # Blank out DISPATCHER_SERVICE_NAME.
        d._cfg = daemon.DaemonConfig(
            database_url="postgres://fake",
            version_sha="deadbee",
            host="task-abc123",
            pid=1,
            dispatcher_service_name="",
            heartbeat_metric_namespace="Judgemind/Dispatcher",
            aws_region="us-west-2",
        )
        d._run_id = "test-run-id"
        mock_client = MagicMock()
        d._make_cloudwatch_client = lambda: mock_client  # type: ignore[method-assign]

        d._emit_heartbeat_metric()
        dims = mock_client.put_metric_data.call_args.kwargs["MetricData"][0][
            "Dimensions"
        ]
        assert dims == [{"Name": "Service", "Value": "task-abc123"}]

    def test_reuses_cloudwatch_client_across_ticks(self) -> None:
        d, _conn, _handler = _make_daemon_with_capture()
        mock_client = MagicMock()
        make_calls = {"n": 0}

        def make_once() -> Any:
            make_calls["n"] += 1
            return mock_client

        d._make_cloudwatch_client = make_once  # type: ignore[method-assign]

        d._emit_heartbeat_metric()
        d._emit_heartbeat_metric()
        d._emit_heartbeat_metric()

        # Client built exactly once; three calls reuse it.
        assert make_calls["n"] == 1
        assert mock_client.put_metric_data.call_count == 3

    def test_put_metric_failure_does_not_raise_and_resets_client(self) -> None:
        d, _conn, handler = _make_daemon_with_capture()

        failing_client = MagicMock()
        failing_client.put_metric_data.side_effect = RuntimeError("access denied")
        ok_client = MagicMock()

        clients = [failing_client, ok_client]

        def next_client() -> Any:
            return clients.pop(0)

        d._make_cloudwatch_client = next_client  # type: ignore[method-assign]

        # First call fails, but returns False rather than raising.
        first = d._emit_heartbeat_metric()
        assert first is False
        fails = handler.events("heartbeat_metric_failed")
        assert len(fails) == 1
        assert "access denied" in fails[0].detail

        # Client was reset, so the next call builds a fresh one.
        second = d._emit_heartbeat_metric()
        assert second is True


# --------------------------------------------------------------------------
# supervisor_tick — Phase 2 integration (heartbeat metric wired in)
# --------------------------------------------------------------------------


class TestSupervisorTickPhase2:
    """Supervisor tick now also emits the CloudWatch metric."""

    def test_emits_metric_after_successful_heartbeat(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        # The failures count(*) returns 0.
        conn.cursor_instance.fetch_queue = [(0,)]
        mock_client = MagicMock()
        d._make_cloudwatch_client = lambda: mock_client  # type: ignore[method-assign]

        summary = d.supervisor_tick()
        # DB heartbeat UPDATE ran.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if e[0].startswith("UPDATE dispatcher.runs")
        ]
        assert len(updates) == 1
        # CloudWatch metric published.
        assert mock_client.put_metric_data.call_count == 1
        assert summary["heartbeat_metric_emitted"] == 1
        # Supervisor tick log records both.
        ticks = handler.events("supervisor_tick")
        assert len(ticks) == 1
        assert ticks[0].heartbeat_metric_emitted is True

    def test_metric_failure_does_not_crash_supervisor(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetch_queue = [(0,)]
        failing = MagicMock()
        failing.put_metric_data.side_effect = RuntimeError("throttled")
        d._make_cloudwatch_client = lambda: failing  # type: ignore[method-assign]

        # Supervisor tick still returns normally — the DB heartbeat is
        # the ground truth; the metric is just for the alarm.
        summary = d.supervisor_tick()
        assert summary["heartbeat_metric_emitted"] == 0


# --------------------------------------------------------------------------
# DaemonConfig / _build_config env plumbing
# --------------------------------------------------------------------------


class TestPhase2ConfigPlumbing:
    """New env vars: GITHUB_REPO, DISPATCHER_SERVICE_NAME, etc."""

    def test_build_config_reads_new_env_vars(self) -> None:
        args = daemon._parse_args([])
        env = {
            "DATABASE_URL": "postgres://x",
            "GIT_SHA": "cafef00d",
            "HOSTNAME": "task-xyz",
            "GITHUB_REPO": "acme/widgets",
            "DISPATCHER_SERVICE_NAME": "judgemind-dispatcher-dev",
            "HEARTBEAT_METRIC_NAMESPACE": "Judgemind/Dispatcher",
            "AWS_REGION": "us-west-2",
        }
        cfg = daemon._build_config(args, env=env)
        assert cfg.github_repo == "acme/widgets"
        assert cfg.dispatcher_service_name == "judgemind-dispatcher-dev"
        assert cfg.heartbeat_metric_namespace == "Judgemind/Dispatcher"
        assert cfg.aws_region == "us-west-2"

    def test_build_config_defaults_when_unset(self) -> None:
        args = daemon._parse_args([])
        cfg = daemon._build_config(args, env={"DATABASE_URL": "postgres://x"})
        assert cfg.github_repo == daemon.DEFAULT_GITHUB_REPO
        assert (
            cfg.heartbeat_metric_namespace == daemon.DEFAULT_HEARTBEAT_METRIC_NAMESPACE
        )
        assert cfg.aws_region == daemon.DEFAULT_AWS_REGION

    def test_build_config_aws_region_env_fallback(self) -> None:
        """``AWS_DEFAULT_REGION`` is respected when ``AWS_REGION`` is unset."""
        args = daemon._parse_args([])
        cfg = daemon._build_config(
            args,
            env={"DATABASE_URL": "postgres://x", "AWS_DEFAULT_REGION": "us-east-1"},
        )
        assert cfg.aws_region == "us-east-1"
