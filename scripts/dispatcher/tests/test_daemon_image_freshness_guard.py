"""Unit tests for issue #3754 — agent-runner image-freshness guard.

The guard lives in ``DispatcherDaemon._check_agent_runner_image_freshness``
and is called from ``_launch_agent_ecs_task`` before ``ecs:RunTask``.

Test coverage:

* Digest mismatch (task-def pinned to an old ``@sha256:`` digest, ECR
  ``:latest`` has a newer digest) → ``_check_agent_runner_image_freshness``
  returns ``False`` and ``_launch_agent_ecs_task`` returns ``None``.
* Timestamp staleness (task-def registered at T, ECR ``:latest`` pushed at
  T + FRESHNESS_WINDOW + 1s) → same refuse-launch outcome.
* Fresh image (ECR ``:latest`` pushed within the freshness window) → guard
  returns ``True`` and launch proceeds.
* AWS API error in the freshness check → fail-open (guard returns ``True``).
* Task-def wiring missing (empty family) → guard returns ``True`` (skip).

Fakes follow the ``test_daemon_launch_agent_ecs_task.py`` pattern:
bypass ``__init__`` via ``__new__``, stub psycopg via conftest, use
``MagicMock`` ECS + ECR clients.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from pathlib import Path

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


_ECR_REPO = "judgemind/dispatcher-agent-runner"
_TASK_DEF_FAMILY = "judgemind-dispatcher-agent-runner-dev"
_CLUSTER_ARN = "arn:aws:ecs:us-west-2:123:cluster/judgemind-dev"
_TASK_ARN = "arn:aws:ecs:us-west-2:123:task/judgemind-dev/abc"


def _make_daemon(
    *,
    task_def_family: str = _TASK_DEF_FAMILY,
    cluster_arn: str = _CLUSTER_ARN,
    subnets: tuple[str, ...] = ("subnet-a",),
    sg: str = "sg-abc",
    aws_region: str = "us-west-2",
) -> tuple[daemon.DispatcherDaemon, _FakeConn]:
    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    conn = _FakeConn()
    d._main_conn = conn  # type: ignore[attr-defined]
    d._cfg = MagicMock(  # type: ignore[attr-defined]
        aws_region=aws_region,
        ecs_cluster_arn=cluster_arn,
        agent_runner_task_definition_family=task_def_family,
        agent_runner_subnet_ids=subnets,
        agent_runner_security_group_id=sg,
    )
    d._log = logging.getLogger("test.daemon_image_freshness_guard")  # type: ignore[attr-defined]
    d._run_id = "test-run-id"  # type: ignore[attr-defined]
    d._ecs_client = None  # type: ignore[attr-defined]
    return d, conn


# UTC-aware timestamps for building test fixtures.
_DEPLOY_TIME = datetime(2026, 4, 29, 0, 12, 40, tzinfo=UTC)  # post-#3750
_TASK_DEF_TIME = datetime(2026, 4, 24, 3, 25, 19, tzinfo=UTC)  # pre-#3750

_OLD_DIGEST = (
    "sha256:aaa000000000000000000000000000000000000000000000000000000000000001"
)
_NEW_DIGEST = (
    "sha256:bbb000000000000000000000000000000000000000000000000000000000000002"
)


def _ecs_describe_task_definition_response(
    *,
    registered_at: datetime = _TASK_DEF_TIME,
    image: str = (
        "155326049300.dkr.ecr.us-west-2.amazonaws.com/" + _ECR_REPO + ":latest"
    ),
) -> dict[str, Any]:
    """Build a minimal ``ecs:DescribeTaskDefinition`` response."""
    return {
        "taskDefinition": {
            "taskDefinitionArn": (
                f"arn:aws:ecs:us-west-2:123:task-definition/{_TASK_DEF_FAMILY}:2"
            ),
            "containerDefinitions": [{"name": "agent-runner", "image": image}],
            "registeredAt": registered_at,
            "revision": 2,
        },
    }


def _ecr_describe_images_response(
    *,
    pushed_at: datetime = _DEPLOY_TIME,
    digest: str = _NEW_DIGEST,
) -> dict[str, Any]:
    """Build a minimal ``ecr:DescribeImages`` response."""
    return {
        "imageDetails": [
            {
                "imageDigest": digest,
                "imageTags": ["latest"],
                "imagePushedAt": pushed_at,
            }
        ],
    }


# --------------------------------------------------------------------------
# _check_agent_runner_image_freshness — unit tests
# --------------------------------------------------------------------------


class TestImageFreshnessGuardDigestMismatch:
    """Task-def pinned to old digest; ECR latest has a newer digest → STALE."""

    def test_stale_digest_refuses_launch(self, caplog: Any) -> None:
        """AC2: guard detects digest mismatch and returns False."""
        d, _ = _make_daemon()
        caplog.set_level(logging.ERROR, logger="test.daemon_image_freshness_guard")

        pinned_image = (
            "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
            + _ECR_REPO
            + "@"
            + _OLD_DIGEST
        )
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(image=pinned_image)
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            digest=_NEW_DIGEST
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_image_stale" in events

    def test_stale_log_carries_both_digests(self, caplog: Any) -> None:
        """The structured log event includes both old and new digest."""
        d, _ = _make_daemon()
        caplog.set_level(logging.ERROR, logger="test.daemon_image_freshness_guard")

        pinned_image = (
            "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
            + _ECR_REPO
            + "@"
            + _OLD_DIGEST
        )
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(image=pinned_image)
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            digest=_NEW_DIGEST
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            d._check_agent_runner_image_freshness()

        stale_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_image_stale"
        ]
        assert len(stale_records) == 1
        rec = stale_records[0]
        assert rec.task_def_image_digest == _OLD_DIGEST  # type: ignore[attr-defined]
        assert rec.ecr_latest_digest == _NEW_DIGEST  # type: ignore[attr-defined]
        assert rec.reason == "digest_mismatch"  # type: ignore[attr-defined]

    def test_same_digest_returns_true(self) -> None:
        """Matching digests → fresh → True."""
        d, _ = _make_daemon()

        pinned_image = (
            "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
            + _ECR_REPO
            + "@"
            + _NEW_DIGEST
        )
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(image=pinned_image)
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            digest=_NEW_DIGEST,
            # pushed slightly after task-def but digests match → fresh
            pushed_at=_TASK_DEF_TIME + timedelta(seconds=60),
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is True


class TestImageFreshnessGuardTimestampStaleness:
    """ECR :latest pushed > FRESHNESS_WINDOW after task-def registered → STALE."""

    def test_ecr_pushed_after_freshness_window_refuses_launch(
        self, caplog: Any
    ) -> None:
        """Staleness > FRESHNESS_WINDOW triggers refuse-launch."""
        d, _ = _make_daemon()
        caplog.set_level(logging.ERROR, logger="test.daemon_image_freshness_guard")

        # ECR pushed FRESHNESS_WINDOW + 60 seconds after task-def registered.
        stale_push = _TASK_DEF_TIME + timedelta(
            seconds=daemon.AGENT_RUNNER_IMAGE_FRESHNESS_WINDOW_SECONDS + 60
        )
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(
                registered_at=_TASK_DEF_TIME,
                # :latest tag (no pinned digest)
                image=(
                    "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
                    + _ECR_REPO
                    + ":latest"
                ),
            )
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            pushed_at=stale_push,
            digest=_NEW_DIGEST,
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_image_stale" in events

    def test_stale_log_carries_staleness_seconds(self, caplog: Any) -> None:
        """Structured log on timestamp-based staleness includes staleness_seconds."""
        d, _ = _make_daemon()
        caplog.set_level(logging.ERROR, logger="test.daemon_image_freshness_guard")

        extra_seconds = 600
        stale_push = _TASK_DEF_TIME + timedelta(
            seconds=daemon.AGENT_RUNNER_IMAGE_FRESHNESS_WINDOW_SECONDS + extra_seconds
        )
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(
                registered_at=_TASK_DEF_TIME,
                image=(
                    "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
                    + _ECR_REPO
                    + ":latest"
                ),
            )
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            pushed_at=stale_push,
            digest=_NEW_DIGEST,
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            d._check_agent_runner_image_freshness()

        stale_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_image_stale"
        ]
        assert len(stale_records) == 1
        rec = stale_records[0]
        assert rec.reason == "ecr_pushed_after_task_def_registered"  # type: ignore[attr-defined]
        expected_staleness = (
            daemon.AGENT_RUNNER_IMAGE_FRESHNESS_WINDOW_SECONDS + extra_seconds
        )
        assert rec.staleness_seconds == expected_staleness  # type: ignore[attr-defined]

    def test_ecr_pushed_within_freshness_window_is_fresh(self) -> None:
        """Staleness within window → fresh → True."""
        d, _ = _make_daemon()

        # ECR pushed FRESHNESS_WINDOW - 1 seconds after task-def → within window.
        fresh_push = _TASK_DEF_TIME + timedelta(
            seconds=daemon.AGENT_RUNNER_IMAGE_FRESHNESS_WINDOW_SECONDS - 1
        )
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(
                registered_at=_TASK_DEF_TIME,
                image=(
                    "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
                    + _ECR_REPO
                    + ":latest"
                ),
            )
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            pushed_at=fresh_push,
            digest=_NEW_DIGEST,
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is True

    def test_ecr_pushed_before_task_def_is_fresh(self) -> None:
        """ECR older than task-def registration → no staleness → True."""
        d, _ = _make_daemon()

        # Task-def registered after ECR push → ECR is "older" → no staleness.
        late_td_time = _DEPLOY_TIME + timedelta(hours=1)
        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response(
                registered_at=late_td_time,
                image=(
                    "155326049300.dkr.ecr.us-west-2.amazonaws.com/"
                    + _ECR_REPO
                    + ":latest"
                ),
            )
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = _ecr_describe_images_response(
            pushed_at=_DEPLOY_TIME,
            digest=_NEW_DIGEST,
        )

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is True


class TestImageFreshnessGuardFailOpen:
    """AWS API errors → fail-open → True (launch proceeds)."""

    def test_ecr_access_denied_returns_true(self, caplog: Any) -> None:
        """An ECR AccessDenied error is caught and the guard fails open."""
        d, _ = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_image_freshness_guard")

        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response()
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.side_effect = RuntimeError("AccessDenied")

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is True
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_image_freshness_check_failed" in events

    def test_ecs_describe_error_returns_true(self, caplog: Any) -> None:
        """An ECS DescribeTaskDefinition error also fails open."""
        d, _ = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_image_freshness_guard")

        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.side_effect = RuntimeError("Throttle")

        with patch.object(d, "_make_ecs_client", return_value=fake_ecs):
            result = d._check_agent_runner_image_freshness()

        assert result is True

    def test_empty_ecr_image_list_returns_true(self) -> None:
        """No imageDetails in ECR response → skip → True."""
        d, _ = _make_daemon()

        fake_ecs = MagicMock()
        fake_ecs.describe_task_definition.return_value = (
            _ecs_describe_task_definition_response()
        )
        fake_ecr = MagicMock()
        fake_ecr.describe_images.return_value = {"imageDetails": []}

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_make_ecr_client", return_value=fake_ecr),
        ):
            result = d._check_agent_runner_image_freshness()

        assert result is True


class TestImageFreshnessGuardSkipConditions:
    """Guard skips its checks when wiring is absent."""

    def test_empty_task_def_family_skips_check(self) -> None:
        """No task-def family → skip → True (unit-test / local-dev mode)."""
        d, _ = _make_daemon(task_def_family="")
        with patch.object(d, "_make_ecs_client") as mock_make:
            result = d._check_agent_runner_image_freshness()
        assert result is True
        mock_make.assert_not_called()


class TestLaunchAgentECSTaskFreshnessIntegration:
    """_launch_agent_ecs_task refuses launch when guard returns False."""

    def test_stale_image_causes_launch_to_return_none(self, caplog: Any) -> None:
        """Guard returning False → _launch_agent_ecs_task returns None."""
        d, _ = _make_daemon()
        caplog.set_level(logging.ERROR, logger="test.daemon_image_freshness_guard")

        fake_ecs = MagicMock()
        # run_task should NOT be called when the guard refuses.
        fake_ecs.run_task.return_value = {
            "tasks": [{"taskArn": _TASK_ARN}],
            "failures": [],
        }

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_check_agent_runner_image_freshness", return_value=False),
        ):
            result = d._launch_agent_ecs_task("agent-stale", 99)

        assert result is None
        fake_ecs.run_task.assert_not_called()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_launch_refused_stale_image" in events

    def test_fresh_image_proceeds_to_run_task(self) -> None:
        """Guard returning True → _launch_agent_ecs_task calls run_task."""
        d, _ = _make_daemon()

        fake_ecs = MagicMock()
        fake_ecs.run_task.return_value = {
            "tasks": [{"taskArn": _TASK_ARN}],
            "failures": [],
        }

        with (
            patch.object(d, "_make_ecs_client", return_value=fake_ecs),
            patch.object(d, "_check_agent_runner_image_freshness", return_value=True),
        ):
            result = d._launch_agent_ecs_task("agent-fresh", 42)

        assert result == _TASK_ARN
        fake_ecs.run_task.assert_called_once()
