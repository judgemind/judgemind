"""``_spawn_diagnoser_subprocess`` is async fire-and-forget (#3376).

Pre-#3376 the spawn called ``proc.wait()`` synchronously inside
``supervisor_tick``. With the empowered diagnoser's 90-min wall-clock
budget, any run exceeding the watchdog's 120-s exit threshold killed
the daemon for ``scheduler_tick_stalled`` and orphaned the diagnosis
row at ``status='pending'`` forever — three observed kills 20:14–20:34Z
2026-04-25.

These tests assert the async contract:

* Returns within ~1 s even when the underlying ``Popen`` would block
  forever on ``wait()``.
* Returns the OS PID (``int``) on success, ``None`` on launch failure.
* Calls ``Popen`` with ``start_new_session=True`` so the reaper can
  signal the diagnoser process group cleanly.
* The supervisor tick's spawn pass writes the PID to
  ``dispatcher.diagnoses.subprocess_pid``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402
from dispatcher.tests._popen_fake import make_popen_factory  # noqa: E402


# --------------------------------------------------------------------------
# Fakes — minimal DB stubs so the spawn pass can run without psycopg.
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


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _make_daemon(tmp_path: Path) -> daemon.DispatcherDaemon:
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
        baseline_repo_root=tmp_path,
    )
    log = logging.getLogger(f"dispatcher.test.async_spawn.{id(tmp_path)}")
    log.handlers = []
    log.propagate = False
    d = daemon.DispatcherDaemon(cfg, log)
    d._conn = _FakeConn()  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


# --------------------------------------------------------------------------
# AC: returns within ~1 s, no proc.wait() in the supervisor-tick path.
# --------------------------------------------------------------------------


class TestSpawnReturnsImmediately:
    """``_spawn_diagnoser_subprocess`` must NOT call ``proc.wait()`` —
    the whole point of the async refactor is that the supervisor tick
    returns quickly so the watchdog never trips on a long diagnoser run.
    """

    def test_returns_within_one_second(self, monkeypatch: Any, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)

        # The fake Popen never blocks — but we still want a stopwatch
        # assertion as a regression guard. If a future refactor
        # accidentally re-introduces proc.wait() on the production
        # path, a 1 s budget here will not catch it on the fake (the
        # fake's wait() returns instantly), but the structural assertion
        # below WILL — by stubbing Popen with a factory whose .wait()
        # blocks forever and asserting the call returns nonetheless.
        monkeypatch.setattr(
            subprocess, "Popen", make_popen_factory(returncode=0, pid=4242)
        )

        start = time.monotonic()
        pid = d._spawn_diagnoser_subprocess(101)
        elapsed = time.monotonic() - start

        assert pid == 4242
        assert elapsed < 1.0, (
            f"_spawn_diagnoser_subprocess must return within ~1s "
            f"(got {elapsed:.3f}s) so the watchdog never trips on a "
            f"long-running diagnoser. Issue #3376 regression guard."
        )

    def test_does_not_call_proc_wait(self, monkeypatch: Any, tmp_path: Path) -> None:
        """Structural guard: the spawn path returns even when ``wait()``
        would block forever. Pre-#3376 this test would hang.
        """
        d = _make_daemon(tmp_path)

        class _BlockingPopen:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self.pid = 7777
                self.stdout = None
                self.stderr = None
                self._wait_called = False

            def wait(self, timeout: Any = None) -> int:  # noqa: ARG002
                self._wait_called = True
                # If anything ever calls wait, hang forever. The async
                # contract guarantees this path is not exercised.
                while True:
                    time.sleep(60)

            def kill(self) -> None:
                pass

            def poll(self) -> int | None:
                return None

        monkeypatch.setattr(subprocess, "Popen", _BlockingPopen)

        # Use a thread-watcher with a short timeout to detect a hang.
        import threading

        result: dict[str, Any] = {}

        def runner() -> None:
            result["pid"] = d._spawn_diagnoser_subprocess(202)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert not t.is_alive(), (
            "_spawn_diagnoser_subprocess hung — it must NOT call "
            "proc.wait(). Issue #3376."
        )
        assert result.get("pid") == 7777


# --------------------------------------------------------------------------
# AC: returns the OS PID; None on FileNotFoundError / OSError.
# --------------------------------------------------------------------------


class TestSpawnReturnShape:
    def test_returns_pid_on_success(self, monkeypatch: Any, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        monkeypatch.setattr(
            subprocess, "Popen", make_popen_factory(pid=12345, returncode=0)
        )
        pid = d._spawn_diagnoser_subprocess(303)
        assert isinstance(pid, int)
        assert pid == 12345

    def test_returns_none_on_filenotfound(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)

        def fake_popen(_cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError("claude missing")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        assert d._spawn_diagnoser_subprocess(404) is None


# --------------------------------------------------------------------------
# AC: Popen invoked with start_new_session=True.
# --------------------------------------------------------------------------


class TestSpawnDetaches:
    def test_start_new_session_true(self, monkeypatch: Any, tmp_path: Path) -> None:
        """Detached process group so the reaper's SIGTERM/SIGKILL targets
        only the diagnoser tree (not any operator-attached terminal).
        """
        d = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def on_start(_cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["kwargs"] = kwargs

        monkeypatch.setattr(
            subprocess, "Popen", make_popen_factory(on_start=on_start, pid=1)
        )
        d._spawn_diagnoser_subprocess(505)

        assert captured["kwargs"].get("start_new_session") is True


# --------------------------------------------------------------------------
# AC: spawn pass writes subprocess_pid to dispatcher.diagnoses.
# --------------------------------------------------------------------------


class TestSpawnPassPersistsPid:
    """The supervisor tick's spawn pass MUST persist the PID so the
    reaper can find the subprocess on a later tick. Without this,
    completed diagnoses sit pending forever.
    """

    def test_run_diagnoser_pass_persists_pid(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d = _make_daemon(tmp_path)

        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(d, "_max_concurrent_diagnoses", lambda: 5)
        monkeypatch.setattr(d, "_count_pending_diagnoses_with_pid", lambda: 0)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [
                {
                    "failure_id": 9001,
                    "agent_id": "agent-uuid",
                    "category": "subprocess_crash",
                    "tier": 2,
                    "issue_number": 1234,
                    "details": {},
                }
            ],
        )
        monkeypatch.setattr(d, "_build_diagnoser_context", lambda _c: {})
        monkeypatch.setattr(d, "_insert_pending_diagnosis", lambda **_kw: 77)
        monkeypatch.setattr(d, "_spawn_diagnoser_subprocess", lambda _id: 31337)

        recorded: dict[str, Any] = {}

        def fake_record(diagnosis_id: int, pid: int) -> None:
            recorded["id"] = diagnosis_id
            recorded["pid"] = pid

        monkeypatch.setattr(d, "_record_diagnoser_subprocess_pid", fake_record)

        ran = d._run_diagnoser_pass()
        assert ran == 1
        assert recorded == {"id": 77, "pid": 31337}

    def test_record_pid_emits_update_with_correct_params(self, tmp_path: Path) -> None:
        """_record_diagnoser_subprocess_pid issues the canonical UPDATE
        with positional bind params (pid, diagnosis_id)."""
        d = _make_daemon(tmp_path)
        d._record_diagnoser_subprocess_pid(diagnosis_id=99, pid=4242)

        conn = d._conn  # type: ignore[assignment]
        executed = conn.cursor_instance.executed  # type: ignore[union-attr]
        updates = [e for e in executed if "UPDATE dispatcher.diagnoses" in e[0]]
        assert len(updates) == 1
        sql, params = updates[0]
        assert "subprocess_pid" in sql
        assert "WHERE diagnosis_id" in sql
        assert params == (4242, 99)
