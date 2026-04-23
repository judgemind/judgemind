"""Unit tests for the ralph per-iteration HEAD-SHA watcher (#3042).

The watcher replaces the LLM-skipped CLI helper (issue #3042 evidence:
13 ralph SHIPs, 3 multi-iteration, zero rows with ``iteration_n IS NOT
NULL`` in ``dispatcher.ralph_patches``). The daemon now polls
``git -C <worktree> rev-parse HEAD`` every
:data:`daemon.RALPH_HEAD_POLL_INTERVAL_SECONDS` during the ralph phase
and calls :meth:`DispatcherDaemon._persist_ralph_iteration_patch` on
each observed SHA change.

Coverage:

* **AC #1** — Watcher inserts a row per observed SHA change during
  ralph. Baseline SHA does NOT persist (redundant with the previous
  iteration's terminal write).
* **AC #2** — Watcher stops cleanly on phase exit (success, failure,
  timeout). Thread joins without leaking.
* **AC #3** — Watcher does not fire on non-ralph phases (plan, summary,
  verify, fix-ci, retro).
* **AC #4** — Polling interval is bounded. Exposed as
  :data:`RALPH_HEAD_POLL_INTERVAL_SECONDS = 30`.
* **iteration_n inference** — Reads ``{worktree}/tmp/ralph/iteration.txt``.
  Falls back to 0 on missing / malformed content.

All DB access goes through ``_FakeConnection`` + ``_FakeCursor``; no
real Postgres, no real claude subprocess.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class _UniqueViolation(Exception):
    """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules.setdefault("psycopg", _psycopg_stub)

from dispatcher import daemon  # noqa: E402  — sys.path mutation above

# --------------------------------------------------------------------------
# Shared fakes — parity with sibling ralph_patches test modules
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
    logger = logging.getLogger(f"dispatcher.test.head_watcher.{id(tmp_path)}")
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
# Polling interval bound (AC #4)
# --------------------------------------------------------------------------


class TestPollingIntervalConstant:
    def test_interval_is_defined(self) -> None:
        assert hasattr(daemon, "RALPH_HEAD_POLL_INTERVAL_SECONDS")

    def test_interval_is_positive_int(self) -> None:
        assert isinstance(daemon.RALPH_HEAD_POLL_INTERVAL_SECONDS, int)
        assert daemon.RALPH_HEAD_POLL_INTERVAL_SECONDS > 0

    def test_interval_is_bounded_reasonable_window(self) -> None:
        """AC #4 — the poll interval is bounded (at most once per 30s).
        Allow some slack either direction so a future tuning change that
        stays within the bounded-overhead intent doesn't break the test,
        but guard hard against an accidental sub-second or hour-long
        regression."""
        assert 10 <= daemon.RALPH_HEAD_POLL_INTERVAL_SECONDS <= 120


# --------------------------------------------------------------------------
# iteration.txt inference
# --------------------------------------------------------------------------


class TestReadRalphIterationFile:
    def test_returns_parsed_int(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        state_dir = worktree / "tmp" / "ralph"
        state_dir.mkdir(parents=True)
        (state_dir / "iteration.txt").write_text("3\n", encoding="utf-8")

        result = daemon.DispatcherDaemon._read_ralph_iteration_file(worktree)
        assert result == 3

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # No tmp/ralph/iteration.txt at all.
        assert daemon.DispatcherDaemon._read_ralph_iteration_file(worktree) == 0

    def test_malformed_content_returns_zero(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        state_dir = worktree / "tmp" / "ralph"
        state_dir.mkdir(parents=True)
        (state_dir / "iteration.txt").write_text("not-an-int\n", encoding="utf-8")

        assert daemon.DispatcherDaemon._read_ralph_iteration_file(worktree) == 0

    def test_empty_file_returns_zero(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        state_dir = worktree / "tmp" / "ralph"
        state_dir.mkdir(parents=True)
        (state_dir / "iteration.txt").write_text("", encoding="utf-8")

        assert daemon.DispatcherDaemon._read_ralph_iteration_file(worktree) == 0


# --------------------------------------------------------------------------
# _read_head_sha
# --------------------------------------------------------------------------


class TestReadHeadSha:
    def test_returns_trimmed_sha(self, monkeypatch: Any, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        result = subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=0,
            stdout="abc123def456\n",
            stderr="",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)

        assert daemon.DispatcherDaemon._read_head_sha(worktree) == "abc123def456"

    def test_returns_none_on_nonzero_exit(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        result = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fatal: not a git repo"
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
        assert daemon.DispatcherDaemon._read_head_sha(tmp_path) is None

    def test_returns_none_on_exception(self, monkeypatch: Any, tmp_path: Path) -> None:
        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("boom")

        monkeypatch.setattr(subprocess, "run", boom)
        assert daemon.DispatcherDaemon._read_head_sha(tmp_path) is None


# --------------------------------------------------------------------------
# Watcher loop — end-to-end behaviour driven by deterministic fakes
# --------------------------------------------------------------------------


class _ShaSequence:
    """Return a scripted sequence of SHA values from successive calls.

    After the scripted values are exhausted, return the final value
    repeatedly — models "HEAD has settled, no more iterations" which
    keeps the watcher quiescent until the caller stops it.
    """

    def __init__(self, shas: list[str | None]) -> None:
        self._shas = list(shas)
        self._idx = 0

    def __call__(self, worktree: Path) -> str | None:
        if self._idx < len(self._shas):
            value = self._shas[self._idx]
            self._idx += 1
            return value
        return self._shas[-1] if self._shas else None


class TestWatcherLoopBehaviour:
    """Drive :meth:`_ralph_head_watcher_loop` with a zeroed poll
    interval so the test runs in milliseconds. The stop_event is
    set after N ticks by advancing through the SHA sequence and then
    asserting the persist helper was called the right number of times."""

    def _run_loop(
        self,
        monkeypatch: Any,
        d: daemon.DispatcherDaemon,
        worktree: Path,
        shas: list[str | None],
        iteration_contents: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the list of kwargs recorded per persist call."""
        worktree.mkdir(parents=True, exist_ok=True)
        state_dir = worktree / "tmp" / "ralph"
        state_dir.mkdir(parents=True, exist_ok=True)
        if iteration_contents is not None:
            (state_dir / "iteration.txt").write_text(
                iteration_contents, encoding="utf-8"
            )

        # Shrink the poll interval so test latency is measured in ms.
        monkeypatch.setattr(daemon, "RALPH_HEAD_POLL_INTERVAL_SECONDS", 0)

        # Stub psycopg.connect — the watcher opens its own connection.
        fake_watcher_conn = _FakeConnection()
        fake_psycopg = sys.modules["psycopg"]
        fake_psycopg.connect = MagicMock(return_value=fake_watcher_conn)

        # Stub the SHA read.
        seq = _ShaSequence(shas)
        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_read_head_sha", staticmethod(seq)
        )

        # Replace the persist helper with a recorder. Return a bogus
        # patch_id per call so the watcher logs the persisted event.
        persist_calls: list[dict[str, Any]] = []

        def fake_persist(
            self_: Any,
            *,
            agent_id: str,
            issue_number: int,
            iteration_n: int,
            verdict: str,
            worktree: Path,
        ) -> str:
            persist_calls.append(
                {
                    "agent_id": agent_id,
                    "issue_number": issue_number,
                    "iteration_n": iteration_n,
                    "verdict": verdict,
                    "worktree": worktree,
                }
            )
            return f"patch-{len(persist_calls)}"

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_persist_ralph_iteration_patch", fake_persist
        )

        stop_event = threading.Event()
        thread = threading.Thread(
            target=d._ralph_head_watcher_loop,
            args=("agent-uuid-1234", 42, worktree, stop_event),
            daemon=True,
        )
        thread.start()

        # Wait for the scripted SHAs to be consumed, then stop. Poll
        # the call recorder rather than sleep a fixed window — much
        # more reliable under CI load.
        expected_persists = sum(
            1
            for i, sha in enumerate(shas)
            if sha is not None and i > 0 and sha != shas[i - 1]
        )
        deadline = 5.0  # generous — the loop should churn through in ms
        import time as _time

        start = _time.monotonic()
        while len(persist_calls) < expected_persists:
            if _time.monotonic() - start > deadline:
                break
            _time.sleep(0.01)

        stop_event.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "watcher thread did not exit after stop_event"

        return persist_calls

    def test_sha_change_triggers_persist_with_loop_verdict(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC #1 happy path — one SHA change → one persist call with
        verdict='LOOP' and iteration_n from iteration.txt."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        calls = self._run_loop(
            monkeypatch,
            d,
            worktree,
            shas=["sha-baseline", "sha-iter-2"],
            iteration_contents="2\n",
        )
        assert len(calls) == 1
        assert calls[0]["verdict"] == "LOOP"
        assert calls[0]["iteration_n"] == 2
        assert calls[0]["agent_id"] == "agent-uuid-1234"
        assert calls[0]["issue_number"] == 42

    def test_baseline_sha_does_not_persist(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The first observed SHA is the baseline — no persist. Only
        SHA *changes* after the baseline trigger writes."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        calls = self._run_loop(
            monkeypatch,
            d,
            worktree,
            shas=["sha-steady", "sha-steady", "sha-steady"],
            iteration_contents="1\n",
        )
        assert calls == []

    def test_multiple_sha_changes_each_persist(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Two sequential SHA changes → two persist calls."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        calls = self._run_loop(
            monkeypatch,
            d,
            worktree,
            shas=["sha-1", "sha-2", "sha-3"],
            iteration_contents="2\n",
        )
        assert len(calls) == 2
        assert all(c["verdict"] == "LOOP" for c in calls)

    def test_unchanged_sha_does_not_persist(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC #1 negative path — no SHA change across 4 polls, no row."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        calls = self._run_loop(
            monkeypatch,
            d,
            worktree,
            shas=["same-sha", "same-sha", "same-sha", "same-sha"],
            iteration_contents="1\n",
        )
        assert calls == []

    def test_stop_event_exits_cleanly(self, monkeypatch: Any, tmp_path: Path) -> None:
        """AC #2 — stop_event set → thread exits, no leak."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        # _run_loop already sets stop_event after scripted SHAs are
        # consumed and asserts the thread is no longer alive; the
        # presence of this dedicated test makes the coverage intent
        # explicit.
        self._run_loop(
            monkeypatch,
            d,
            worktree,
            shas=["sha-a", "sha-a"],
            iteration_contents="1\n",
        )

    def test_iteration_file_missing_falls_back_to_zero(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """iteration_n inference failure does not crash — falls back to 0."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        calls = self._run_loop(
            monkeypatch,
            d,
            worktree,
            shas=["sha-a", "sha-b"],
            iteration_contents=None,  # do not create iteration.txt
        )
        assert len(calls) == 1
        assert calls[0]["iteration_n"] == 0


# --------------------------------------------------------------------------
# Watcher gating — only runs during ralph phase
# --------------------------------------------------------------------------


def _patch_popen_and_forwarder(
    monkeypatch: Any,
    captured: dict[str, Any] | None = None,
) -> None:
    """Install test doubles for ``subprocess.Popen`` + the stream
    forwarder so ``_spawn_phase_subprocess`` can be driven without a
    real ``claude`` binary. Mirrors the fixture in
    ``test_daemon_ralph_opus_final_attempt.py``."""
    fake_proc = MagicMock()
    fake_proc.wait.return_value = 0
    fake_proc.returncode = 0
    fake_proc.stdout = None
    fake_proc.stderr = None

    def fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        if captured is not None:
            captured["cmd"] = cmd
        return fake_proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    fake_threads = MagicMock()
    fake_threads.join.return_value = None
    monkeypatch.setattr(
        daemon, "stream_subprocess_output_async", lambda *a, **kw: fake_threads
    )


class TestWatcherGatedByRalphPhase:
    """AC #3 — the watcher only starts for ``phase == 'ralph'``."""

    @pytest.mark.parametrize(
        "phase",
        ["plan", "summary", "verify", "fix-ci", "retro"],
    )
    def test_non_ralph_phase_does_not_start_watcher(
        self, monkeypatch: Any, tmp_path: Path, phase: str
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # _model_for_phase calls _current_attempt_for only for ralph;
        # for non-ralph phases the PHASE_MODELS default is used without
        # a DB query, but prime the fetch queue defensively anyway.
        conn.cursor_instance.fetch_queue = [(0,)]

        start_calls: list[tuple[Any, ...]] = []

        def fake_start(
            self_: Any,
            *,
            agent_id: str,
            issue_number: int | None,
            worktree: Path,
        ) -> None:
            start_calls.append((agent_id, issue_number, worktree))
            return None

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_start_ralph_head_watcher", fake_start
        )
        _patch_popen_and_forwarder(monkeypatch)
        d._spawn_phase_subprocess(phase, tmp_path, "agent-id")

        assert start_calls == []

    def test_ralph_phase_starts_watcher(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]  # retries_used

        start_calls: list[tuple[Any, ...]] = []
        stop_event = threading.Event()
        fake_thread = MagicMock()
        fake_thread.join.return_value = None
        fake_thread.is_alive.return_value = False

        def fake_start(
            self_: Any,
            *,
            agent_id: str,
            issue_number: int | None,
            worktree: Path,
        ) -> tuple[threading.Thread, threading.Event]:
            start_calls.append((agent_id, issue_number, worktree))
            return fake_thread, stop_event

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_start_ralph_head_watcher", fake_start
        )
        _patch_popen_and_forwarder(monkeypatch)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-id")

        assert len(start_calls) == 1
        assert start_calls[0][0] == "agent-id"
        # stop_event was signalled and the watcher thread was joined.
        assert stop_event.is_set()
        fake_thread.join.assert_called_once()

    def test_ralph_phase_stops_watcher_on_timeout(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """AC #2 — timeout path must still stop + join the watcher."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        stop_event = threading.Event()
        fake_thread = MagicMock()
        fake_thread.join.return_value = None
        fake_thread.is_alive.return_value = False

        def fake_start(
            self_: Any,
            *,
            agent_id: str,
            issue_number: int | None,
            worktree: Path,
        ) -> tuple[threading.Thread, threading.Event]:
            return fake_thread, stop_event

        monkeypatch.setattr(
            daemon.DispatcherDaemon, "_start_ralph_head_watcher", fake_start
        )

        # Popen that returns a proc whose wait() raises TimeoutExpired,
        # then a kill() + wait() that resolves. Matches the real
        # _spawn_phase_subprocess timeout branch.
        fake_proc = MagicMock()
        fake_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=1),
            0,
        ]
        fake_proc.returncode = -9
        fake_proc.stdout = None
        fake_proc.stderr = None
        fake_proc.kill.return_value = None

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake_proc)
        monkeypatch.setattr(
            daemon, "stream_subprocess_output_async", lambda *a, **kw: MagicMock()
        )

        with pytest.raises(subprocess.TimeoutExpired):
            d._spawn_phase_subprocess("ralph", tmp_path, "agent-id")

        # Watcher was signalled and joined despite the timeout.
        assert stop_event.is_set()
        fake_thread.join.assert_called_once()

    def test_start_ralph_head_watcher_skips_missing_worktree(
        self, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        missing = tmp_path / "does-not-exist"
        result = d._start_ralph_head_watcher(
            agent_id="agent-id",
            issue_number=1,
            worktree=missing,
        )
        assert result is None
        assert handler.events("ralph_head_watcher_skip")

    def test_start_ralph_head_watcher_emits_started_event(self, tmp_path: Path) -> None:
        """The watcher start helper emits a structured event so
        CloudWatch can confirm live activity per-agent."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Patch psycopg.connect so the watcher thread does not try to
        # talk to a real DB.
        fake_psycopg = sys.modules["psycopg"]
        fake_psycopg.connect = MagicMock(return_value=_FakeConnection())

        result = d._start_ralph_head_watcher(
            agent_id="agent-id",
            issue_number=1,
            worktree=worktree,
        )
        assert result is not None
        thread, stop_event = result
        try:
            events = handler.events("ralph_head_watcher_started")
            assert events
            assert events[0].poll_interval_seconds == (
                daemon.RALPH_HEAD_POLL_INTERVAL_SECONDS
            )
        finally:
            stop_event.set()
            thread.join(timeout=5)
