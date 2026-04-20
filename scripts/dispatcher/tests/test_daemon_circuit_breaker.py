"""Unit tests for the overnight-safety circuit breaker in ``scripts.dispatcher.daemon``.

Issue #2860. Covers:

* Terminal outcomes are appended to ``dispatcher.terminal_outcomes`` by
  ``_mark_agent_terminal`` via ``_write_terminal_outcome``.
* ``_evaluate_circuit_breaker`` counts "bad" outcomes (anything not in
  :data:`daemon.OVERNIGHT_CB_GOOD_OUTCOME_STATUSES`) in the rolling
  window and flips ``concurrency_cap`` to 0 when the M-of-N threshold
  is met.
* On open: ``cap_flipped_by`` is set to ``"circuit_breaker"``, the
  ``daemon.circuit_breaker_opened`` event is logged, and
  ``scripts/notify-telegram.sh`` is invoked.
* Idempotence: re-running the check against an already-open breaker
  does not fire a second Telegram alert or bump the open counter.
* Auto-close: when the operator manually flips cap back to ≥1, the
  scheduler tick's auto-close path logs
  ``daemon.circuit_breaker_closed`` and clears ``cap_flipped_by``.
* Classifier: the good-outcome set currently contains only
  ``succeeded`` — ``failed``/``crashed``/``plan_blocked``/
  ``needs_review`` all count as bad.

All external calls — subprocess (``notify-telegram.sh``) and psycopg
— are mocked. The tests exercise the classifier + SQL scan logic
directly so they run in milliseconds regardless of DB / Telegram
availability.
"""

from __future__ import annotations

import json
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

# Provide a real-Exception psycopg stub for the daemon's lazy import.
# Other test files install a pure MagicMock stub whose
# ``errors.UniqueViolation`` is itself a Mock — that trips Python's
# "catching classes that do not inherit from BaseException" rule.


class _UniqueViolation(Exception):
    """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — mirror the pattern used in sibling test files so the
# fixtures compose the same way under pytest collection.
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


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    """Make a daemon with a fake connection. Matches the phase3a factory."""
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.circuit_breaker.{id(tmp_path)}")
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
    return d, conn, handler


# --------------------------------------------------------------------------
# Classifier — the good-outcome set is the single source of truth.
# --------------------------------------------------------------------------


class TestIsBadOutcome:
    """``_is_bad_outcome`` treats anything not in the good set as bad.

    This matches the tolerant-classifier contract from issue #2860:
    future correct-outcome terminals (``needs_review`` from #2856, or
    later) are conservatively counted as bad until the good-outcome
    set is updated — erring toward opening the breaker is safer for
    overnight operation.
    """

    def test_succeeded_is_good(self, tmp_path: Path) -> None:
        d, _c, _h = _make_daemon(tmp_path)
        assert d._is_bad_outcome("succeeded") is False

    def test_failed_is_bad(self, tmp_path: Path) -> None:
        d, _c, _h = _make_daemon(tmp_path)
        assert d._is_bad_outcome("failed") is True

    def test_crashed_is_bad(self, tmp_path: Path) -> None:
        d, _c, _h = _make_daemon(tmp_path)
        assert d._is_bad_outcome("crashed") is True

    def test_plan_blocked_is_bad(self, tmp_path: Path) -> None:
        """#2857's ``plan_blocked`` terminal counts as bad for the breaker."""
        d, _c, _h = _make_daemon(tmp_path)
        assert d._is_bad_outcome("plan_blocked") is True

    def test_needs_review_is_bad(self, tmp_path: Path) -> None:
        """#2856's ``needs_review`` terminal counts as bad for the breaker.

        Even if #2856 ships first and we update the good-outcome set
        later, today's tolerant classifier correctly treats it as bad.
        """
        d, _c, _h = _make_daemon(tmp_path)
        assert d._is_bad_outcome("needs_review") is True

    def test_future_unknown_terminal_is_bad(self, tmp_path: Path) -> None:
        """A hypothetical new terminal state defaults to "bad"."""
        d, _c, _h = _make_daemon(tmp_path)
        assert d._is_bad_outcome("ci_unrecoverable") is True
        assert d._is_bad_outcome("some_new_state") is True


# --------------------------------------------------------------------------
# Terminal-outcome write — appended on every terminal transition.
# --------------------------------------------------------------------------


class TestWriteTerminalOutcome:
    """``_write_terminal_outcome`` appends to ``dispatcher.terminal_outcomes``."""

    def test_writes_row_with_issue_number_lookup(self, tmp_path: Path) -> None:
        """Row carries the issue_number looked up from ``dispatcher.agents``."""
        d, conn, _h = _make_daemon(tmp_path)
        # First fetchone returns the agent's issue_number.
        conn.cursor_instance.fetch_queue = [(42,)]

        d._write_terminal_outcome("agent-42-uuid", "failed")

        # Last executed statement is the INSERT.
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.terminal_outcomes" in e[0]
        ]
        assert len(inserts) == 1
        params = inserts[0][1]
        assert params == ("agent-42-uuid", 42, "failed")
        assert conn.commits >= 1

    def test_writes_row_with_null_issue_number_when_agent_missing(
        self, tmp_path: Path
    ) -> None:
        """Fallback: agent row absent → issue_number is NULL. Still inserts."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]  # agent row absent

        d._write_terminal_outcome("orphan-agent", "succeeded")

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.terminal_outcomes" in e[0]
        ]
        assert len(inserts) == 1
        params = inserts[0][1]
        assert params == ("orphan-agent", None, "succeeded")


# --------------------------------------------------------------------------
# Config readers — overnight-CB knobs with safe defaults.
# --------------------------------------------------------------------------


class TestCBConfigReaders:
    """The ``_cb_config_int``/``_cb_enabled`` helpers fall back to safe defaults."""

    def test_enabled_defaults_to_true_when_row_missing(self, tmp_path: Path) -> None:
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._cb_enabled() is True

    def test_enabled_reads_bool_from_config(self, tmp_path: Path) -> None:
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(False,)]
        assert d._cb_enabled() is False

    def test_enabled_parses_json_string(self, tmp_path: Path) -> None:
        """Stored as JSONB → driver may hand us the string ``"false"``."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("false",)]
        assert d._cb_enabled() is False

    def test_enabled_defaults_to_true_on_malformed_json(self, tmp_path: Path) -> None:
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("not-json{",)]
        assert d._cb_enabled() is True

    def test_config_int_defaults_when_missing(self, tmp_path: Path) -> None:
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._cb_config_int("circuit_breaker_window_minutes", 30) == 30

    def test_config_int_reads_raw_int(self, tmp_path: Path) -> None:
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(10,)]
        assert d._cb_config_int("circuit_breaker_window_size", 10) == 10

    def test_config_int_parses_json_string(self, tmp_path: Path) -> None:
        """An integer stored as a JSON string ``"5"`` still parses."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("5",)]
        assert d._cb_config_int("circuit_breaker_bad_outcome_threshold", 999) == 5

    def test_config_int_negative_falls_back_to_default(self, tmp_path: Path) -> None:
        """Negative values are nonsense for count/window knobs."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(-7,)]
        assert d._cb_config_int("circuit_breaker_window_minutes", 30) == 30


# --------------------------------------------------------------------------
# Evaluation — the M-of-N threshold check.
# --------------------------------------------------------------------------


class TestEvaluateCircuitBreaker:
    """``_evaluate_circuit_breaker`` trips on 5-of-10 bad in 30 min."""

    def _stub_config_reads(
        self,
        conn: _FakeConnection,
        *,
        enabled: Any = True,
        window_minutes: Any = 30,
        window_size: Any = 10,
        threshold: Any = 5,
    ) -> None:
        """Queue up the four config SELECTs in order."""
        conn.cursor_instance.fetch_queue.extend(
            [
                (enabled,),
                (window_minutes,),
                (window_size,),
                (threshold,),
            ]
        )

    def test_trips_when_five_of_ten_are_bad(self, tmp_path: Path) -> None:
        """AC: 5 failures in 25 min → circuit opens, cap set to 0."""
        d, conn, handler = _make_daemon(tmp_path)
        self._stub_config_reads(conn)
        # Scan returns 5 failures + 5 successes. Rows are (status,).
        conn.cursor_instance.fetch_queue.append(None)  # will be rewritten below

        # Override the scan fetchall AFTER stubbing config so the ORDER
        # matches daemon.py: 4 fetchones (config), then 1 fetchall (scan),
        # then 1 fetchone (concurrency_cap read pre-flip).
        conn.cursor_instance.fetch_queue.pop()  # drop placeholder
        # Fifth item in the queue (after 4 config reads) is used by the
        # fetchall — but _FakeCursor uses fetchall_queue separately.
        conn.cursor_instance.fetchall_queue.append(
            [
                ("failed",),
                ("failed",),
                ("failed",),
                ("plan_blocked",),
                ("needs_review",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
            ]
        )
        # ``current_cap = self._cb_config_int("concurrency_cap", -1)``
        # runs inside the fifth config read. Add its row.
        conn.cursor_instance.fetch_queue.append((1,))  # cap was 1 before flip

        tripped = d._evaluate_circuit_breaker("agent-x")

        assert tripped is True
        # cap flip UPDATE fired.
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "concurrency_cap" in e[0]
            and e[1] == ("circuit_breaker",)
        ]
        assert len(cap_updates) == 1
        # cap_flipped_by UPDATE fired with JSON-encoded sentinel.
        flag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "cap_flipped_by" in e[0]
            and e[1] == ('"circuit_breaker"', "circuit_breaker")
        ]
        assert len(flag_updates) == 1
        # Structured log event.
        opened = handler.events("circuit_breaker_opened")
        assert len(opened) == 1
        assert opened[0].bad_count == 5  # type: ignore[attr-defined]
        assert opened[0].window_size == 10  # type: ignore[attr-defined]
        assert opened[0].threshold == 5  # type: ignore[attr-defined]

    def test_does_not_trip_when_four_of_ten_are_bad(self, tmp_path: Path) -> None:
        """AC: 4 fails, 6 successes → circuit stays closed."""
        d, conn, handler = _make_daemon(tmp_path)
        self._stub_config_reads(conn)
        conn.cursor_instance.fetchall_queue.append(
            [
                ("failed",),
                ("failed",),
                ("failed",),
                ("failed",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
            ]
        )

        tripped = d._evaluate_circuit_breaker("agent-x")

        assert tripped is False
        # No cap flip.
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "concurrency_cap" in e[0]
        ]
        assert cap_updates == []
        assert handler.events("circuit_breaker_opened") == []

    def test_happy_path_all_success_never_trips(self, tmp_path: Path) -> None:
        """Regression: all 10 recent successes → circuit stays closed."""
        d, conn, handler = _make_daemon(tmp_path)
        self._stub_config_reads(conn)
        conn.cursor_instance.fetchall_queue.append([("succeeded",)] * 10)

        tripped = d._evaluate_circuit_breaker("agent-x")

        assert tripped is False
        assert handler.events("circuit_breaker_opened") == []

    def test_disabled_breaker_never_trips(self, tmp_path: Path) -> None:
        """``circuit_breaker_enabled=false`` → short-circuit, no scan."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(False,)]  # enabled=false

        tripped = d._evaluate_circuit_breaker("agent-x")

        assert tripped is False
        # No SELECT against terminal_outcomes happened.
        scans = [
            e
            for e in conn.cursor_instance.executed
            if "dispatcher.terminal_outcomes" in e[0]
        ]
        assert scans == []
        assert handler.events("circuit_breaker_opened") == []

    def test_zero_threshold_treated_as_disabled(self, tmp_path: Path) -> None:
        """Defensive: operator setting threshold=0 doesn't trip on empty window."""
        d, conn, handler = _make_daemon(tmp_path)
        self._stub_config_reads(conn, threshold=0)
        # No scan should happen — the early-return catches threshold<=0.

        tripped = d._evaluate_circuit_breaker("agent-x")

        assert tripped is False
        assert handler.events("circuit_breaker_opened") == []


# --------------------------------------------------------------------------
# Idempotence — re-evaluation while already open does not fire a second
# Telegram alert. The open log is still written (so operators see the
# re-trip in CloudWatch) but ``was_already_open=True`` suppresses the
# alert side effect.
# --------------------------------------------------------------------------


class TestIdempotence:
    def test_already_open_does_not_fire_second_telegram_alert(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Second trip with cap already 0 → no duplicate Telegram send."""
        d, conn, handler = _make_daemon(tmp_path)
        # enabled, window_minutes, window_size, threshold, concurrency_cap,
        # (then fetchall for scan).
        conn.cursor_instance.fetch_queue = [
            (True,),
            (30,),
            (10,),
            (5,),
            (0,),  # current_cap already 0 → was_already_open=True
        ]
        conn.cursor_instance.fetchall_queue.append(
            [("failed",)] * 5 + [("succeeded",)] * 5
        )

        telegram_calls: list[Any] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            telegram_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        tripped = d._evaluate_circuit_breaker("agent-x")

        assert tripped is True
        # Open log still written (forensics want to see the re-trip).
        opened = handler.events("circuit_breaker_opened")
        assert len(opened) == 1
        assert opened[0].was_already_open is True  # type: ignore[attr-defined]
        # But NO Telegram alert — idempotence suppressed it.
        assert telegram_calls == []


# --------------------------------------------------------------------------
# Auto-close — operator re-flip clears the flag.
# --------------------------------------------------------------------------


class TestReadCapFlippedBy:
    """``_read_cap_flipped_by`` handles both psycopg3 and fake-cursor JSONB shapes.

    psycopg3 unwraps jsonb scalars to their Python equivalents: a JSONB
    string ``"circuit_breaker"`` (9 bytes in Postgres) becomes the
    Python string ``"circuit_breaker"`` (no quotes). Test fake cursors
    may pass the pre-decode shape ``'"circuit_breaker"'``. Both must
    resolve identically.
    """

    def test_returns_none_when_row_absent(self, tmp_path: Path) -> None:
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._read_cap_flipped_by() is None

    def test_returns_none_when_json_null(self, tmp_path: Path) -> None:
        """JSONB null → psycopg3 → Python None."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(None,)]
        assert d._read_cap_flipped_by() is None

    def test_returns_unwrapped_string_production_shape(self, tmp_path: Path) -> None:
        """psycopg3 production shape: JSONB string → Python string (no quotes)."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("circuit_breaker",)]
        assert d._read_cap_flipped_by() == "circuit_breaker"

    def test_returns_unwrapped_string_fake_cursor_shape(self, tmp_path: Path) -> None:
        """Test fixtures may pass JSON-encoded bytes — accepted equivalently."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [('"circuit_breaker"',)]
        assert d._read_cap_flipped_by() == "circuit_breaker"

    def test_returns_none_for_non_string_value(self, tmp_path: Path) -> None:
        """A JSON number / bool / null where we expected a string is a
        malformed row — return None."""
        d, conn, _h = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(42,)]
        assert d._read_cap_flipped_by() is None


class TestAutoClose:
    """``_check_circuit_breaker_auto_close`` clears the flag on operator re-flip."""

    def test_does_nothing_when_flag_is_null(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]  # cap_flipped_by = null

        closed = d._check_circuit_breaker_auto_close(current_cap=1)

        assert closed is False
        assert handler.events("circuit_breaker_closed") == []
        # No UPDATE.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []

    def test_does_nothing_when_flag_is_operator_string(self, tmp_path: Path) -> None:
        """Flag was set by something other than the breaker — don't touch it."""
        d, conn, _h = _make_daemon(tmp_path)
        # psycopg3 unwraps JSONB scalar strings to Python str in production.
        conn.cursor_instance.fetch_queue = [("operator",)]

        closed = d._check_circuit_breaker_auto_close(current_cap=1)

        assert closed is False

    def test_does_nothing_when_cap_still_zero(self, tmp_path: Path) -> None:
        """Breaker still open (cap=0) — no auto-close."""
        d, conn, handler = _make_daemon(tmp_path)
        # The helper short-circuits before reading cap_flipped_by if
        # current_cap < 1. No fetchones needed.

        closed = d._check_circuit_breaker_auto_close(current_cap=0)

        assert closed is False
        assert handler.events("circuit_breaker_closed") == []

    def test_closes_when_flag_is_circuit_breaker_and_cap_raised(
        self, tmp_path: Path
    ) -> None:
        """Operator re-flip: flag=circuit_breaker + cap=1 → log + clear flag."""
        d, conn, handler = _make_daemon(tmp_path)
        # psycopg3 unwraps JSONB scalar strings to Python str in production.
        conn.cursor_instance.fetch_queue = [("circuit_breaker",)]

        closed = d._check_circuit_breaker_auto_close(current_cap=1)

        assert closed is True
        # One UPDATE that sets value='null'.
        flag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "cap_flipped_by" in e[0]
        ]
        assert len(flag_updates) == 1
        closed_events = handler.events("circuit_breaker_closed")
        assert len(closed_events) == 1
        assert closed_events[0].new_cap == 1  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# End-to-end: the synthetic acceptance-criteria scenarios from #2860.
# Exercises the full ``_evaluate_circuit_breaker`` flow including the
# Telegram subprocess call via mocked ``subprocess.run``.
# --------------------------------------------------------------------------


class TestAcceptanceCriteria:
    """Map the issue's acceptance criteria to synthetic-data assertions."""

    def test_ac_five_failures_in_twenty_five_minutes_opens_circuit(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC: feed 5-of-10 bad → circuit opens, cap=0, Telegram fires.

        Single agent runs the helper; the scan fetchall returns the
        synthetic 10-row window. The cap-flip UPDATE is asserted
        directly against ``conn.executed`` so the test does not depend
        on the real DB.
        """
        d, conn, handler = _make_daemon(tmp_path)
        # Four config reads + current_cap read.
        conn.cursor_instance.fetch_queue = [
            (True,),  # enabled
            (30,),  # window_minutes
            (10,),  # window_size
            (5,),  # threshold
            (1,),  # current_cap before flip
        ]
        # 25 min window has 10 terminals: 5 bad + 5 good.
        conn.cursor_instance.fetchall_queue.append(
            [
                ("failed",),
                ("crashed",),
                ("failed",),
                ("plan_blocked",),
                ("needs_review",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
                ("succeeded",),
            ]
        )

        telegram_cmds: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            telegram_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Create the notify-telegram.sh placeholder file so the code
        # path that exists() the script returns True.
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "notify-telegram.sh").write_text("#!/bin/sh\n")

        tripped = d._evaluate_circuit_breaker("agent-y")

        assert tripped is True
        # Cap flipped to 0.
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "concurrency_cap" in e[0]
        ]
        assert cap_updates, "expected UPDATE ... WHERE key='concurrency_cap'"
        # cap_flipped_by='circuit_breaker'
        flag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "cap_flipped_by" in e[0]
        ]
        assert flag_updates, "expected UPDATE ... WHERE key='cap_flipped_by'"
        assert flag_updates[0][1] == (
            json.dumps("circuit_breaker"),
            "circuit_breaker",
        )
        # Telegram invoked.
        assert len(telegram_cmds) == 1
        assert "--message-file" in telegram_cmds[0]
        # Structured log.
        opened = handler.events("circuit_breaker_opened")
        assert len(opened) == 1
        assert opened[0].bad_count == 5  # type: ignore[attr-defined]

    def test_ac_mixed_sequence_does_not_open_circuit(self, tmp_path: Path) -> None:
        """AC: 4 fails, 6 successes in 30 min → circuit stays closed."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (True,),
            (30,),
            (10,),
            (5,),
        ]
        conn.cursor_instance.fetchall_queue.append(
            [("failed",)] * 4 + [("succeeded",)] * 6
        )

        tripped = d._evaluate_circuit_breaker("agent-y")

        assert tripped is False
        assert handler.events("circuit_breaker_opened") == []

    def test_ac_operator_reflip_closes_circuit(self, tmp_path: Path) -> None:
        """AC: operator raises cap=1 after open → flag cleared, close logged."""
        d, conn, handler = _make_daemon(tmp_path)
        # psycopg3 unwraps JSONB scalar strings to Python str in production.
        conn.cursor_instance.fetch_queue = [("circuit_breaker",)]

        closed = d._check_circuit_breaker_auto_close(current_cap=1)

        assert closed is True
        assert handler.events("circuit_breaker_closed") != []


# --------------------------------------------------------------------------
# Hook integration — ``_mark_agent_terminal`` calls both
# ``_write_terminal_outcome`` and ``_evaluate_circuit_breaker`` for
# terminal statuses. Non-terminal statuses skip both (the intermediate
# ``awaiting_ci`` / ``awaiting_deploy`` handoff states must not populate
# the circuit breaker's ring buffer).
# --------------------------------------------------------------------------


class TestMarkAgentTerminalHooks:
    def test_terminal_statuses_trigger_write_and_evaluate(self, tmp_path: Path) -> None:
        """succeeded / failed / crashed / plan_blocked → both helpers fire."""
        for status in ("succeeded", "failed", "crashed", "plan_blocked"):
            d, _conn, _h = _make_daemon(tmp_path)
            # Stub the inner helpers so we only test the wiring.
            write_calls: list[tuple[str, str]] = []
            eval_calls: list[str] = []

            def fake_write(agent_id: str, s: str, _c: list = write_calls) -> None:
                _c.append((agent_id, s))

            def fake_evaluate(agent_id: str, _c: list = eval_calls) -> bool:
                _c.append(agent_id)
                return False

            def fake_diag_outcome(*_args: Any, **_kwargs: Any) -> None:
                return None

            d._write_terminal_outcome = fake_write  # type: ignore[method-assign]
            d._evaluate_circuit_breaker = fake_evaluate  # type: ignore[method-assign]
            d._write_diagnosis_outcome_for_agent = fake_diag_outcome  # type: ignore[method-assign]

            d._mark_agent_terminal("agent-t", status, "some_phase")

            assert write_calls == [("agent-t", status)], (
                f"status={status!r}: expected one write call, got {write_calls!r}"
            )
            assert eval_calls == ["agent-t"], (
                f"status={status!r}: expected one eval call, got {eval_calls!r}"
            )

    def test_non_terminal_status_skips_write_and_evaluate(self, tmp_path: Path) -> None:
        """``running`` (the awaiting_ci/awaiting_deploy handoff) skips both."""
        d, _conn, _h = _make_daemon(tmp_path)
        write_calls: list[Any] = []
        eval_calls: list[Any] = []

        d._write_terminal_outcome = (  # type: ignore[method-assign]
            lambda *a, **k: write_calls.append((a, k))
        )
        d._evaluate_circuit_breaker = (  # type: ignore[method-assign]
            lambda *a, **k: eval_calls.append((a, k)) or False
        )
        d._write_diagnosis_outcome_for_agent = (  # type: ignore[method-assign]
            lambda *a, **k: None
        )

        d._mark_agent_terminal("agent-t", "running", "awaiting_ci")

        assert write_calls == []
        assert eval_calls == []

    def test_write_terminal_outcome_failure_does_not_stop_evaluate(
        self, tmp_path: Path
    ) -> None:
        """Independence: outcome-write exception does not prevent eval."""
        d, _conn, handler = _make_daemon(tmp_path)

        def raising_write(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated DB failure")

        eval_calls: list[str] = []

        def fake_evaluate(agent_id: str) -> bool:
            eval_calls.append(agent_id)
            return False

        d._write_terminal_outcome = raising_write  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = fake_evaluate  # type: ignore[method-assign]
        d._write_diagnosis_outcome_for_agent = (  # type: ignore[method-assign]
            lambda *a, **k: None
        )

        d._mark_agent_terminal("agent-t", "failed", "some_phase")

        # eval still ran.
        assert eval_calls == ["agent-t"]
        # and the error was logged.
        assert handler.events("terminal_outcome_write_failed") != []


# --------------------------------------------------------------------------
# Telegram message rendering — smoke-test the body content so the
# operator sees the useful fields.
# --------------------------------------------------------------------------


class TestTelegramMessage:
    def test_message_contains_bad_count_and_window(self, tmp_path: Path) -> None:
        d, _c, _h = _make_daemon(tmp_path)
        body = d._render_circuit_breaker_telegram_message(
            bad_count=6,
            window_size=10,
            window_minutes=30,
            statuses=["failed", "failed", "crashed"],
        )
        assert "OPENED" in body
        assert "6/10" in body
        assert "30 min" in body
        assert "concurrency_cap has been set to 0" in body
        assert "failed, failed, crashed" in body

    def test_message_handles_empty_status_list(self, tmp_path: Path) -> None:
        d, _c, _h = _make_daemon(tmp_path)
        body = d._render_circuit_breaker_telegram_message(
            bad_count=5,
            window_size=10,
            window_minutes=30,
            statuses=[],
        )
        assert "(none)" in body


# --------------------------------------------------------------------------
# Issue #2921: the candidate SELECT must JOIN dispatcher.agents and
# filter ``kind='task'`` so the supervisor's stuck_timeout sweeps on
# ``kind='task-skill'`` rows (operator-spawned /task subagents whose
# phase never advances past ``claiming``) do not trip the breaker.
#
# Before the fix: 5 task-skill stuck_timeout sweeps in 30 min → breaker
# opens, cap=0, daemon paused, running pipeline killed by the
# killswitch. Observed 2026-04-20 16:48:26 UTC with
# ``recent_statuses=['crashed','crashed','crashed','crashed','crashed']``
# all sourced from task-skill rows.
#
# The DB-level filter is the canonical gate: ``_FakeCursor`` in these
# tests doesn't enforce the JOIN semantics (it returns whatever rows
# the test queued), so we assert the filter by inspecting the executed
# SQL string. An integration test against a real Postgres would
# additionally exercise the JOIN semantics end-to-end; those live in
# the dispatcher-integration-tests job and are out of scope here.
# --------------------------------------------------------------------------


class TestCircuitBreakerKindFilter:
    """Issue #2921 — SELECT must filter to ``kind='task'`` rows."""

    def _stub_config_reads(
        self,
        conn: _FakeConnection,
        *,
        enabled: Any = True,
        window_minutes: Any = 30,
        window_size: Any = 10,
        threshold: Any = 5,
    ) -> None:
        conn.cursor_instance.fetch_queue.extend(
            [
                (enabled,),
                (window_minutes,),
                (window_size,),
                (threshold,),
            ]
        )

    def test_scan_query_joins_dispatcher_agents_on_kind_task(
        self, tmp_path: Path
    ) -> None:
        """The candidate SELECT must JOIN ``dispatcher.agents`` and filter ``kind='task'``.

        Regression for #2921: the pre-fix query read from
        ``dispatcher.terminal_outcomes`` alone, with no ``kind``
        filter. That let supervisor stuck_timeout sweeps on
        ``kind='task-skill'`` rows count toward the breaker. The
        post-fix query joins against ``dispatcher.agents`` on
        ``agent_id`` and filters ``a.kind = 'task'`` so only
        daemon-owned outcomes influence the breaker.
        """
        d, conn, _h = _make_daemon(tmp_path)
        self._stub_config_reads(conn)
        # Empty scan result — we only care about the SQL string here.
        conn.cursor_instance.fetchall_queue.append([])

        d._evaluate_circuit_breaker("agent-x")

        scans = [
            e
            for e in conn.cursor_instance.executed
            if "dispatcher.terminal_outcomes" in e[0]
        ]
        assert len(scans) == 1, (
            f"expected exactly one scan SELECT, got {len(scans)}: {scans!r}"
        )
        sql = scans[0][0]
        # The JOIN on dispatcher.agents is the structural filter.
        assert "JOIN dispatcher.agents" in sql, (
            f"scan SQL missing JOIN on dispatcher.agents: {sql!r}"
        )
        # And the kind='task' predicate is the filter itself.
        assert "a.kind = 'task'" in sql, (
            f"scan SQL missing kind='task' predicate: {sql!r}"
        )

    def test_five_task_skill_crashed_rows_do_not_trip_breaker(
        self, tmp_path: Path
    ) -> None:
        """AC: 5 ``kind='task-skill'`` crashed rows in 30 min → breaker stays closed.

        The DB-level JOIN filter excludes task-skill rows before they
        reach Python, so the scan's ``fetchall`` returns zero rows
        when the only terminal outcomes in the window are task-skill
        stuck_timeout sweeps. With zero bad rows observed, the
        breaker cannot trip regardless of threshold. This mirrors
        the 2026-04-20 16:48:26 UTC incident's real-world shape
        (``recent_statuses=['crashed','crashed','crashed','crashed','crashed']``
        all from task-skill rows) — with the fix the scan returns
        ``[]`` and the breaker stays closed.
        """
        d, conn, handler = _make_daemon(tmp_path)
        self._stub_config_reads(conn)
        # Post-fix the JOIN excludes task-skill rows at the DB layer,
        # so the scan sees an empty window for this workload.
        conn.cursor_instance.fetchall_queue.append([])

        tripped = d._evaluate_circuit_breaker("daemon-task-agent")

        assert tripped is False, (
            "breaker tripped on an empty scan result — the kind='task' "
            "filter is the only thing keeping task-skill stuck_timeouts "
            "from polluting the breaker signal"
        )
        # No cap flip UPDATE fired.
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "concurrency_cap" in e[0]
        ]
        assert cap_updates == []
        assert handler.events("circuit_breaker_opened") == []

    def test_five_task_crashed_rows_still_trip_breaker(self, tmp_path: Path) -> None:
        """Regression: 5 genuine ``kind='task'`` crashed rows still trip.

        Mirror of the AC scenario but with daemon-owned crashed
        rows — the breaker must still fire as before. The fake
        cursor represents the post-JOIN result set (task-kind rows
        only), so the five ``crashed`` entries here stand for five
        daemon-pipeline crashes that survived the kind filter. The
        breaker must open and flip cap.
        """
        d, conn, handler = _make_daemon(tmp_path)
        self._stub_config_reads(conn)
        conn.cursor_instance.fetchall_queue.append([("crashed",)] * 5)
        # ``current_cap`` read after the scan / threshold check.
        conn.cursor_instance.fetch_queue.append((1,))

        tripped = d._evaluate_circuit_breaker("daemon-task-agent")

        assert tripped is True
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "concurrency_cap" in e[0]
            and e[1] == ("circuit_breaker",)
        ]
        assert len(cap_updates) == 1
        opened = handler.events("circuit_breaker_opened")
        assert len(opened) == 1
        assert opened[0].bad_count == 5  # type: ignore[attr-defined]
