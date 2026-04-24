"""Unit tests for #2854 — scheduler tick cadence monitoring.

Covers the two helpers added to :mod:`scripts.dispatcher.daemon`:

* :meth:`DispatcherDaemon._check_tick_cadence_slip` — emits a WARNING
  ``daemon.tick_cadence_slip`` event when ``now - last_scheduler`` is
  more than 2× the configured cadence.  Silent on boot tick
  (``last_scheduler == 0.0``) and on ticks that arrive on time.

* :meth:`DispatcherDaemon._emit_tick_cadence_metric` — posts
  ``TickCadenceSeconds`` to CloudWatch under the ``Judgemind/Dispatcher``
  namespace with a ``Service`` dimension.  Swallows boto3 errors and
  resets the client for the next tick.

No real CloudWatch / ECS is reached.  No real Postgres is used.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable without installing the repo as a package —
# mirrors the preamble in the other ``test_daemon_*.py`` modules.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (local copies so this test file stays self-contained —
# the established convention in this test suite).
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
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
    logger = logging.getLogger(f"dispatcher.test.cadence.{id(tmp_path)}")
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
    d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
    d._scan_blocked_and_snapshot = lambda: 0  # type: ignore[method-assign]
    d._maybe_spawn_orchestration_thread = MagicMock(return_value=False)  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# _check_tick_cadence_slip
# --------------------------------------------------------------------------


class TestCheckTickCadenceSlip:
    def test_slip_above_2x_cadence_emits_warning(self, tmp_path: Path) -> None:
        """Elapsed > 2*cadence must fire a WARNING with the expected fields."""
        d, _conn, handler = _make_daemon(tmp_path)
        cadence = d._cfg.tick_scheduler_seconds
        # Simulate: last tick was (2*cadence + 0.1) seconds ago.
        last_scheduler = 1000.0
        now = last_scheduler + 2 * cadence + 0.1
        d._check_tick_cadence_slip(now, last_scheduler)

        events = handler.events("tick_cadence_slip")
        assert len(events) == 1, (
            f"expected 1 tick_cadence_slip event, got {len(events)}"
        )
        rec = events[0]
        assert rec.levelno == logging.WARNING
        assert getattr(rec, "slip_multiple", None) == 2
        assert getattr(rec, "cadence_seconds", None) == cadence
        elapsed = getattr(rec, "elapsed_seconds", None)
        assert isinstance(elapsed, float)
        assert elapsed > 2 * cadence

    def test_slip_exactly_2x_does_not_emit(self, tmp_path: Path) -> None:
        """The check is strict (>), so exactly 2× cadence must NOT fire."""
        d, _conn, handler = _make_daemon(tmp_path)
        cadence = d._cfg.tick_scheduler_seconds
        last_scheduler = 1000.0
        # Exactly 2*cadence — must not trip the guard (strict >).
        now = last_scheduler + 2 * cadence
        d._check_tick_cadence_slip(now, last_scheduler)
        assert handler.events("tick_cadence_slip") == []

    def test_fast_tick_does_not_emit(self, tmp_path: Path) -> None:
        """A tick that arrives roughly on time must not fire."""
        d, _conn, handler = _make_daemon(tmp_path)
        cadence = d._cfg.tick_scheduler_seconds
        last_scheduler = 1000.0
        # Simulate a tick that arrived 1× cadence late (on time).
        now = last_scheduler + cadence
        d._check_tick_cadence_slip(now, last_scheduler)
        assert handler.events("tick_cadence_slip") == []

    def test_first_tick_never_emits(self, tmp_path: Path) -> None:
        """Bootstrap tick (last_scheduler == 0.0) must always be silent."""
        d, _conn, handler = _make_daemon(tmp_path)
        # last_scheduler is 0.0 — the sentinel for "no prior tick".
        # Even with a very large 'now' the check must be a no-op.
        d._check_tick_cadence_slip(999999.0, 0.0)
        assert handler.events("tick_cadence_slip") == []


# --------------------------------------------------------------------------
# _emit_tick_cadence_metric
# --------------------------------------------------------------------------


class TestEmitTickCadenceMetric:
    def test_emit_tick_cadence_metric_posts_to_cloudwatch(self, tmp_path: Path) -> None:
        """On success, put_metric_data is called with the correct payload."""
        d, _conn, _handler = _make_daemon(tmp_path)
        fake_client = MagicMock()
        d._cloudwatch_client = fake_client

        elapsed = 65.0
        result = d._emit_tick_cadence_metric(elapsed)

        assert result is True
        fake_client.put_metric_data.assert_called_once()
        call_kwargs = fake_client.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == d._cfg.heartbeat_metric_namespace
        metric = call_kwargs["MetricData"][0]
        assert metric["MetricName"] == "TickCadenceSeconds"
        assert metric["Unit"] == "Seconds"
        assert metric["Value"] == elapsed
        assert metric["Dimensions"] == [
            {"Name": "Service", "Value": "judgemind-dispatcher-test"}
        ]

    def test_emit_tick_cadence_metric_swallows_boto_errors(
        self, tmp_path: Path
    ) -> None:
        """A boto3 exception must return False and reset the client."""
        d, _conn, handler = _make_daemon(tmp_path)
        fake_client = MagicMock()
        fake_client.put_metric_data.side_effect = RuntimeError("boto3 boom")
        d._cloudwatch_client = fake_client

        result = d._emit_tick_cadence_metric(72.5)

        assert result is False
        # Client is reset so the next tick recreates it.
        assert d._cloudwatch_client is None
        # A warning event is logged.
        events = handler.events("tick_cadence_metric_failed")
        assert len(events) == 1
        assert events[0].levelno == logging.WARNING

        # Confirm that a subsequent call attempts to recreate the client.
        new_fake_client = MagicMock()
        with patch.object(d, "_make_cloudwatch_client", return_value=new_fake_client):
            result2 = d._emit_tick_cadence_metric(70.0)
        assert result2 is True
        assert d._cloudwatch_client is new_fake_client
