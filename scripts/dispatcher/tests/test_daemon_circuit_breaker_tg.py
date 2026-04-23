"""Exit-code → event mapping for ``_send_circuit_breaker_telegram_alert``.

Issue #3061. The ``notify-telegram.sh`` helper now distinguishes
"sent", "usage error", "all sends failed", and three
"misconfigured — couldn't send" paths (secret fetch fail, empty
bot_token, empty user_ids) via exit codes 0/1/2/3/4/5. The daemon
caller must map those to distinct structured log events instead of
logging ``circuit_breaker_telegram_sent`` for every exit-0 return (the
pre-#3061 behavior that hid a ~4-week outage of real Telegram
delivery).

These tests exercise ``_send_circuit_breaker_telegram_alert`` with
``subprocess.run`` mocked to return each exit code in turn and assert
the correct event name + reason field fire. They also cover the
module-level helper ``_map_notify_telegram_exit_code`` in isolation so
future daemon callers (escalate, block_and_comment, …) can reuse the
mapping without duplicating the switch logic.

All external calls (subprocess, psycopg) are mocked — tests run in
milliseconds and do not depend on AWS or Telegram.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — same shape as test_daemon_circuit_breaker.py. Kept
# local so these tests don't import private fixtures from sibling files.
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
    """Make a daemon plus a placeholder notify-telegram.sh under ``tmp_path``.

    The send method short-circuits if the script path does not exist on
    disk, so we write a stub file (never executed — subprocess.run is
    mocked) inside ``tmp_path/scripts/``.
    """
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.cb_tg.{id(tmp_path)}")
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


def _invoke_alert_with_returncode(
    d: daemon.DispatcherDaemon,
    monkeypatch: Any,
    returncode: int,
    stderr: str = "",
) -> None:
    """Call ``_send_circuit_breaker_telegram_alert`` with subprocess mocked."""

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        r = MagicMock()
        r.returncode = returncode
        r.stdout = ""
        r.stderr = stderr
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    d._send_circuit_breaker_telegram_alert(
        bad_count=5,
        window_size=10,
        window_minutes=30,
        statuses=["failed", "crashed"],
    )


# --------------------------------------------------------------------------
# Module-level helper — pure function, no daemon state needed.
# --------------------------------------------------------------------------


class TestMapNotifyTelegramExitCode:
    """``_map_notify_telegram_exit_code`` maps each known exit code."""

    def test_exit_0_maps_to_sent_without_reason(self) -> None:
        event_suffix, reason = daemon._map_notify_telegram_exit_code(0)
        assert event_suffix == "sent"
        assert reason is None

    def test_exit_1_maps_to_usage_error(self) -> None:
        event_suffix, reason = daemon._map_notify_telegram_exit_code(1)
        assert event_suffix == "usage_error"
        assert reason is None

    def test_exit_2_maps_to_all_send_failed(self) -> None:
        event_suffix, reason = daemon._map_notify_telegram_exit_code(2)
        assert event_suffix == "all_send_failed"
        assert reason is None

    def test_exit_3_maps_to_config_missing_secret_fetch_failed(self) -> None:
        event_suffix, reason = daemon._map_notify_telegram_exit_code(3)
        assert event_suffix == "config_missing"
        assert reason == "secret_fetch_failed"

    def test_exit_4_maps_to_config_missing_empty_bot_token(self) -> None:
        event_suffix, reason = daemon._map_notify_telegram_exit_code(4)
        assert event_suffix == "config_missing"
        assert reason == "empty_bot_token"

    def test_exit_5_maps_to_config_missing_empty_user_ids(self) -> None:
        event_suffix, reason = daemon._map_notify_telegram_exit_code(5)
        assert event_suffix == "config_missing"
        assert reason == "empty_user_ids"

    def test_unknown_exit_code_falls_back_to_nonzero_exit(self) -> None:
        """An exit code we haven't mapped yet must fall through to the
        generic warning path — never silently treated as success."""
        event_suffix, reason = daemon._map_notify_telegram_exit_code(42)
        assert event_suffix == "nonzero_exit"
        assert reason is None

    def test_negative_exit_code_falls_back_to_nonzero_exit(self) -> None:
        """subprocess.run can return negative codes when the child was
        killed by a signal. Those must also not be misread as success."""
        event_suffix, reason = daemon._map_notify_telegram_exit_code(-9)
        assert event_suffix == "nonzero_exit"
        assert reason is None


# --------------------------------------------------------------------------
# End-to-end: ``_send_circuit_breaker_telegram_alert`` emits the right
# event for each returncode. These are the tests that would have caught
# the #3061 incident — exit codes 3/4/5 must NOT produce a "sent" event.
# --------------------------------------------------------------------------


class TestSendCircuitBreakerTelegramAlertEvents:
    def test_returncode_0_emits_sent_info(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Happy path: exit 0 → INFO event circuit_breaker_telegram_sent."""
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(d, monkeypatch, returncode=0)

        sent = handler.events("circuit_breaker_telegram_sent")
        assert len(sent) == 1
        record = sent[0]
        assert record.levelno == logging.INFO
        assert record.exit_code == 0  # type: ignore[attr-defined]
        # No config_missing event — the sent event is exclusive.
        assert handler.events("circuit_breaker_telegram_config_missing") == []
        assert handler.events("circuit_breaker_telegram_nonzero_exit") == []

    def test_returncode_1_emits_usage_error_warning(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(
            d, monkeypatch, returncode=1, stderr="Usage: notify-telegram.sh <msg>"
        )

        usage = handler.events("circuit_breaker_telegram_usage_error")
        assert len(usage) == 1
        assert usage[0].levelno == logging.WARNING
        assert usage[0].exit_code == 1  # type: ignore[attr-defined]
        # stderr tail carried into extras so the operator can diagnose.
        assert "Usage" in usage[0].stderr_tail  # type: ignore[attr-defined]
        # No false sent event.
        assert handler.events("circuit_breaker_telegram_sent") == []

    def test_returncode_2_emits_all_send_failed_warning(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Every recipient got a non-200 — explicit, distinct from
        the pre-#3061 generic ``_nonzero_exit``."""
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(
            d, monkeypatch, returncode=2, stderr="all 2 send(s) failed"
        )

        evts = handler.events("circuit_breaker_telegram_all_send_failed")
        assert len(evts) == 1
        assert evts[0].levelno == logging.WARNING
        assert evts[0].exit_code == 2  # type: ignore[attr-defined]
        assert handler.events("circuit_breaker_telegram_sent") == []

    def test_returncode_3_emits_config_missing_secret_fetch_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """#3061 primary regression: secret fetch fail must NOT log sent."""
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(
            d,
            monkeypatch,
            returncode=3,
            stderr="failed to fetch Telegram secret — Telegram notification NOT sent",
        )

        evts = handler.events("circuit_breaker_telegram_config_missing")
        assert len(evts) == 1
        record = evts[0]
        assert record.levelno == logging.WARNING
        assert record.exit_code == 3  # type: ignore[attr-defined]
        assert record.reason == "secret_fetch_failed"  # type: ignore[attr-defined]
        # Critical: the pre-#3061 false-success event must NOT fire.
        assert handler.events("circuit_breaker_telegram_sent") == []

    def test_returncode_4_emits_config_missing_empty_bot_token(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(
            d, monkeypatch, returncode=4, stderr="bot_token is empty in secret"
        )

        evts = handler.events("circuit_breaker_telegram_config_missing")
        assert len(evts) == 1
        assert evts[0].reason == "empty_bot_token"  # type: ignore[attr-defined]
        assert handler.events("circuit_breaker_telegram_sent") == []

    def test_returncode_5_emits_config_missing_empty_user_ids(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(
            d, monkeypatch, returncode=5, stderr="allowed_user_ids is empty in secret"
        )

        evts = handler.events("circuit_breaker_telegram_config_missing")
        assert len(evts) == 1
        assert evts[0].reason == "empty_user_ids"  # type: ignore[attr-defined]
        assert handler.events("circuit_breaker_telegram_sent") == []

    def test_unknown_returncode_emits_nonzero_exit_fallback(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A future/unmapped exit code falls back to the generic
        ``_nonzero_exit`` event — never ``_sent``."""
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(
            d, monkeypatch, returncode=99, stderr="unexpected"
        )

        evts = handler.events("circuit_breaker_telegram_nonzero_exit")
        assert len(evts) == 1
        assert evts[0].levelno == logging.WARNING
        assert evts[0].exit_code == 99  # type: ignore[attr-defined]
        assert handler.events("circuit_breaker_telegram_sent") == []
        assert handler.events("circuit_breaker_telegram_config_missing") == []


# --------------------------------------------------------------------------
# Regression: the sent-path must stay the happy path. This is the
# acceptance criterion #3 from the issue — backward-compat with the
# existing consumer (CloudWatch queries grepping for
# ``circuit_breaker_telegram_sent``).
# --------------------------------------------------------------------------


class TestHappyPathBackwardCompat:
    def test_returncode_0_event_name_matches_pre_3061(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The exit-0 event name is unchanged so existing CloudWatch
        queries / alarms keying off ``circuit_breaker_telegram_sent``
        continue to fire without modification."""
        d, _c, handler, _notify = _make_daemon_with_script(tmp_path)

        _invoke_alert_with_returncode(d, monkeypatch, returncode=0)

        sent = handler.events("circuit_breaker_telegram_sent")
        assert len(sent) == 1
        # The message prefix ("daemon.") + event name is what appears in
        # CloudWatch Logs Insights queries. Lock in the full string.
        assert sent[0].msg == "daemon.circuit_breaker_telegram_sent"
