"""Unit tests for #2854 scheduler_tick cadence-slip detection.

The dispatcher daemon emits two pieces of cadence observability on
every ``scheduler_tick``:

* An ``inter_tick_seconds`` field on the regular ``daemon.scheduler_tick``
  INFO event, computed as the gap between this tick's emission timestamp
  and the previous tick's. ``None`` on the first tick after boot (no
  prior reference). Powers the ``TickCadenceSeconds`` CloudWatch metric
  filter / alarm.

* A separate ``daemon.tick_cadence_slip`` WARNING event when
  ``inter_tick_seconds > tick_cadence_slip_multiplier × tick_scheduler_seconds``
  (default multiplier 2.0, so 60s on the 30s cadence). This is the
  belt-and-braces signal — distinct from the metric filter alarm so an
  operator running ``tail -f`` on logs sees the slip immediately,
  without waiting for the CloudWatch alarm's 5-min p95 window.

These cover the daemon-side acceptance criteria of #2854. Terraform
side (the metric filter + alarm + variables) is exercised by terraform
plan / apply on dev — there is no python-level test for the HCL.

No real subprocess spawned, no real CloudWatch / ECS reached.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package —
# mirrors the preamble in ``test_daemon_scheduler_tick_instrumentation.py``.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (local copies for the same reason the sibling
# ``test_daemon_scheduler_tick_instrumentation.py`` file keeps them
# local — there's no shared helper module today).
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
    logger = logging.getLogger(f"dispatcher.test.cadence_slip.{id(tmp_path)}")
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
    # Stub the queue scans so scheduler_tick does not touch ``gh``.
    d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
    d._scan_blocked_and_snapshot = lambda: 0  # type: ignore[method-assign]
    d._maybe_spawn_orchestration_thread = MagicMock(return_value=False)  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# AC #1 — first tick has no prior reference, so ``inter_tick_seconds``
# is ``None`` and no slip event is emitted.
# --------------------------------------------------------------------------


class TestFirstTickHasNoInterTickReference:
    """The first scheduler_tick after boot cannot compute an inter-tick
    interval — there is no previous tick to compare against. The
    ``inter_tick_seconds`` field on the regular event must be ``None``
    and no ``daemon.tick_cadence_slip`` WARNING may fire."""

    def test_first_tick_inter_tick_seconds_is_none(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        events = handler.events("scheduler_tick")
        assert len(events) == 1
        assert getattr(events[0], "inter_tick_seconds", "missing") is None

    def test_first_tick_emits_no_slip_event(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        assert handler.events("tick_cadence_slip") == []


# --------------------------------------------------------------------------
# AC #2 — second tick on a healthy cadence produces a small
# ``inter_tick_seconds`` value and no slip event.
# --------------------------------------------------------------------------


class TestHealthyCadenceEmitsNoSlip:
    """When the gap between consecutive ticks is below the slip
    threshold (default ``2.0 × tick_scheduler_seconds`` = 60s on the
    30s cadence), the ``daemon.scheduler_tick`` event must carry a
    small numeric ``inter_tick_seconds`` and no ``tick_cadence_slip``
    WARNING may fire."""

    def test_healthy_second_tick_has_small_inter_tick_seconds(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        # Second tick fires immediately — gap is sub-second.
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        events = handler.events("scheduler_tick")
        assert len(events) == 2
        inter = getattr(events[1], "inter_tick_seconds", None)
        assert isinstance(inter, (int, float))
        # Default cadence is 30s; the gap here is sub-second, so well
        # below the slip threshold of 60s.
        assert 0.0 <= inter < 1.0

    def test_healthy_cadence_emits_no_slip_event(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        assert handler.events("tick_cadence_slip") == []


# --------------------------------------------------------------------------
# AC #3 — when the inter-tick gap exceeds
# ``tick_cadence_slip_multiplier × tick_scheduler_seconds``, a
# ``daemon.tick_cadence_slip`` WARNING is logged with the elapsed,
# expected cadence, threshold, and multiplier on the record.
# --------------------------------------------------------------------------


class TestSlowCadenceEmitsSlipWarning:
    """The synthetic slip path: stale ``_previous_scheduler_tick_at``
    forces the slip-detector branch to fire on the next tick."""

    def test_stale_previous_tick_fires_slip_warning(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        # Simulate the daemon having last emitted a tick well over the
        # slip threshold ago. Default cadence is 30s, multiplier 2.0
        # → threshold 60s. We backdate by 90s so the gap is decisively
        # over the threshold even with monotonic clock noise.
        slip_lookback = (
            daemon.DEFAULT_TICK_CADENCE_SLIP_MULTIPLIER
            * float(daemon.DEFAULT_SCHEDULER_TICK_SECONDS)
            + 30.0
        )
        d._previous_scheduler_tick_at = time.monotonic() - slip_lookback
        d.scheduler_tick()
        slip_events = handler.events("tick_cadence_slip")
        assert len(slip_events) == 1
        rec = slip_events[0]
        assert rec.levelno == logging.WARNING
        # Inspect the structured payload — the alarm wiring depends on
        # these field names being stable.
        elapsed = getattr(rec, "inter_tick_seconds", None)
        assert isinstance(elapsed, (int, float))
        assert elapsed >= slip_lookback - 1.0  # allow scheduling jitter
        expected_cadence = getattr(rec, "expected_cadence_seconds", None)
        assert expected_cadence == daemon.DEFAULT_SCHEDULER_TICK_SECONDS
        threshold = getattr(rec, "threshold_seconds", None)
        assert isinstance(threshold, (int, float))
        assert threshold == round(
            daemon.DEFAULT_TICK_CADENCE_SLIP_MULTIPLIER
            * float(daemon.DEFAULT_SCHEDULER_TICK_SECONDS),
            3,
        )
        multiplier = getattr(rec, "multiplier", None)
        assert multiplier == daemon.DEFAULT_TICK_CADENCE_SLIP_MULTIPLIER

    def test_slip_warning_includes_tick_n(self, tmp_path: Path) -> None:
        """Operators correlate slip events with the surrounding
        ``daemon.scheduler_tick`` events via ``tick_n``."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d._previous_scheduler_tick_at = (
            time.monotonic() - 120.0
        )  # well past 60s threshold
        d.scheduler_tick()
        rec = handler.events("tick_cadence_slip")[0]
        # ``tick_n`` is the daemon's monotonic tick counter — first tick
        # is 1 (incremented at the top of scheduler_tick before this
        # event fires).
        assert getattr(rec, "tick_n", None) == 1


# --------------------------------------------------------------------------
# AC #4 — the per-tick INFO event still records the elapsed gap on a
# slip-firing tick (so the metric filter still sees the value).
# --------------------------------------------------------------------------


class TestSchedulerTickEventCarriesInterTickSecondsOnSlip:
    """Even when a slip WARNING fires, the regular ``daemon.scheduler_tick``
    INFO event still must include the ``inter_tick_seconds`` field —
    the CloudWatch metric filter that powers the alarm reads from the
    INFO event, not the WARN event. (We want both: the WARN for fast
    operator visibility, the INFO field for the cleaner p95 metric.)"""

    def test_inter_tick_seconds_on_info_event_when_slip_fires(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d._previous_scheduler_tick_at = time.monotonic() - 200.0  # far over threshold
        d.scheduler_tick()
        info_events = handler.events("scheduler_tick")
        assert len(info_events) == 1
        inter = getattr(info_events[0], "inter_tick_seconds", None)
        assert isinstance(inter, (int, float))
        assert inter >= 199.0


# --------------------------------------------------------------------------
# AC #5 — multiplier defaults are sane (matches spec).
# --------------------------------------------------------------------------


class TestDefaultTickCadenceSlipMultiplier:
    """Sanity-check the module constant against the spec — the
    threshold must be 2× the cadence so 60s on the 30s cadence and we
    stay quiet on the healthy 30-32s common case."""

    def test_default_multiplier_is_two(self) -> None:
        # Allow a small float tolerance in case the constant ever
        # becomes operator-tunable and is read as a float.
        assert daemon.DEFAULT_TICK_CADENCE_SLIP_MULTIPLIER == 2.0

    def test_threshold_is_60s_on_default_cadence(self) -> None:
        threshold = daemon.DEFAULT_TICK_CADENCE_SLIP_MULTIPLIER * float(
            daemon.DEFAULT_SCHEDULER_TICK_SECONDS
        )
        assert threshold == 60.0
