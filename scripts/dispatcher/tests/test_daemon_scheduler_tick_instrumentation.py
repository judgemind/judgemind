"""Unit tests for #3205 scheduler_tick instrumentation.

Covers the diagnostic-first changes added to :mod:`scripts.dispatcher.daemon`:

* The per-step instrumentation inside :meth:`scheduler_tick` emits
  ``daemon.scheduler_tick_slow_<step>`` WARNING events when a
  sub-step's elapsed wall-clock exceeds
  :data:`SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS`, and updates
  ``_last_scheduler_tick_at`` between steps so the #3097 watchdog sees
  forward progress mid-body.
* :func:`_arm_periodic_faulthandler` is idempotent and safe to call
  multiple times, and honours the
  ``JUDGEMIND_DISPATCHER_DISABLE_FAULTHANDLER_TIMER`` opt-out.

Note: AST-level coverage of ``subprocess.run`` timeout kwargs (formerly
``TestSubprocessTimeoutCoverage``) has been moved to the
``scripts/check-subprocess-timeouts.sh`` hygiene guard (#3213), which
covers all of ``scripts/**/*.py`` rather than just ``daemon.py``.

No real subprocess is spawned. No real CloudWatch / ECS is reached.
"""

from __future__ import annotations

import logging
import sys
import time
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
# the watchdog test file's fakes aren't importable across test modules
# without a shared helper module, and the set of fakes is small.)
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
    logger = logging.getLogger(f"dispatcher.test.sched_instr.{id(tmp_path)}")
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
# AC #1 — ``QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS`` is sane.
#
# The AST-level scan that verified every ``subprocess.run`` call site in
# daemon.py has a ``timeout=`` kwarg has been moved to the broader
# ``scripts/check-subprocess-timeouts.sh`` hygiene guard (#3213), which
# covers all of ``scripts/**/*.py`` rather than just ``daemon.py``.
# --------------------------------------------------------------------------


class TestSubprocessTimeoutCoverage:
    def test_queue_scan_timeout_constant_is_bounded(self) -> None:
        """The shared ``QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS`` drives
        the hot-path ``gh issue list`` calls. Keep it well below the
        scheduler tick cadence so a slow ``gh`` can never pile up
        across ticks."""
        assert daemon.QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS <= 30
        assert daemon.QUEUE_SCAN_SUBPROCESS_TIMEOUT_SECONDS >= 1


# --------------------------------------------------------------------------
# AC #2 — per-step slow warnings from scheduler_tick.
#
# When a step's wall-clock elapsed exceeds
# :data:`SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS`,
# :meth:`_record_scheduler_step` emits a
# ``daemon.scheduler_tick_slow_<step>`` WARNING log event carrying the
# elapsed seconds. This is the "which helper owned the wedge?"
# diagnostic — fires BEFORE the 300s watchdog threshold so the next
# silent wedge self-diagnoses.
# --------------------------------------------------------------------------


class TestRecordSchedulerStep:
    def test_fast_step_does_not_emit_warning(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        # t_before is ~now; elapsed will be tiny → no warning.
        t_before = time.monotonic()
        d._record_scheduler_step("consume_commands", t_before)
        assert handler.events("scheduler_tick_slow_consume_commands") == []

    def test_slow_step_emits_warning_with_elapsed_seconds(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        # Fake a step that took well over the threshold by passing a
        # stale t_before (elapsed = now - stale).
        stale = time.monotonic() - (daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS + 1.0)
        d._record_scheduler_step("active_agent_count", stale)
        events = handler.events("scheduler_tick_slow_active_agent_count")
        assert len(events) == 1
        rec = events[0]
        assert rec.levelno == logging.WARNING
        elapsed = getattr(rec, "elapsed_seconds", None)
        assert isinstance(elapsed, (int, float))
        assert elapsed >= daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS
        assert getattr(rec, "step", None) == "active_agent_count"
        threshold = getattr(rec, "threshold_seconds", None)
        assert threshold == daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS

    def test_record_step_does_not_refresh_last_scheduler_tick_at(
        self, tmp_path: Path
    ) -> None:
        """#3801: per-sub-step heartbeat refresh is REMOVED.

        Pre-#3801 ``_record_scheduler_step`` wrote ``_last_scheduler_tick_at
        = now`` between every sub-step. Intent: "give the watchdog
        forward-progress signal mid-tick." Side effect: a multi-sub-step
        wedge (12 sub-steps × 60s each = 720s tick) could never trip the
        watchdog because each fast sub-step reset the timer. That side
        effect was the load-bearing reason the 2026-04-29 reaper wedge
        stayed silent for hours despite both watchdogs being alive.

        Post-#3801 the heartbeat is set ONLY at scheduler_tick entry
        (and at watchdog start). ``_record_scheduler_step`` does NOT
        touch ``_last_scheduler_tick_at`` — the primary watchdog now
        observes the whole-tick elapsed and fires correctly when any
        single tick exceeds the EXIT threshold.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        d._last_scheduler_tick_at = 0.0  # known-stale sentinel
        d._record_scheduler_step("consume_commands", time.monotonic())
        # Heartbeat must NOT be advanced by the per-step recorder.
        assert d._last_scheduler_tick_at == 0.0

    def test_record_step_returns_current_monotonic(self, tmp_path: Path) -> None:
        """Callers chain ``t_step = _record_scheduler_step(...)`` so the
        return value must be the ``time.monotonic()`` reading taken at
        the end of the recorder. ``_last_scheduler_tick_at`` is no
        longer touched (#3801) so we just bound the return against
        before/after wall-clock."""
        d, _conn, _handler = _make_daemon(tmp_path)
        before = time.monotonic()
        returned = d._record_scheduler_step("consume_commands", before)
        after = time.monotonic()
        assert before <= returned <= after


# --------------------------------------------------------------------------
# AC #2b — scheduler_tick actually calls _record_scheduler_step between
# steps. End-to-end check via a slow monkeypatched helper.
# --------------------------------------------------------------------------


class TestSchedulerTickEmitsSlowEvents:
    def test_slow_queue_scan_emits_slow_scan_queue_event(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]

        def _slow_scan() -> int:
            time.sleep(daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS + 0.25)
            return 0

        d._scan_queue_and_snapshot = _slow_scan  # type: ignore[method-assign]
        d.scheduler_tick()
        events = handler.events("scheduler_tick_slow_scan_queue")
        assert len(events) == 1
        assert getattr(events[0], "levelno", None) == logging.WARNING

    def test_fast_tick_emits_no_slow_events(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        # No step is slow when every helper is a stub that returns
        # immediately, so no warning should fire.
        slow_events = [
            rec
            for rec in handler.records
            if getattr(rec, "event", "").startswith("scheduler_tick_slow_")
        ]
        assert slow_events == [], (
            f"expected no slow-step events on a fast tick; got {[e.event for e in slow_events]!r}"
        )

    def test_scheduler_tick_records_tick_elapsed_s(self, tmp_path: Path) -> None:
        """Every successful tick logs ``tick_elapsed_s`` on the
        ``daemon.scheduler_tick`` event so CloudWatch Insights can
        ``stats avg(tick_elapsed_s) by bin(5m)`` without waiting for a
        slow-step threshold trip."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        events = handler.events("scheduler_tick")
        assert events, "expected at least one scheduler_tick event"
        tick_elapsed = getattr(events[-1], "tick_elapsed_s", None)
        assert isinstance(tick_elapsed, (int, float))
        assert tick_elapsed >= 0.0


# --------------------------------------------------------------------------
# #4109 — scheduler_tick carries per-sub-step elapsed_seconds dict.
#
# The end-of-tick ``scheduler_tick`` INFO event must include a
# ``step_durations`` field (dict of step-name → elapsed_seconds, rounded
# to 3 decimals) covering every sub-step that ran during the tick. This
# lets CloudWatch Logs Insights queries like
# ``stats avg(step_durations.scan_queue) by bin(5m)``
# answer "is the queue scan getting slower?" without waiting for the
# 5s slow-step WARN to fire.
#
# Regression test for #4109 — the gap was that the slow-WARN path was
# the ONLY happy-path source of per-step timing, so a ``scan_queue``
# going from 1s → 4.9s was indistinguishable from one going 0.1s → 0.1s.
# Mirrors the supervisor-side ``TestSupervisorTickStepDurations`` from
# PR #4102 / issue #4087.
# --------------------------------------------------------------------------


class TestSchedulerTickStepDurations:
    """Verify per-sub-step elapsed_seconds appears on every tick (#4109)."""

    # Step names declared in scheduler_tick — keep in sync with the
    # ``_record_scheduler_step`` calls inside ``daemon.scheduler_tick``.
    # On the default fast-tick path used by ``_make_daemon`` (cap=1,
    # ``_active_agent_count`` returns 0 because the fake cursor has no
    # rows, gate opens, ``_maybe_spawn_orchestration_thread`` runs as a
    # mocked no-op) every step including the two conditional ones
    # (``active_agent_count`` and ``maybe_spawn_orchestration``) fires.
    _EXPECTED_STEPS = {
        "consume_commands",
        "concurrency_cap_read",
        "circuit_breaker_auto_close",
        "process_retry_markers",
        "reap_agent_tasks",
        "scan_queue",
        "scan_blocked",
        "active_agent_count",
        "maybe_spawn_orchestration",
    }

    def test_tick_event_carries_step_durations_dict(self, tmp_path: Path) -> None:
        """The end-of-tick INFO event has a ``step_durations`` dict
        covering every sub-step that ran (not just slow ones)."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()

        events = handler.events("scheduler_tick")
        assert len(events) == 1
        rec = events[0]
        step_durations = getattr(rec, "step_durations", None)
        assert isinstance(step_durations, dict), (
            f"expected step_durations dict on scheduler_tick event; "
            f"got {step_durations!r}"
        )
        # Every step that fired must appear. Sub-step list mirrors the
        # body of scheduler_tick — if a step is added/removed the
        # daemon code AND this set must change together.
        assert set(step_durations.keys()) == self._EXPECTED_STEPS

    def test_step_durations_values_are_rounded_to_3_decimals(
        self, tmp_path: Path
    ) -> None:
        """Per AC: values are rounded to 3 decimals so CloudWatch Logs
        Insights ``stats avg(step_durations.scan_queue)`` returns
        readable numbers, not 17-digit floats."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        rec = handler.events("scheduler_tick")[0]
        step_durations: dict[str, float] = getattr(rec, "step_durations")
        for step_name, elapsed in step_durations.items():
            assert isinstance(elapsed, (int, float)), (
                f"step_durations[{step_name!r}] is {type(elapsed).__name__}, "
                f"expected number"
            )
            assert elapsed >= 0.0, f"step_durations[{step_name!r}]={elapsed} < 0"
            # Round-trip check — value must be exactly equal to its
            # 3-decimal rounding (i.e. no extra precision leaked).
            assert elapsed == round(elapsed, 3), (
                f"step_durations[{step_name!r}]={elapsed!r} not rounded to 3 decimals"
            )

    def test_slow_step_value_in_step_durations_matches_warn_event(
        self, tmp_path: Path
    ) -> None:
        """A slow sub-step still emits the existing ``slow_<step>`` WARN
        AND its duration shows up in the tick-level ``step_durations``
        dict — the two paths are complementary, not exclusive."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]

        def _slow_scan() -> int:
            time.sleep(daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS + 0.1)
            return 0

        d._scan_queue_and_snapshot = _slow_scan  # type: ignore[method-assign]
        d.scheduler_tick()

        # The slow-WARN still fires.
        slow_events = handler.events("scheduler_tick_slow_scan_queue")
        assert len(slow_events) == 1

        # The tick-level event carries the same step name with a
        # duration ≥ the slow threshold.
        tick_events = handler.events("scheduler_tick")
        assert len(tick_events) == 1
        step_durations: dict[str, float] = getattr(tick_events[0], "step_durations")
        scan_elapsed = step_durations["scan_queue"]
        assert scan_elapsed >= daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS

    def test_step_durations_is_present_on_fast_tick(self, tmp_path: Path) -> None:
        """The whole point of #4109 — durations show up on EVERY tick,
        not just slow ones. This is the regression that would have caught
        the gap that motivated the issue."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d.scheduler_tick()
        rec = handler.events("scheduler_tick")[0]
        step_durations = getattr(rec, "step_durations", None)
        assert step_durations is not None
        # On a fast tick every value is well below the slow threshold.
        for step_name, elapsed in step_durations.items():
            assert elapsed < daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS, (
                f"fast tick should not produce slow {step_name}; got {elapsed}s"
            )

    def test_step_durations_is_in_summary_dict(self, tmp_path: Path) -> None:
        """The summary dict returned from ``scheduler_tick`` must also
        carry ``step_durations`` so callers (tests, future telemetry
        consumers) can read it without re-parsing log records."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        summary = d.scheduler_tick()
        assert "step_durations" in summary
        step_durations = summary["step_durations"]
        assert isinstance(step_durations, dict)
        assert set(step_durations.keys()) == self._EXPECTED_STEPS


# --------------------------------------------------------------------------
# AC #3 — _arm_periodic_faulthandler is idempotent and safe to re-enter.
#
# Called on every daemon boot. ECS can restart the task rapidly (rolling
# deploys, task-def churn) and a prior process's ``dump_traceback_later``
# timer may still be armed on the same file descriptor — we must not
# fail in that case. Also test the opt-out env var.
# --------------------------------------------------------------------------


class TestArmPeriodicFaulthandler:
    def test_arms_timer_by_default(self, monkeypatch: Any) -> None:
        monkeypatch.delenv(
            "JUDGEMIND_DISPATCHER_DISABLE_FAULTHANDLER_TIMER", raising=False
        )
        log = logging.getLogger("dispatcher.test.faulthandler.arm")
        handler = _CapturingLogHandler()
        log.handlers = [handler]
        log.propagate = False
        log.setLevel(logging.DEBUG)

        calls: list[tuple[int, dict[str, Any]]] = []

        def _fake_dump_later(interval: int, **kwargs: Any) -> None:
            calls.append((interval, kwargs))

        with (
            patch.object(daemon.faulthandler, "dump_traceback_later", _fake_dump_later),
            patch.object(daemon.faulthandler, "enable") as fake_enable,
        ):
            ok = daemon._arm_periodic_faulthandler(log, interval_seconds=123)

        assert ok is True
        assert fake_enable.called
        assert len(calls) == 1
        assert calls[0][0] == 123
        assert calls[0][1].get("repeat") is True
        assert calls[0][1].get("exit") is False
        events = handler.events("faulthandler_timer_armed")
        assert len(events) == 1

    def test_idempotent_repeat_calls(self, monkeypatch: Any) -> None:
        """Calling ``_arm_periodic_faulthandler`` twice in a row is
        a no-op on the second call (``dump_traceback_later`` simply
        replaces the prior timer) — it does NOT raise."""
        monkeypatch.delenv(
            "JUDGEMIND_DISPATCHER_DISABLE_FAULTHANDLER_TIMER", raising=False
        )
        log = logging.getLogger("dispatcher.test.faulthandler.idempotent")
        log.handlers = []
        log.propagate = False

        with (
            patch.object(daemon.faulthandler, "dump_traceback_later"),
            patch.object(daemon.faulthandler, "enable"),
        ):
            ok1 = daemon._arm_periodic_faulthandler(log)
            ok2 = daemon._arm_periodic_faulthandler(log)

        assert ok1 is True
        assert ok2 is True

    def test_opt_out_env_var_disables_timer(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("JUDGEMIND_DISPATCHER_DISABLE_FAULTHANDLER_TIMER", "1")
        log = logging.getLogger("dispatcher.test.faulthandler.optout")
        handler = _CapturingLogHandler()
        log.handlers = [handler]
        log.propagate = False
        log.setLevel(logging.DEBUG)

        calls: list[int] = []

        def _fake_dump_later(interval: int, **_kwargs: Any) -> None:
            calls.append(interval)

        with patch.object(
            daemon.faulthandler, "dump_traceback_later", _fake_dump_later
        ):
            ok = daemon._arm_periodic_faulthandler(log)

        assert ok is False
        assert calls == []
        events = handler.events("faulthandler_timer_disabled")
        assert len(events) == 1

    def test_swallows_faulthandler_exception(self, monkeypatch: Any) -> None:
        """A failure inside ``faulthandler.dump_traceback_later`` must
        NOT crash the daemon at boot. Log + swallow."""
        monkeypatch.delenv(
            "JUDGEMIND_DISPATCHER_DISABLE_FAULTHANDLER_TIMER", raising=False
        )
        log = logging.getLogger("dispatcher.test.faulthandler.swallow")
        handler = _CapturingLogHandler()
        log.handlers = [handler]
        log.propagate = False
        log.setLevel(logging.DEBUG)

        def _raising(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("faulthandler boom")

        with (
            patch.object(daemon.faulthandler, "dump_traceback_later", _raising),
            patch.object(daemon.faulthandler, "enable"),
        ):
            ok = daemon._arm_periodic_faulthandler(log)

        assert ok is False
        events = handler.events("faulthandler_timer_failed")
        assert len(events) == 1


# --------------------------------------------------------------------------
# AC #4 — module-level constants are sane defaults.
# --------------------------------------------------------------------------


class TestModuleConstants:
    def test_slow_step_threshold_is_below_watchdog_warn(self) -> None:
        """Per-step slow WARN fires BEFORE the 300s wedge watchdog so
        we pinpoint WHICH step is slow instead of just "no tick for
        300s". 5s << 300s honours that ordering."""
        assert daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS > 0.0
        assert (
            daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS
            < daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS
        )

    def test_faulthandler_interval_is_a_diagnostic_cadence(self) -> None:
        """The faulthandler periodic stderr dump is a fixed-interval
        observability tool — independent of the #3097 watchdog WARN
        threshold.

        Pre-#3344 the two were bound to the same value (300s). #3344
        tightened the watchdog WARN to 60s; coupling faulthandler to
        that new cadence would spray a full thread dump to stderr every
        60s, which is too noisy. Faulthandler stays at its independent
        cadence (300s — operator-tunable in the constants module if
        needed). The bound to enforce here is just "positive and not
        smaller than the EXIT threshold" — by the time EXIT fires the
        watchdog has already produced a dump, so faulthandler is the
        deeper-cadence safety net only.
        """
        assert daemon.FAULTHANDLER_DUMP_INTERVAL_SECONDS > 0
        assert (
            daemon.FAULTHANDLER_DUMP_INTERVAL_SECONDS
            >= daemon.DEFAULT_SCHEDULER_TICK_STALL_EXIT_SECONDS
        )
