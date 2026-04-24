"""Per-event Telegram notifications and digest for the dispatcher daemon.

Issue #2861. Tests cover:
  - Per-event enqueue + render on terminal transitions
  - Throttle: ≤1 message per 30s window (configurable)
  - Queue-full guard: overflow drops oldest and logs the event
  - Digest: interval boundary, first-run fallback, idempotency

All external calls (subprocess, psycopg) are mocked. Tests run in
milliseconds and do not depend on AWS or Telegram.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — mirrors test_daemon_circuit_breaker_tg.py
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


def _make_daemon_with_script(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler, Path]:
    """Make a daemon plus a placeholder notify-telegram.sh under ``tmp_path``."""
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.tg_notif.{id(tmp_path)}")
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
        baseline_repo_root=str(tmp_path),
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    notify = scripts_dir / "notify-telegram.sh"
    notify.write_text("#!/bin/sh\n")
    notify.chmod(0o755)

    return d, conn, handler, notify


def _fake_run_exit_0(cmd: list[str], **kwargs: Any) -> Any:
    """subprocess.run stub that always returns exit code 0."""
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


# --------------------------------------------------------------------------
# Per-event enqueue + message rendering
# --------------------------------------------------------------------------


class TestPerEventTelegram:
    """Terminal transitions enqueue messages with the correct emoji and content."""

    def _enqueue_and_drain(
        self,
        d: daemon.DispatcherDaemon,
        monkeypatch: Any,
        status: str,
        issue_number: int = 101,
        pr_number: int | None = None,
        issue_title: str = "Test issue title",
    ) -> list[list[str]]:
        """Enqueue a single message and drain once. Returns all subprocess.run calls."""
        calls: list[list[str]] = []

        def capturing_run(cmd: list[str], **kwargs: Any) -> Any:
            calls.append(cmd)
            return _fake_run_exit_0(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", capturing_run)

        # Seed the agent row fetch to return our test data.
        d._conn.cursor_instance.fetch_queue.append(  # type: ignore[union-attr]
            (issue_number, issue_title, None, None)
        )

        d._enqueue_per_event_telegram(
            status=status,
            agent_id="test-agent-id",
            issue_number=issue_number,
            pr_number=pr_number,
        )

        # Reset throttle so drain fires immediately.
        d._last_per_event_telegram_emit_monotonic = 0.0
        # Seed config fetch for throttle_seconds (returns 30).
        d._conn.cursor_instance.fetch_queue.append(("30",))  # type: ignore[union-attr]
        d._drain_per_event_telegram_queue()

        return calls

    def test_succeeded_emits_checkmark_template(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """succeeded → 🎯 emoji + PR # + issue number in message body."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        calls = self._enqueue_and_drain(
            d, monkeypatch, status="succeeded", issue_number=42, pr_number=123
        )

        assert len(calls) == 1
        # The message was written to a tmp file, not inlined in argv.
        assert "--message-file" in calls[0]
        msg_path = Path(calls[0][calls[0].index("--message-file") + 1])
        body = msg_path.read_text(encoding="utf-8")
        assert body[0] == "\U0001f3af", f"Expected 🎯 as first char, got: {body!r}"
        assert "PR #123 merged" in body
        assert "#42" in body

        # Structured log event for sent.
        sent = handler.events("per_event_telegram_sent")
        assert len(sent) == 1
        assert sent[0].levelno == logging.INFO

    def test_plan_blocked_emits_stop_template(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """plan_blocked → 🛑 first char in message body."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        calls = self._enqueue_and_drain(d, monkeypatch, status="plan_blocked")
        assert len(calls) == 1
        msg_path = Path(calls[0][calls[0].index("--message-file") + 1])
        body = msg_path.read_text(encoding="utf-8")
        assert body[0] == "\U0001f6d1", f"Expected 🛑 as first char, got: {body!r}"

    def test_needs_review_emits_warning_template(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """needs_review → ⚠️ prefix in message body."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        calls = self._enqueue_and_drain(d, monkeypatch, status="needs_review")
        assert len(calls) == 1
        msg_path = Path(calls[0][calls[0].index("--message-file") + 1])
        body = msg_path.read_text(encoding="utf-8")
        assert "\u26a0" in body, f"Expected ⚠️ in body, got: {body!r}"

    def test_failed_emits_x_template(self, monkeypatch: Any, tmp_path: Path) -> None:
        """failed → ❌ first char in message body."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        calls = self._enqueue_and_drain(d, monkeypatch, status="failed")
        assert len(calls) == 1
        msg_path = Path(calls[0][calls[0].index("--message-file") + 1])
        body = msg_path.read_text(encoding="utf-8")
        assert body[0] == "\u274c", f"Expected ❌ as first char, got: {body!r}"

    def test_crashed_does_not_enqueue(self, monkeypatch: Any, tmp_path: Path) -> None:
        """crashed is not in the notify set — no enqueue."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))

        d._enqueue_per_event_telegram(
            status="crashed",
            agent_id="test-agent-id",
        )
        assert len(d._per_event_telegram_queue) == 0
        d._last_per_event_telegram_emit_monotonic = 0.0
        d._conn.cursor_instance.fetch_queue.append(("30",))  # type: ignore[union-attr]
        d._drain_per_event_telegram_queue()
        assert calls == []


# --------------------------------------------------------------------------
# Throttle
# --------------------------------------------------------------------------


class TestThrottle:
    """Per-event Telegram queue is throttled ≤1 send per throttle window."""

    def test_burst_of_10_in_5_seconds_fires_one_then_queues_rest(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """10 enqueues, 10 drain calls across <30s → only 1 subprocess.run."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        subprocess_calls: list[list[str]] = []

        def capturing_run(cmd: list[str], **kwargs: Any) -> Any:
            subprocess_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", capturing_run)

        # Enqueue 10 messages directly (bypass DB fetch for speed).
        for i in range(10):
            d._per_event_telegram_queue.append(("succeeded", f"Msg {i}"))

        throttle_s = 30

        # Simulate draining over a 5-second span (less than 30s throttle).
        # Each call advances fake monotonic by 0.5s.
        base_mono = time.monotonic()
        for tick in range(10):
            fake_mono = base_mono + (tick * 0.5)
            monkeypatch.setattr(time, "monotonic", lambda m=fake_mono: m)
            # Seed config fetch.
            conn.cursor_instance.fetch_queue.append((str(throttle_s),))
            d._drain_per_event_telegram_queue()

        # Only the first drain (at tick=0) should have fired — subsequent
        # drains see fake_mono - last_emit < 30 and are no-ops.
        assert len(subprocess_calls) == 1, (
            f"Expected 1 subprocess.run in the first 30s window, "
            f"got {len(subprocess_calls)}"
        )
        # 9 messages remain queued.
        assert len(d._per_event_telegram_queue) == 9

        # Now advance past the throttle window for each remaining message.
        for idx in range(9):
            after_throttle = base_mono + throttle_s * (idx + 1) + 1
            monkeypatch.setattr(time, "monotonic", lambda m=after_throttle: m)
            conn.cursor_instance.fetch_queue.append((str(throttle_s),))
            d._drain_per_event_telegram_queue()

        assert len(subprocess_calls) == 10
        assert len(d._per_event_telegram_queue) == 0


# --------------------------------------------------------------------------
# Queue-full guard
# --------------------------------------------------------------------------


class TestQueueFull:
    """When the queue is full, enqueue drops oldest and logs the event."""

    def test_overflow_drops_oldest_and_logs_event(self, tmp_path: Path) -> None:
        """Enqueue MAX+1 messages — queue stays at MAX, one queue_full event fires."""
        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)
        cap = daemon.PER_EVENT_TELEGRAM_QUEUE_MAX

        # Fill the queue to the cap directly.
        for i in range(cap):
            d._per_event_telegram_queue.append(("succeeded", f"Msg {i}"))

        assert len(d._per_event_telegram_queue) == cap

        # Seed the agent row fetch for the enqueue call.
        conn.cursor_instance.fetch_queue.append((999, "51st issue", None, None))

        d._enqueue_per_event_telegram(
            status="succeeded",
            agent_id="overflow-agent-id",
            issue_number=999,
        )

        # The queue is bounded: deque(maxlen=cap) drops oldest, so size stays at cap.
        assert len(d._per_event_telegram_queue) == cap

        # The queue_full warning fired.
        full_events = handler.events("per_event_telegram_queue_full")
        assert len(full_events) == 1
        assert full_events[0].levelno == logging.WARNING
        assert full_events[0].cap == cap  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


class TestDigest:
    """_maybe_emit_digest emits the right Markdown shape at interval boundaries."""

    def test_emit_at_interval_boundary_produces_expected_markdown(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """After exactly digest_interval_seconds, the digest fires with all buckets."""
        import datetime as _dt

        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        subprocess_calls: list[list[str]] = []

        def capturing_run(cmd: list[str], **kwargs: Any) -> Any:
            subprocess_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", capturing_run)

        # Seed: last_digest_at is 4h ago (interval has elapsed).
        interval_s = 14400
        now = _dt.datetime(2026, 4, 24, 18, 0, 0, tzinfo=_dt.timezone.utc)
        last_digest = now - _dt.timedelta(seconds=interval_s + 1)
        d._last_digest_emit_at = last_digest

        # Seed dispatcher.config read for interval_s.
        conn.cursor_instance.fetch_queue.append((str(interval_s),))
        # Seed terminal_outcomes query → rows for each bucket.
        conn.cursor_instance.fetchall_queue.append(
            [
                ("succeeded", 5),
                ("needs_review", 2),
                ("plan_blocked", 1),
                ("failed", 3),
            ]
        )
        # Seed queue_depth read.
        conn.cursor_instance.fetch_queue.append((7,))
        # Seed last_digest_at write config (INSERT ... ON CONFLICT).
        # (no fetch needed for INSERT)

        monkeypatch.setattr(
            _dt,
            "datetime",
            type(
                "FakeDatetime",
                (_dt.datetime,),
                {
                    "now": staticmethod(lambda tz=None: now),
                    "fromisoformat": staticmethod(_dt.datetime.fromisoformat),
                    "fromtimestamp": staticmethod(_dt.datetime.fromtimestamp),
                },
            ),
        )

        from datetime import datetime

        monkeypatch.setattr(
            daemon,
            "datetime",
            type(
                "FakeDaemonDatetime",
                (datetime,),
                {
                    "now": staticmethod(lambda tz=None: now),
                    "fromisoformat": staticmethod(datetime.fromisoformat),
                    "fromtimestamp": staticmethod(datetime.fromtimestamp),
                },
            ),
        )

        d._maybe_emit_digest()

        assert len(subprocess_calls) == 1, "Expected exactly one digest send"
        msg_path = Path(
            subprocess_calls[0][subprocess_calls[0].index("--message-file") + 1]
        )
        body = msg_path.read_text(encoding="utf-8")

        # Header line check.
        assert "Dispatcher digest" in body
        assert "window: 4h" in body

        # All five bucket labels present.
        assert "Shipped:" in body
        assert "Needs review:" in body
        assert "Plan blocked:" in body
        assert "Failed:" in body
        assert "Queue depth:" in body

        # Correct counts.
        assert "Shipped: 5" in body
        assert "Needs review: 2" in body
        assert "Plan blocked: 1" in body
        assert "Failed: 3" in body
        assert "Queue depth: 7" in body

        # last_digest_at was updated.
        assert d._last_digest_emit_at == now

        sent = handler.events("digest_telegram_sent")
        assert len(sent) == 1


class TestDigestFirstRun:
    """When last_digest_at is None (first run), digest covers last interval window."""

    def test_no_last_digest_falls_back_to_interval_window(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """First run: _last_digest_emit_at is None; digest fires and persists new timestamp."""
        import datetime as _dt

        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        subprocess_calls: list[list[str]] = []

        def capturing_run(cmd: list[str], **kwargs: Any) -> Any:
            subprocess_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", capturing_run)

        # No last_digest_at set.
        assert d._last_digest_emit_at is None

        now = _dt.datetime(2026, 4, 24, 18, 0, 0, tzinfo=_dt.timezone.utc)

        from datetime import datetime

        monkeypatch.setattr(
            daemon,
            "datetime",
            type(
                "FakeDaemonDatetime",
                (datetime,),
                {
                    "now": staticmethod(lambda tz=None: now),
                    "fromisoformat": staticmethod(datetime.fromisoformat),
                    "fromtimestamp": staticmethod(datetime.fromtimestamp),
                },
            ),
        )

        # Seed config reads: interval_s, then last_digest_at = null.
        conn.cursor_instance.fetch_queue.append(
            ("14400",)
        )  # interval_s from _cb_config_int
        conn.cursor_instance.fetch_queue.append(
            ("null",)
        )  # last_digest_at from _cb_config_str
        # Seed terminal_outcomes query.
        conn.cursor_instance.fetchall_queue.append([("succeeded", 3)])
        # Seed queue_depth.
        conn.cursor_instance.fetch_queue.append((2,))

        d._maybe_emit_digest()

        # Should have fired.
        assert len(subprocess_calls) == 1
        # new last_digest_at persisted in memory.
        assert d._last_digest_emit_at == now


class TestDigestIdempotent:
    """A second call within the interval window is a no-op."""

    def test_second_call_within_interval_is_noop(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Two consecutive _maybe_emit_digest calls — second fires no subprocess."""
        import datetime as _dt

        d, conn, handler, _notify = _make_daemon_with_script(tmp_path)

        subprocess_calls: list[list[str]] = []

        def capturing_run(cmd: list[str], **kwargs: Any) -> Any:
            subprocess_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", capturing_run)

        now = _dt.datetime(2026, 4, 24, 18, 0, 0, tzinfo=_dt.timezone.utc)
        # last_digest was just 10 seconds ago — well within 4h interval.
        d._last_digest_emit_at = now - _dt.timedelta(seconds=10)

        from datetime import datetime

        monkeypatch.setattr(
            daemon,
            "datetime",
            type(
                "FakeDaemonDatetime",
                (datetime,),
                {
                    "now": staticmethod(lambda tz=None: now),
                    "fromisoformat": staticmethod(datetime.fromisoformat),
                    "fromtimestamp": staticmethod(datetime.fromtimestamp),
                },
            ),
        )

        # Seed interval_s read.
        conn.cursor_instance.fetch_queue.append(("14400",))

        d._maybe_emit_digest()

        # No subprocess.run — noop.
        assert subprocess_calls == []
        # No config write — last_digest_at unchanged.
        assert d._last_digest_emit_at == now - _dt.timedelta(seconds=10)
