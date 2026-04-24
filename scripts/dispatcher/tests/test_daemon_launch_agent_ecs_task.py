"""Unit tests for issue #3091 — ``_launch_agent_ecs_task`` launches per-agent ECS tasks.

Coverage (issue #3091 Stage 2 acceptance criteria):

* Happy path — ``ecs:RunTask`` called with the task def family,
  cluster, network config, and env overrides wired from ``DaemonConfig``.
  On success the agent row is UPDATEd with ``agent_task_arn=<arn>`` and
  ``execution_mode='ecs'``.
* Retry on transient botocore ``ClientError`` (Throttling,
  ServiceUnavailable) up to 3 attempts with 1s + 2s backoff.
* NO retry on non-transient errors (AccessDenied, ValidationException).
* Response-level ``failures[]`` (200 OK but no tasks started) is
  treated as transient and retried.
* Missing ECS wiring (empty cluster / task-def / subnets / SG) fails
  closed with a structured log event and returns None.
* Tags include ``agent_id`` + ``issue_number`` so CloudWatch +
  admin-cockpit correlation works.

Fakes follow the pattern from ``test_daemon_baseline_fetch_retry.py``
— bypass ``__init__`` via ``__new__`` and stub the psycopg connection
with a ``MagicMock``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — minimal cursor + conn that records executed SQL.
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

    def fetchone(self) -> Any:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _make_daemon(
    *,
    cluster_arn: str = "arn:aws:ecs:us-west-2:123:cluster/judgemind-dev",
    task_def_family: str = "judgemind-dispatcher-agent-runner-dev",
    subnets: tuple[str, ...] = ("subnet-a", "subnet-b"),
    sg: str = "sg-abc",
) -> tuple[daemon.DispatcherDaemon, _FakeConn]:
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    conn = _FakeConn()
    d._main_conn = conn  # type: ignore[attr-defined]
    d._cfg = MagicMock(  # type: ignore[attr-defined]
        aws_region="us-west-2",
        ecs_cluster_arn=cluster_arn,
        agent_runner_task_definition_family=task_def_family,
        agent_runner_subnet_ids=subnets,
        agent_runner_security_group_id=sg,
    )
    d._log = logging.getLogger("test.daemon_launch_agent_ecs_task")  # type: ignore[attr-defined]
    d._run_id = "test-run-id"  # type: ignore[attr-defined]
    d._ecs_client = None  # type: ignore[attr-defined]
    return d, conn


def _client_error(code: str) -> Exception:
    """Build a faux botocore ClientError-shaped exception."""

    class _Fake(Exception):
        pass

    exc = _Fake(f"An error occurred ({code}) when calling the RunTask operation")
    exc.response = {"Error": {"Code": code, "Message": "test"}}
    return exc


# --------------------------------------------------------------------------
# Happy path.
# --------------------------------------------------------------------------


class TestLaunchAgentECSTaskHappyPath:
    def test_run_task_called_with_expected_arguments(self) -> None:
        d, conn = _make_daemon()
        fake_client = MagicMock()
        fake_client.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/judgemind-dev/abc"}],
            "failures": [],
        }
        with patch.object(d, "_make_ecs_client", return_value=fake_client):
            result = d._launch_agent_ecs_task("agent-1", 42)
        assert result == "arn:aws:ecs:us-west-2:123:task/judgemind-dev/abc"
        fake_client.run_task.assert_called_once()
        call_kwargs = fake_client.run_task.call_args.kwargs
        assert (
            call_kwargs["cluster"] == "arn:aws:ecs:us-west-2:123:cluster/judgemind-dev"
        )
        assert call_kwargs["taskDefinition"] == "judgemind-dispatcher-agent-runner-dev"
        assert call_kwargs["launchType"] == "FARGATE"
        assert call_kwargs["count"] == 1
        # Env overrides carry AGENT_ID + ISSUE_NUMBER.
        container_overrides = call_kwargs["overrides"]["containerOverrides"]
        assert container_overrides[0]["name"] == "agent-runner"
        env_vars = {
            e["name"]: e["value"] for e in container_overrides[0]["environment"]
        }
        assert env_vars["AGENT_ID"] == "agent-1"
        assert env_vars["ISSUE_NUMBER"] == "42"
        # Network config uses the two subnets + security group.
        netcfg = call_kwargs["networkConfiguration"]["awsvpcConfiguration"]
        assert netcfg["subnets"] == ["subnet-a", "subnet-b"]
        assert netcfg["securityGroups"] == ["sg-abc"]
        assert netcfg["assignPublicIp"] == "DISABLED"
        # Tags carry agent_id + issue_number for CloudWatch correlation.
        tag_map = {t["key"]: t["value"] for t in call_kwargs["tags"]}
        assert tag_map["agent_id"] == "agent-1"
        assert tag_map["issue_number"] == "42"
        # ECS Exec enabled per-invocation (#3145) — operators need to
        # shell into a running agent-runner for live debugging.
        assert call_kwargs["enableExecuteCommand"] is True

    def test_success_persists_task_arn_and_execution_mode(self) -> None:
        d, conn = _make_daemon()
        fake_client = MagicMock()
        fake_client.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
            "failures": [],
        }
        with patch.object(d, "_make_ecs_client", return_value=fake_client):
            d._launch_agent_ecs_task("agent-2", 7)
        # UPDATE dispatcher.agents was called with the ARN and mode.
        assert any(
            "UPDATE dispatcher.agents" in sql
            and "agent_task_arn" in sql
            and "execution_mode = 'ecs'" in sql
            for sql, _ in conn.cursor_obj.executed
        )

    def test_log_event_on_success(self, caplog: Any) -> None:
        d, _ = _make_daemon()
        caplog.set_level(logging.INFO, logger="test.daemon_launch_agent_ecs_task")
        fake_client = MagicMock()
        fake_client.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/xyz"}],
            "failures": [],
        }
        with patch.object(d, "_make_ecs_client", return_value=fake_client):
            d._launch_agent_ecs_task("agent-ok", 101)
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_run_task_succeeded" in events


# --------------------------------------------------------------------------
# Retry on transient errors.
# --------------------------------------------------------------------------


class TestLaunchAgentECSTaskRetry:
    def test_throttle_twice_then_success(self, caplog: Any) -> None:
        d, _ = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_launch_agent_ecs_task")
        fake_client = MagicMock()
        fake_client.run_task.side_effect = [
            _client_error("ThrottlingException"),
            _client_error("ThrottlingException"),
            {
                "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/good"}],
                "failures": [],
            },
        ]
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            result = d._launch_agent_ecs_task("agent-r", 1)
        assert result == "arn:aws:ecs:us-west-2:123:task/good"
        assert fake_client.run_task.call_count == 3
        # 1s + 2s backoff between the three attempts.
        assert mock_sleep.call_args_list[0].args[0] == 1.0
        assert mock_sleep.call_args_list[1].args[0] == 2.0
        # Two retry-attempt warnings, but NO exhausted warning.
        retry_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_run_task_attempt_failed"
        ]
        assert len(retry_records) == 2
        assert all(r.retryable is True for r in retry_records)
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_run_task_exhausted"
        ]
        assert exhausted == []

    def test_three_throttles_exhausts_and_returns_none(self, caplog: Any) -> None:
        d, _ = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_launch_agent_ecs_task")
        fake_client = MagicMock()
        fake_client.run_task.side_effect = _client_error("ThrottlingException")
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            result = d._launch_agent_ecs_task("agent-e", 2)
        assert result is None
        assert fake_client.run_task.call_count == 3
        # Two backoffs between three attempts.
        assert mock_sleep.call_count == 2
        retry_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_run_task_attempt_failed"
        ]
        assert len(retry_records) == 3
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_run_task_exhausted"
        ]
        assert len(exhausted) == 1

    def test_non_retryable_fails_immediately(self, caplog: Any) -> None:
        d, _ = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_launch_agent_ecs_task")
        fake_client = MagicMock()
        fake_client.run_task.side_effect = _client_error("AccessDeniedException")
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            result = d._launch_agent_ecs_task("agent-perm", 3)
        assert result is None
        # Only one attempt — AccessDenied is not retryable.
        assert fake_client.run_task.call_count == 1
        # No backoff was incurred.
        mock_sleep.assert_not_called()
        retry_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_run_task_attempt_failed"
        ]
        assert len(retry_records) == 1
        assert retry_records[0].retryable is False

    def test_response_failures_trigger_retry(self) -> None:
        d, _ = _make_daemon()
        fake_client = MagicMock()
        # First two responses: 200 OK but failures[] set (capacity
        # unavailable). Third: success.
        fake_client.run_task.side_effect = [
            {"tasks": [], "failures": [{"reason": "AGENT", "detail": "capacity"}]},
            {"tasks": [], "failures": [{"reason": "AGENT", "detail": "capacity"}]},
            {
                "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/late"}],
                "failures": [],
            },
        ]
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch("dispatcher.daemon.time.sleep"),
        ):
            result = d._launch_agent_ecs_task("agent-c", 4)
        assert result == "arn:aws:ecs:us-west-2:123:task/late"
        assert fake_client.run_task.call_count == 3


# --------------------------------------------------------------------------
# Wiring guard — missing config fails closed.
# --------------------------------------------------------------------------


class TestLaunchAgentECSTaskWiringGuard:
    def test_missing_cluster_returns_none(self, caplog: Any) -> None:
        d, _ = _make_daemon(cluster_arn="")
        caplog.set_level(logging.ERROR, logger="test.daemon_launch_agent_ecs_task")
        with patch.object(d, "_make_ecs_client") as mock_make:
            result = d._launch_agent_ecs_task("agent-x", 1)
        assert result is None
        mock_make.assert_not_called()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_wiring_missing" in events

    def test_missing_task_def_returns_none(self) -> None:
        d, _ = _make_daemon(task_def_family="")
        with patch.object(d, "_make_ecs_client") as mock_make:
            assert d._launch_agent_ecs_task("agent-x", 1) is None
        mock_make.assert_not_called()

    def test_missing_subnets_returns_none(self) -> None:
        d, _ = _make_daemon(subnets=())
        with patch.object(d, "_make_ecs_client") as mock_make:
            assert d._launch_agent_ecs_task("agent-x", 1) is None
        mock_make.assert_not_called()

    def test_missing_sg_returns_none(self) -> None:
        d, _ = _make_daemon(sg="")
        with patch.object(d, "_make_ecs_client") as mock_make:
            assert d._launch_agent_ecs_task("agent-x", 1) is None
        mock_make.assert_not_called()


# --------------------------------------------------------------------------
# botocore error-code extractor.
# --------------------------------------------------------------------------


class TestExtractBotocoreErrorCode:
    def test_client_error_shape_returns_code(self) -> None:
        assert (
            daemon._extract_botocore_error_code(_client_error("Throttling"))
            == "Throttling"
        )

    def test_non_botocore_exception_returns_empty(self) -> None:
        assert daemon._extract_botocore_error_code(RuntimeError("nope")) == ""

    def test_missing_error_key_returns_empty(self) -> None:
        exc = Exception("no error key")
        exc.response = {"NotError": {}}  # type: ignore[attr-defined]
        assert daemon._extract_botocore_error_code(exc) == ""
