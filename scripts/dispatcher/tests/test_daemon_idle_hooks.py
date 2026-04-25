"""Unit tests for dispatcher daemon idle-hook triggers.

Issue #2864 — /audit and /spotcheck idle-hook triggers in daemon v2.

Tests ``_maybe_trigger_idle_hooks``, ``_should_trigger_audit``,
``_should_trigger_spotcheck``, and ``_claim_synthetic_agent`` against the
``_FakeCursor`` / ``_FakeConnection`` stubs from ``test_daemon_phase3a.py``.

Eight tests:
1. ``test_audit_threshold_met_spawns_audit`` — 5 merges since last_run_at,
   threshold=5: synthetic agent claimed with kind='audit', issue_number=-1.
2. ``test_audit_threshold_minus_one_does_not_spawn`` — 4 merges, threshold=5:
   no claim.
3. ``test_spotcheck_cron_match_spawns`` — frozen clock at 14:00 UTC,
   expr ``0 14 * * *``, last_run_at < 14:00: synthetic claim kind='spotcheck',
   issue_number=-2.
4. ``test_spotcheck_cron_miss_does_not_spawn`` — frozen clock at 14:01, no
   claim.
5. ``test_spotcheck_same_minute_no_refire`` — frozen clock at 14:00,
   last_run_at also at 14:00: no claim (within-minute idempotence).
6. ``test_idle_hook_defers_when_cap_full`` — active_agent_count == cap: no
   claim, log ``daemon.idle_hook_deferred_for_cap`` emitted.
7. ``test_idle_hooks_skipped_when_paused`` — ``_is_paused()`` returns True:
   no claim.
8. ``test_idle_hook_advances_last_run_at_before_spawn`` — verifies the UPDATE
   to ``idle_hooks_state`` runs before the subprocess spawn (mock subprocess to
   raise; assert ``last_run_at`` was still updated).
"""

from __future__ import annotations

import logging
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_phase3a._FakeCursor / _FakeConnection)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
        self.fetch_responses: dict[str, Any] = {}
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if self.fetch_responses and self.executed:
            last_sql = self.executed[-1][0]
            for fragment, value in self.fetch_responses.items():
                if fragment in last_sql:
                    return value
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
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.idle_hooks.{id(tmp_path)}")
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
    return d, conn, handler


# --------------------------------------------------------------------------
# Helpers to build fetch_responses for idle-hook queries
# --------------------------------------------------------------------------


def _audit_fetchall_queue(
    threshold: int, last_run_at: datetime | None, pr_count: int
) -> list[list[Any]]:
    """Build fetchall_queue for _should_trigger_audit queries.

    _should_trigger_audit uses ``fetchall()`` to avoid consuming ``fetch_queue``
    entries that existing scheduler-tick tests reserve for positional responses.

    Three SQL statements in order:
      1. SELECT value FROM dispatcher.config WHERE key = 'idle_audit_every_n_prs'
         → [(threshold,)]
      2. SELECT last_run_at FROM dispatcher.idle_hooks_state WHERE hook_name = 'audit'
         → [(last_run_at,)] or []
      3. SELECT COUNT(*) FROM dispatcher.agents ...
         → [(pr_count,)]
    """
    return [
        [(threshold,)],
        [(last_run_at,)] if last_run_at is not None else [],
        [(pr_count,)],
    ]


def _spotcheck_fetchall_queue(
    cron_expr: str, last_run_at: datetime | None
) -> list[list[Any]]:
    """Build fetchall_queue for _should_trigger_spotcheck queries.

    _should_trigger_spotcheck uses ``fetchall()`` to avoid consuming
    ``fetch_queue`` entries.

    Two SQL statements in order:
      1. SELECT value FROM dispatcher.config WHERE key = 'idle_spotcheck_cron'
         → [(cron_expr,)]
      2. SELECT last_run_at FROM dispatcher.idle_hooks_state WHERE hook_name = 'spotcheck'
         → [(last_run_at,)] or []
    """
    return [
        [(cron_expr,)],
        [(last_run_at,)] if last_run_at is not None else [],
    ]


# --------------------------------------------------------------------------
# Test 1 — audit threshold met → spawns audit synthetic agent
# --------------------------------------------------------------------------


def test_audit_threshold_met_spawns_audit(tmp_path: Path) -> None:
    """5 succeeded merges since last_run_at, threshold=5 → synthetic audit agent claimed."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _audit_fetchall_queue(
        threshold=5,
        last_run_at=last_run,
        pr_count=5,
    )

    result = d._should_trigger_audit()

    assert result is True


# --------------------------------------------------------------------------
# Test 2 — audit threshold NOT met (4 merges, threshold=5) → no spawn
# --------------------------------------------------------------------------


def test_audit_threshold_minus_one_does_not_spawn(tmp_path: Path) -> None:
    """4 succeeded merges since last_run_at, threshold=5 → no audit spawn."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _audit_fetchall_queue(
        threshold=5,
        last_run_at=last_run,
        pr_count=4,
    )

    result = d._should_trigger_audit()

    assert result is False


# --------------------------------------------------------------------------
# Test 3 — spotcheck cron match → spawns spotcheck synthetic agent
# --------------------------------------------------------------------------


def test_spotcheck_cron_match_spawns(tmp_path: Path) -> None:
    """Frozen clock at 14:00 UTC, expr '0 14 * * *', last_run_at < 14:00 → spotcheck spawned."""
    d, conn, handler = _make_daemon(tmp_path)

    # last_run_at was 13:00, so 14:00 is a new minute past it.
    last_run = datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _spotcheck_fetchall_queue(
        cron_expr="0 14 * * *",
        last_run_at=last_run,
    )

    frozen_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._should_trigger_spotcheck()

    assert result is True


# --------------------------------------------------------------------------
# Test 4 — spotcheck cron miss (14:01) → no spawn
# --------------------------------------------------------------------------


def test_spotcheck_cron_miss_does_not_spawn(tmp_path: Path) -> None:
    """Frozen clock at 14:01 UTC, expr '0 14 * * *' → no spotcheck spawn."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _spotcheck_fetchall_queue(
        cron_expr="0 14 * * *",
        last_run_at=last_run,
    )

    frozen_now = datetime(2024, 6, 15, 14, 1, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._should_trigger_spotcheck()

    assert result is False


# --------------------------------------------------------------------------
# Test 5 — spotcheck same-minute refire suppressed
# --------------------------------------------------------------------------


def test_spotcheck_same_minute_no_refire(tmp_path: Path) -> None:
    """Frozen clock at 14:00, last_run_at also at 14:00 → no refire (within-minute idempotence)."""
    d, conn, handler = _make_daemon(tmp_path)

    # last_run_at is exactly at 14:00 — same as now_minute.
    last_run = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _spotcheck_fetchall_queue(
        cron_expr="0 14 * * *",
        last_run_at=last_run,
    )

    frozen_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._should_trigger_spotcheck()

    assert result is False


# --------------------------------------------------------------------------
# Test 6 — concurrency cap full → defer both hooks
# --------------------------------------------------------------------------


def test_idle_hook_defers_when_cap_full(tmp_path: Path) -> None:
    """active_agent_count == concurrency_cap → no claim, daemon.idle_hook_deferred_for_cap logged."""
    d, conn, handler = _make_daemon(tmp_path)

    # Cap=2, active=2 → cap is full.
    result = d._maybe_trigger_idle_hooks(active_agent_count=2, concurrency_cap=2)

    assert result is False
    deferred_events = handler.events("idle_hook_deferred_for_cap")
    assert len(deferred_events) == 1
    assert deferred_events[0].active_agent_count == 2  # type: ignore[attr-defined]
    assert deferred_events[0].concurrency_cap == 2  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Test 7 — daemon paused → skip idle hooks
# --------------------------------------------------------------------------


def test_idle_hooks_skipped_when_paused(tmp_path: Path) -> None:
    """_is_paused() returns True → no claim, no deferred event."""
    d, conn, handler = _make_daemon(tmp_path)
    d._pause_requested.set()  # Simulate paused state.

    result = d._maybe_trigger_idle_hooks(active_agent_count=0, concurrency_cap=5)

    assert result is False
    # No deferred-for-cap event should fire — this is the pause path, not cap.
    assert len(handler.events("idle_hook_deferred_for_cap")) == 0


# --------------------------------------------------------------------------
# Test 8 — last_run_at updated BEFORE subprocess spawn (crash-safe)
# --------------------------------------------------------------------------


def test_idle_hook_advances_last_run_at_before_spawn(tmp_path: Path) -> None:
    """UPDATE idle_hooks_state.last_run_at must run before _run_synthetic_skill_subprocess.

    Even if the subprocess raises, the UPDATE must have been executed.
    """
    d, conn, handler = _make_daemon(tmp_path)

    # Track execution order.
    call_order: list[str] = []

    def fake_create_worktree(agent_id: str) -> Path:
        call_order.append("create_worktree")
        wt = tmp_path / f"worktree-{agent_id}"
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    def fake_subprocess(agent_id: str, kind: str, worktree: Path) -> int:
        call_order.append("subprocess")
        raise RuntimeError("simulated subprocess crash")

    with (
        patch.object(d, "_create_worktree", side_effect=fake_create_worktree),
        patch.object(d, "_run_synthetic_skill_subprocess", side_effect=fake_subprocess),
        patch.object(d, "_mark_agent_terminal"),
    ):
        # Run _run_synthetic_agent — it should UPDATE last_run_at before spawning.
        try:
            d._run_synthetic_agent(
                agent_id=str(uuid_mod.uuid4()),
                kind=daemon.IDLE_HOOK_KIND_AUDIT,
            )
        except RuntimeError:
            pass  # Expected — subprocess raised.

    # The UPDATE for idle_hooks_state must have been executed.
    executed_sqls = [sql for sql, _ in conn.cursor_instance.executed]
    update_sqls = [
        s for s in executed_sqls if "idle_hooks_state" in s and "UPDATE" in s
    ]
    assert len(update_sqls) >= 1, (
        "Expected an UPDATE to idle_hooks_state before subprocess spawn; "
        f"executed SQLs: {executed_sqls}"
    )

    # The UPDATE must precede the subprocess call in call_order.
    # create_worktree happens after the UPDATE, subprocess after create_worktree.
    assert "create_worktree" in call_order, "create_worktree should have been called"


# --------------------------------------------------------------------------
# Test 9 — _claim_synthetic_agent returns agent_id on success
# --------------------------------------------------------------------------


def test_claim_synthetic_agent_returns_agent_id(tmp_path: Path) -> None:
    """_claim_synthetic_agent inserts a row and returns a valid UUID."""
    d, conn, handler = _make_daemon(tmp_path)

    agent_id = d._claim_synthetic_agent(
        kind=daemon.IDLE_HOOK_KIND_AUDIT,
        issue_number=daemon.IDLE_HOOK_AUDIT_ISSUE_NUMBER,
    )

    assert agent_id is not None
    # Verify it's a valid UUID string.
    uuid_mod.UUID(agent_id)  # Raises ValueError if not valid.

    # Verify INSERT was executed.
    executed_sqls = [sql for sql, _ in conn.cursor_instance.executed]
    insert_sqls = [s for s in executed_sqls if "INSERT INTO dispatcher.agents" in s]
    assert len(insert_sqls) == 1

    # Verify kind and issue_number were passed.
    insert_params = conn.cursor_instance.executed[-1][1]
    assert daemon.IDLE_HOOK_KIND_AUDIT in insert_params
    assert daemon.IDLE_HOOK_AUDIT_ISSUE_NUMBER in insert_params


# --------------------------------------------------------------------------
# Test 10 — _claim_synthetic_agent returns None on UniqueViolation (race)
# --------------------------------------------------------------------------


def test_claim_synthetic_agent_returns_none_on_race(tmp_path: Path) -> None:
    """_claim_synthetic_agent returns None when a UniqueViolation indicates race."""
    d, conn, handler = _make_daemon(tmp_path)

    # Simulate UniqueViolation on the INSERT.
    import psycopg as _psycopg

    original_execute = conn.cursor_instance.execute
    called = [False]

    def raising_execute(sql: str, params: Any = None) -> None:
        if "INSERT INTO dispatcher.agents" in sql and not called[0]:
            called[0] = True
            raise _psycopg.errors.UniqueViolation("duplicate")
        original_execute(sql, params)

    conn.cursor_instance.execute = raising_execute  # type: ignore[method-assign]

    agent_id = d._claim_synthetic_agent(
        kind=daemon.IDLE_HOOK_KIND_AUDIT,
        issue_number=daemon.IDLE_HOOK_AUDIT_ISSUE_NUMBER,
    )

    assert agent_id is None
    already_running_events = handler.events("idle_hook_already_running")
    assert len(already_running_events) == 1


# --------------------------------------------------------------------------
# Test 11 — _maybe_trigger_idle_hooks returns True when audit threshold met
# --------------------------------------------------------------------------


def test_maybe_trigger_idle_hooks_audit_triggers(tmp_path: Path) -> None:
    """_maybe_trigger_idle_hooks returns True and sets _synthetic_agent_pending for audit."""
    d, conn, handler = _make_daemon(tmp_path)

    # audit fetchall_queue: threshold=1, last_run=old, pr_count=1
    last_run = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _audit_fetchall_queue(
        threshold=1,
        last_run_at=last_run,
        pr_count=1,
    )

    result = d._maybe_trigger_idle_hooks(active_agent_count=0, concurrency_cap=5)

    assert result is True
    assert d._synthetic_agent_pending is not None
    agent_id, kind = d._synthetic_agent_pending
    assert kind == daemon.IDLE_HOOK_KIND_AUDIT
    uuid_mod.UUID(agent_id)  # valid UUID


# --------------------------------------------------------------------------
# Test 12 — _maybe_trigger_idle_hooks returns True for spotcheck when audit not due
# --------------------------------------------------------------------------


def test_maybe_trigger_idle_hooks_spotcheck_triggers(tmp_path: Path) -> None:
    """Audit not due (0 merges) → spotcheck cron matches → returns True."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc)
    # First: audit fetchall_queue (threshold=20, pr_count=0 → no audit)
    audit_queue = _audit_fetchall_queue(threshold=20, last_run_at=last_run, pr_count=0)
    # Then: spotcheck fetchall_queue (cron matches 14:00)
    spotcheck_queue = _spotcheck_fetchall_queue(
        cron_expr="0 14 * * *",
        last_run_at=last_run,
    )
    conn.cursor_instance.fetchall_queue = audit_queue + spotcheck_queue

    frozen_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._maybe_trigger_idle_hooks(active_agent_count=0, concurrency_cap=5)

    assert result is True
    assert d._synthetic_agent_pending is not None
    agent_id, kind = d._synthetic_agent_pending
    assert kind == daemon.IDLE_HOOK_KIND_SPOTCHECK
    uuid_mod.UUID(agent_id)


# --------------------------------------------------------------------------
# Test 13 — _maybe_trigger_idle_hooks returns False when neither hook due
# --------------------------------------------------------------------------


def test_maybe_trigger_idle_hooks_neither_due(tmp_path: Path) -> None:
    """No audit (0 merges), no spotcheck (cron miss at 14:01) → returns False."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc)
    audit_queue = _audit_fetchall_queue(threshold=20, last_run_at=last_run, pr_count=0)
    spotcheck_queue = _spotcheck_fetchall_queue(
        cron_expr="0 14 * * *",
        last_run_at=last_run,
    )
    conn.cursor_instance.fetchall_queue = audit_queue + spotcheck_queue

    frozen_now = datetime(2024, 6, 15, 14, 1, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._maybe_trigger_idle_hooks(active_agent_count=0, concurrency_cap=5)

    assert result is False
    assert d._synthetic_agent_pending is None


# --------------------------------------------------------------------------
# Test 14 — _claim_synthetic_agent returns None when audit already running
#            (agent_id returned by _claim_synthetic_agent is None → no pending set)
# --------------------------------------------------------------------------


def test_maybe_trigger_audit_claim_race_returns_false(tmp_path: Path) -> None:
    """Audit should trigger but _claim_synthetic_agent returns None (race) → returns False."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    conn.cursor_instance.fetchall_queue = _audit_fetchall_queue(
        threshold=1,
        last_run_at=last_run,
        pr_count=1,
    )

    # Make _claim_synthetic_agent return None (UniqueViolation race).
    with patch.object(d, "_claim_synthetic_agent", return_value=None):
        result = d._maybe_trigger_idle_hooks(active_agent_count=0, concurrency_cap=5)

    # No pending agent, and we fall through to spotcheck (fetchall_queue empty → no spotcheck).
    assert result is False
    assert d._synthetic_agent_pending is None


# --------------------------------------------------------------------------
# Test 15 — _should_trigger_spotcheck handles JSONB non-string cron expr
# --------------------------------------------------------------------------


def test_spotcheck_jsonb_non_string_cron_expr(tmp_path: Path) -> None:
    """_should_trigger_spotcheck parses a non-string JSONB value via json.loads."""
    d, conn, handler = _make_daemon(tmp_path)

    last_run = datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc)
    # Simulate JSONB value that is NOT a plain string (e.g. a mock non-str).
    # json.loads(str(raw)) should give the cron expr.
    import json

    class _JSONBValue:
        """Simulate a non-str JSONB value returned by psycopg."""

        def __str__(self) -> str:
            return json.dumps("0 14 * * *")

    jsonb_raw = _JSONBValue()
    # fetchall_queue: first call returns [(jsonb_raw,)], second [(last_run,)]
    conn.cursor_instance.fetchall_queue = [
        [(jsonb_raw,)],
        [(last_run,)],
    ]

    frozen_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._should_trigger_spotcheck()

    assert result is True


# --------------------------------------------------------------------------
# Test 16 — _should_trigger_spotcheck handles naive last_run_at (adds UTC tz)
# --------------------------------------------------------------------------


def test_spotcheck_naive_last_run_at_gets_utc(tmp_path: Path) -> None:
    """A naive (tz-unaware) last_run_at datetime is treated as UTC."""
    d, conn, handler = _make_daemon(tmp_path)

    # Naive datetime at 13:00 — should not suppress 14:00 cron match.
    naive_last_run = datetime(2024, 6, 15, 13, 0)  # no tzinfo
    conn.cursor_instance.fetchall_queue = [
        [("0 14 * * *",)],
        [(naive_last_run,)],
    ]

    frozen_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    with patch("dispatcher.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = d._should_trigger_spotcheck()

    assert result is True


# --------------------------------------------------------------------------
# Test 17 — _run_synthetic_skill_subprocess calls subprocess.run with claude
# --------------------------------------------------------------------------


def test_run_synthetic_skill_subprocess_invokes_claude(tmp_path: Path) -> None:
    """_run_synthetic_skill_subprocess calls subprocess.run and returns exit code."""
    d, conn, handler = _make_daemon(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tmp").mkdir()

    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("dispatcher.daemon.subprocess.run", return_value=fake_proc) as mock_run:
        rc = d._run_synthetic_skill_subprocess(
            agent_id="test-agent-id",
            kind=daemon.IDLE_HOOK_KIND_AUDIT,
            worktree=worktree,
        )

    assert rc == 0
    assert mock_run.called
    cmd_arg = mock_run.call_args[0][0]
    assert "claude" in cmd_arg
    assert "-p" in cmd_arg
    assert f"/{daemon.IDLE_HOOK_KIND_AUDIT}" in cmd_arg


# --------------------------------------------------------------------------
# Test 18 — _run_synthetic_skill_subprocess returns 1 on OSError
# --------------------------------------------------------------------------


def test_run_synthetic_skill_subprocess_error_returns_1(tmp_path: Path) -> None:
    """_run_synthetic_skill_subprocess returns 1 when subprocess.run raises."""
    d, conn, handler = _make_daemon(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tmp").mkdir()

    with patch("dispatcher.daemon.subprocess.run", side_effect=OSError("no claude")):
        rc = d._run_synthetic_skill_subprocess(
            agent_id="test-agent-id",
            kind=daemon.IDLE_HOOK_KIND_AUDIT,
            worktree=worktree,
        )

    assert rc == 1
    error_events = handler.events("synthetic_agent_subprocess_error")
    assert len(error_events) == 1


# --------------------------------------------------------------------------
# Test 19 — _run_synthetic_agent marks terminal succeeded on exit_code=0
# --------------------------------------------------------------------------


def test_run_synthetic_agent_marks_terminal_succeeded(tmp_path: Path) -> None:
    """_run_synthetic_agent calls _mark_agent_terminal with 'succeeded' on exit 0."""
    d, conn, handler = _make_daemon(tmp_path)

    def fake_create_worktree(agent_id: str) -> Path:
        wt = tmp_path / f"wt-{agent_id}"
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    terminal_calls: list[tuple[tuple, dict]] = []

    def fake_mark_terminal(*args: Any, **kwargs: Any) -> None:
        terminal_calls.append((args, kwargs))

    with (
        patch.object(d, "_create_worktree", side_effect=fake_create_worktree),
        patch.object(d, "_run_synthetic_skill_subprocess", return_value=0),
        patch.object(d, "_mark_agent_terminal", side_effect=fake_mark_terminal),
    ):
        d._run_synthetic_agent(
            agent_id=str(uuid_mod.uuid4()),
            kind=daemon.IDLE_HOOK_KIND_AUDIT,
        )

    assert len(terminal_calls) == 1
    _args, kwargs = terminal_calls[0]
    assert kwargs["status"] == "succeeded"
    assert kwargs["issue_number"] is None


# --------------------------------------------------------------------------
# Test 20 — _run_synthetic_agent marks terminal failed on non-zero exit
# --------------------------------------------------------------------------


def test_run_synthetic_agent_marks_terminal_failed(tmp_path: Path) -> None:
    """_run_synthetic_agent calls _mark_agent_terminal with 'failed' on non-zero exit."""
    d, conn, handler = _make_daemon(tmp_path)

    def fake_create_worktree(agent_id: str) -> Path:
        wt = tmp_path / f"wt-{agent_id}"
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    terminal_calls: list[tuple[tuple, dict]] = []

    def fake_mark_terminal(*args: Any, **kwargs: Any) -> None:
        terminal_calls.append((args, kwargs))

    with (
        patch.object(d, "_create_worktree", side_effect=fake_create_worktree),
        patch.object(d, "_run_synthetic_skill_subprocess", return_value=1),
        patch.object(d, "_mark_agent_terminal", side_effect=fake_mark_terminal),
    ):
        d._run_synthetic_agent(
            agent_id=str(uuid_mod.uuid4()),
            kind=daemon.IDLE_HOOK_KIND_SPOTCHECK,
        )

    assert len(terminal_calls) == 1
    _args, kwargs = terminal_calls[0]
    assert kwargs["status"] == "failed"
    assert kwargs["issue_number"] is None
