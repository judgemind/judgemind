"""Unit tests for the boto3 CloudWatch client timeout config (#3356).

Mirror of :mod:`test_daemon_ecs_client_timeout` for the CloudWatch client.

Why this test exists
--------------------
The 2026-04-25 cascade revealed that boto3's default ``read_timeout``
on the dispatcher's ECS client was effectively unbounded; PR #3353
fixed the ECS site. The same defect was present on the CloudWatch
client (used every supervisor tick to emit ``HeartbeatAge=0``): a hung
``cloudwatch:PutMetricData`` would have wedged the supervisor thread
the same way a hung ``ecs:DescribeTasks`` wedged the scheduler.

Fix: :meth:`DispatcherDaemon._make_cloudwatch_client` now constructs
the boto3 client with an explicit :class:`botocore.config.Config`
carrying a small connect-timeout, a small read-timeout, and a bounded
retry policy. Worst-case wall-clock for a hung PutMetricData is now
~25 seconds (one retry x 10s read + 5s connect) instead of indefinite.

Tests below assert the constants are sane AND that
:meth:`_make_cloudwatch_client` actually wires them onto the boto3
client's ``meta.config`` object.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  -- sys.path mutation above


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_daemon() -> daemon.DispatcherDaemon:
    """Build a minimal daemon instance for client-construction tests.

    Bypasses the normal ctor (and its DB / arg parsing) by using
    ``__new__`` and seeding only the fields ``_make_cloudwatch_client``
    reads -- currently just ``self._cfg.aws_region``.
    """
    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._cfg = MagicMock(aws_region="us-west-2")  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon_cloudwatch_client_timeout")  # type: ignore[attr-defined]
    return d


# --------------------------------------------------------------------------
# Module constants
# --------------------------------------------------------------------------


class TestModuleConstants:
    def test_connect_timeout_default(self) -> None:
        # Match the ECS client budget -- hung CloudWatch and hung ECS
        # have the same wedge mechanism (urllib3 blocking ``recv()``
        # holding the GIL), so the timeout shape should be identical.
        assert daemon.CLOUDWATCH_CLIENT_CONNECT_TIMEOUT_SECONDS == 5

    def test_read_timeout_default(self) -> None:
        assert daemon.CLOUDWATCH_CLIENT_READ_TIMEOUT_SECONDS == 10

    def test_max_attempts_default(self) -> None:
        assert daemon.CLOUDWATCH_CLIENT_MAX_ATTEMPTS == 2

    def test_timeouts_below_watchdog_warn(self) -> None:
        """The total CloudWatch-client budget must fit inside the
        watchdog WARN threshold so a hung call surfaces as
        ``ReadTimeoutError`` BEFORE the watchdog stack-dumps the
        process. Worst-case is one retry, so total approx
        max_attempts x (connect + read).
        """
        worst_case = daemon.CLOUDWATCH_CLIENT_MAX_ATTEMPTS * (
            daemon.CLOUDWATCH_CLIENT_CONNECT_TIMEOUT_SECONDS
            + daemon.CLOUDWATCH_CLIENT_READ_TIMEOUT_SECONDS
        )
        assert worst_case < daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS, (
            f"CloudWatch client worst-case {worst_case}s must be < watchdog "
            f"WARN {daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS}s so a "
            f"hung PutMetricData raises ReadTimeoutError before the watchdog fires."
        )

    def test_matches_ecs_client_budget(self) -> None:
        """CloudWatch and ECS share the same wedge mechanism -- their
        timeout budgets should match. If they ever diverge, this test
        will surface the question 'why?' explicitly."""
        assert (
            daemon.CLOUDWATCH_CLIENT_CONNECT_TIMEOUT_SECONDS
            == daemon.ECS_CLIENT_CONNECT_TIMEOUT_SECONDS
        )
        assert (
            daemon.CLOUDWATCH_CLIENT_READ_TIMEOUT_SECONDS
            == daemon.ECS_CLIENT_READ_TIMEOUT_SECONDS
        )
        assert daemon.CLOUDWATCH_CLIENT_MAX_ATTEMPTS == daemon.ECS_CLIENT_MAX_ATTEMPTS


# --------------------------------------------------------------------------
# _make_cloudwatch_client wires botocore.config.Config onto the client
# --------------------------------------------------------------------------


class TestMakeCloudwatchClientConfig:
    """Assert ``_make_cloudwatch_client`` constructs a boto3 client with
    the expected ``botocore.config.Config`` parameters.

    Mirrors the structure of ``TestMakeEcsClientConfig`` -- assert on
    the Config object passed to ``boto3.client(...)`` rather than on
    ``client.meta.config``, because the latter is the merged config
    that boto3 builds from session defaults + env vars + our Config.
    """

    def _capture_config(self, monkeypatch: Any) -> tuple[Any, list[Any]]:
        """Patch ``boto3.client`` to record (a snapshot of) the kwargs."""
        import boto3  # local import so the test is skipped on a venv
        # without boto3 (the dispatcher venv always has it).

        captured: list[dict[str, Any]] = []
        real_client = boto3.client

        def recording_client(*args: Any, **kwargs: Any) -> Any:
            cfg = kwargs.get("config")
            snapshot = {
                "args": args,
                "kwargs": dict(kwargs),
                "config_snapshot": (
                    {
                        "connect_timeout": getattr(cfg, "connect_timeout", None),
                        "read_timeout": getattr(cfg, "read_timeout", None),
                        "retries": dict(cfg.retries) if cfg and cfg.retries else {},
                    }
                    if cfg is not None
                    else None
                ),
            }
            captured.append(snapshot)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(boto3, "client", recording_client)
        d = _make_daemon()
        return d, captured

    def test_make_cloudwatch_client_passes_read_timeout_to_boto3(
        self, monkeypatch: Any
    ) -> None:
        """The boto3 client constructed in ``_make_cloudwatch_client``
        must receive a ``Config`` with the configured ``read_timeout``
        so a hung ``cloudwatch:PutMetricData`` raises rather than
        blocks forever."""
        d, captured = self._capture_config(monkeypatch)

        d._make_cloudwatch_client()

        assert len(captured) == 1, captured
        snap = captured[0]["config_snapshot"]
        assert snap is not None, "_make_cloudwatch_client must pass a config="
        assert snap["read_timeout"] == daemon.CLOUDWATCH_CLIENT_READ_TIMEOUT_SECONDS, (
            f"expected read_timeout={daemon.CLOUDWATCH_CLIENT_READ_TIMEOUT_SECONDS},"
            f" got {snap['read_timeout']}"
        )

    def test_make_cloudwatch_client_passes_connect_timeout_to_boto3(
        self, monkeypatch: Any
    ) -> None:
        """Connect timeout must also be bounded -- a TCP SYN black-hole
        would otherwise hang forever in
        ``urllib3.HTTPConnection.connect``."""
        d, captured = self._capture_config(monkeypatch)

        d._make_cloudwatch_client()

        snap = captured[0]["config_snapshot"]
        assert snap is not None
        assert (
            snap["connect_timeout"] == daemon.CLOUDWATCH_CLIENT_CONNECT_TIMEOUT_SECONDS
        ), (
            f"expected connect_timeout="
            f"{daemon.CLOUDWATCH_CLIENT_CONNECT_TIMEOUT_SECONDS}, "
            f"got {snap['connect_timeout']}"
        )

    def test_make_cloudwatch_client_passes_retries_to_boto3(
        self, monkeypatch: Any
    ) -> None:
        """The retries config must cap attempts at the configured max so
        a single transient hang triggers exactly one retry before
        raising."""
        d, captured = self._capture_config(monkeypatch)

        d._make_cloudwatch_client()

        snap = captured[0]["config_snapshot"]
        assert snap is not None
        retries = snap["retries"]
        assert retries.get("max_attempts") == daemon.CLOUDWATCH_CLIENT_MAX_ATTEMPTS, (
            f"expected retries.max_attempts="
            f"{daemon.CLOUDWATCH_CLIENT_MAX_ATTEMPTS}, got {retries}"
        )
        assert retries.get("mode") == "standard", retries

    def test_make_cloudwatch_client_uses_configured_region(self) -> None:
        """Sanity: the daemon's ``aws_region`` is wired to the boto3
        client. Regression guard for a refactor that drops the kwarg."""
        d = _make_daemon()
        d._cfg.aws_region = "us-east-1"  # type: ignore[attr-defined]

        client = d._make_cloudwatch_client()

        assert client.meta.region_name == "us-east-1"
