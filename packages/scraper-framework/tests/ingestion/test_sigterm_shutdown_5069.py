"""SIGTERM handling in the ingestion worker (#5069).

ECS stops a task with SIGTERM, then SIGKILL after ``stopTimeout``.  Before
#5069 the worker (PID 1) had no handler, so every deploy SIGKILLed it (exit
137) and left the #5055 crash charge on the message in flight.  These tests
send real SIGTERMs to the test process (and, for the exit code, to a child
process) and check:

* mid-message: the message finishes and is acked, no charge stays, ``run()``
  returns, and no new message is claimed;
* idle: ``run()`` returns at once;
* past the grace period: the message is released (charge taken back, not
  acked) and the process exits 0.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ingestion import shutdown as shutdown_mod
from ingestion.shutdown import (
    DEFAULT_SHUTDOWN_GRACE_S,
    RELEASED_EXIT_CODE,
    SHUTDOWN_GRACE_ENV,
    ShutdownCoordinator,
    shutdown_grace_from_env,
)
from ingestion.worker import PROCESSING_CRASHES_KEY_PREFIX, IngestionWorker

CHILD = Path(__file__).with_name("_sigterm_child.py")


class _FakeRedis:
    """The stream calls the worker makes against one consumer group.

    ``batches`` are returned by successive XREADGROUP calls; once they run
    out, ``on_idle`` (if set) runs inside the blocking read."""

    def __init__(self, n: int) -> None:
        self.entries = {
            f"{i}-0".encode(): {b"data": json.dumps({"document_id": f"doc-{i}"}).encode()}
            for i in range(1, n + 1)
        }
        self.batches: list[list[bytes]] = [list(self.entries)]
        self.pending: list[bytes] = []
        self.kv: dict[str, int] = {}
        self.on_idle: Any = None
        self.idle_reads = 0

    def xreadgroup(self, group: str, consumer: str, streams: Any, **kw: Any) -> Any:
        if self.batches:
            ids = self.batches.pop(0)
            self.pending.extend(ids)
            return [(b"document.captured", [(i, self.entries[i]) for i in ids])]
        self.idle_reads += 1
        if self.on_idle is not None:
            self.on_idle()
        return []

    def xautoclaim(self, *args: Any, **kw: Any) -> Any:
        return (b"0-0", [], [])

    def xack(self, stream: str, group: str, msg_id: bytes) -> int:
        self.pending.remove(msg_id)
        return 1

    def incr(self, key: str) -> int:
        self.kv[key] = self.kv.get(key, 0) + 1
        return self.kv[key]

    def decr(self, key: str) -> int:
        self.kv[key] = self.kv.get(key, 0) - 1
        return self.kv[key]

    def expire(self, key: str, seconds: int) -> bool:
        return True

    def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0


def _key(msg_id: str) -> str:
    return f"{PROCESSING_CRASHES_KEY_PREFIX}{msg_id}"


@pytest.fixture(autouse=True)
def _restore_sigterm() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def _worker(
    redis: _FakeRedis, *, grace_s: float = 30.0, exit_fn: Any = None
) -> tuple[IngestionWorker, list[int]]:
    worker = IngestionWorker(
        redis_client=redis,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker.process_event = MagicMock()  # type: ignore[method-assign]
    worker._record_dead_letter = MagicMock()  # type: ignore[method-assign]
    worker.health_check = MagicMock()  # type: ignore[method-assign]
    worker._ensure_consumer_group = MagicMock()  # type: ignore[method-assign]
    worker._cleanup_stale_consumers = MagicMock()  # type: ignore[method-assign]
    worker._maybe_trim_consumed_entries = MagicMock()  # type: ignore[method-assign]
    worker.close = MagicMock()  # type: ignore[method-assign]
    exits: list[int] = []
    worker._shutdown = ShutdownCoordinator(
        worker._release_in_flight,
        grace_s=grace_s,
        exit_fn=exit_fn if exit_fn is not None else exits.append,
    )
    worker.install_shutdown_handler()
    return worker, exits


def _sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _run_in_time(worker: IngestionWorker, limit_s: float = 10.0) -> float:
    start = time.monotonic()
    worker.run(block_ms=100)
    elapsed = time.monotonic() - start
    assert elapsed < limit_s
    return elapsed


def test_sigterm_mid_message_finishes_it_and_claims_no_more() -> None:
    redis = _FakeRedis(3)
    worker, exits = _worker(redis)

    def process(event: dict[str, Any]) -> None:
        _sigterm()  # arrives while message 1 is in flight
        assert redis.kv == {_key("1-0"): 1}  # charged while it runs

    worker.process_event.side_effect = process
    _run_in_time(worker)

    worker.process_event.assert_called_once()  # messages 2-3 not started
    assert b"1-0" not in redis.pending  # acked
    assert redis.pending == [b"2-0", b"3-0"]  # left for XAUTOCLAIM
    assert redis.kv == {}  # no crash charge on anything
    assert exits == []  # a clean return, not a forced exit
    assert worker._shutdown.in_flight is None
    worker.close.assert_called_once()


def test_sigterm_idle_returns_at_once() -> None:
    redis = _FakeRedis(0)
    redis.batches = []

    def sleep_in_read() -> None:
        _sigterm()
        time.sleep(30)  # pragma: no cover - interrupted by WorkerShutdown

    redis.on_idle = sleep_in_read
    worker, exits = _worker(redis)

    assert _run_in_time(worker) < 5
    worker.process_event.assert_not_called()
    assert redis.kv == {}
    assert exits == []
    worker.close.assert_called_once()


def test_sigterm_past_grace_releases_message_uncharged_and_unacked() -> None:
    redis = _FakeRedis(2)
    exited = threading.Event()
    exits: list[int] = []

    def exit_fn(code: int) -> None:
        exits.append(code)
        exited.set()

    worker, _ = _worker(redis, grace_s=0.2, exit_fn=exit_fn)

    def slow(event: dict[str, Any]) -> None:
        _sigterm()
        # Outlasts the grace period; in production os._exit ends it here.
        assert exited.wait(5)

    worker.process_event.side_effect = slow
    _run_in_time(worker)

    assert exits == [RELEASED_EXIT_CODE] == [0]
    assert redis.kv == {_key("1-0"): 0}  # charge taken back: no blame
    assert redis.pending == [b"1-0", b"2-0"]  # message 1 NOT acked
    assert worker._shutdown.released == b"1-0"
    worker.process_event.assert_called_once()


def test_released_message_is_reprocessed_without_blame() -> None:
    """The consumer that reclaims a released message sees no prior crash."""
    redis = _FakeRedis(1)
    redis.kv[_key("1-0")] = 0  # what a release leaves
    redis.pending = [b"1-0"]
    worker, _ = _worker(redis)
    with patch("ingestion.worker.logger") as log:
        assert worker._process_message(b"1-0", redis.entries[b"1-0"]) is True
    assert not any("again" in str(call.args[0]) for call in log.warning.call_args_list if call.args)
    assert redis.kv == {}


def test_end_processing_after_release_does_not_take_back_twice() -> None:
    redis = _FakeRedis(1)
    worker, _ = _worker(redis)
    redis.kv[_key("1-0")] = 0
    worker._shutdown.released = b"1-0"
    worker._end_processing(b"1-0")
    assert redis.kv == {_key("1-0"): 0}


def test_keyboard_interrupt_still_takes_the_charge_back() -> None:
    """#5055 behaviour kept: Ctrl-C mid-message is not a crash."""
    redis = _FakeRedis(1)
    worker, _ = _worker(redis)
    worker.process_event.side_effect = KeyboardInterrupt
    worker.run(block_ms=100)
    assert redis.kv == {_key("1-0"): 0}
    assert redis.pending == [b"1-0"]


def test_second_sigterm_is_ignored() -> None:
    redis = _FakeRedis(1)
    worker, exits = _worker(redis)

    def process(event: dict[str, Any]) -> None:
        _sigterm()
        first = worker._shutdown.requested_at
        _sigterm()
        assert worker._shutdown.requested_at == first

    worker.process_event.side_effect = process
    _run_in_time(worker)
    assert redis.kv == {}
    assert exits == []


def test_request_shutdown_before_run_claims_nothing() -> None:
    redis = _FakeRedis(2)
    worker, _ = _worker(redis)
    worker.request_shutdown()
    worker.run(block_ms=100)
    worker.process_event.assert_not_called()
    assert redis.pending == []


def test_reclaim_stops_claiming_after_sigterm() -> None:
    redis = _FakeRedis(3)
    worker, _ = _worker(redis)
    claimed = [(i, redis.entries[i]) for i in redis.entries]
    redis.pending = list(redis.entries)
    redis.xautoclaim = lambda *a, **kw: (b"0-0", claimed, [])  # type: ignore[method-assign]
    worker.process_event.side_effect = lambda event: worker.request_shutdown()
    assert worker._reclaim_pending_messages() == 1
    assert redis.pending == [b"2-0", b"3-0"]
    assert redis.kv == {}


def test_release_logs_redis_failure() -> None:
    redis = MagicMock()
    redis.decr.side_effect = ConnectionError("down")
    worker = IngestionWorker(
        redis_client=redis,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    with patch("ingestion.worker.logger") as log:
        worker._release_in_flight(b"1-0")
    log.warning.assert_called_once()


def test_watchdog_exits_with_nothing_in_flight() -> None:
    exits: list[int] = []
    coordinator = ShutdownCoordinator(MagicMock(), grace_s=0.05, exit_fn=exits.append)
    coordinator.install()
    coordinator.request()
    assert coordinator._watchdog is not None
    coordinator._watchdog.join(5)
    assert exits == [0]
    coordinator._release.assert_not_called()  # type: ignore[attr-defined]


def test_watchdog_does_nothing_after_clean_stop() -> None:
    exits: list[int] = []
    coordinator = ShutdownCoordinator(MagicMock(), grace_s=0.05, exit_fn=exits.append)
    coordinator.install()
    coordinator.stopped()
    coordinator.request()
    assert coordinator._watchdog is not None
    coordinator._watchdog.join(5)
    assert exits == []
    assert coordinator.seconds_since_request() is not None


def test_seconds_since_request_none_before_request() -> None:
    assert ShutdownCoordinator(MagicMock()).seconds_since_request() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DEFAULT_SHUTDOWN_GRACE_S),
        ("42", 42.0),
        ("0", 0.0),
        ("abc", DEFAULT_SHUTDOWN_GRACE_S),
        ("-1", DEFAULT_SHUTDOWN_GRACE_S),
    ],
)
def test_shutdown_grace_from_env(raw: str, expected: float) -> None:
    with patch.dict(os.environ, {SHUTDOWN_GRACE_ENV: raw}):
        assert shutdown_grace_from_env() == expected


def test_grace_is_below_the_task_definition_stop_timeout() -> None:
    """The release must run before ECS's SIGKILL (stopTimeout 120 s)."""
    main_tf = Path(__file__).resolve().parents[4] / "infra/terraform/modules/compute/main.tf"
    text = main_tf.read_text()
    block = text[text.index('resource "aws_ecs_task_definition" "ingestion_worker"') :]
    block = block[: block.index("logConfiguration")]
    assert "stopTimeout = 120" in block
    assert DEFAULT_SHUTDOWN_GRACE_S <= 120 - 10


def test_main_installs_the_handler_before_run() -> None:
    env = {
        "DATABASE_URL": "postgresql://localhost/test",
        "REDIS_URL": "redis://localhost:6379",
        "OPENSEARCH_URL": "https://localhost:9200",
        "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
    }
    from ingestion import __main__ as entry

    order: list[str] = []
    worker = MagicMock()
    worker.install_shutdown_handler.side_effect = lambda: order.append("install")
    worker.run.side_effect = lambda: order.append("run")
    with (
        patch.dict(os.environ, env),
        patch.object(entry, "IngestionWorker", return_value=worker),
        patch.object(entry, "make_s3_client"),
        patch.object(entry, "make_opensearch_client"),
        patch.object(entry.redis, "Redis"),
    ):
        entry.main()
    assert order == ["install", "run"]


def test_request_while_idle_does_not_raise_in_the_caller() -> None:
    """``request()`` may come from another thread: it never raises there."""
    coordinator = ShutdownCoordinator(MagicMock())
    coordinator.idle = True
    coordinator.request()
    assert coordinator.requested


def test_no_release_when_the_charge_was_never_taken() -> None:
    """INCR failed: the watchdog must not DECR a charge that was never added."""
    redis = _FakeRedis(1)
    redis.pending = [b"1-0"]
    redis.incr = MagicMock(side_effect=ConnectionError("down"))  # type: ignore[method-assign]
    seen: list[bytes | None] = []
    worker, _ = _worker(redis)
    worker.process_event.side_effect = lambda event: seen.append(worker._shutdown.in_flight)
    assert worker._process_message(b"1-0", redis.entries[b"1-0"]) is True
    assert seen == [None]


def test_flush_logs_is_best_effort() -> None:
    shutdown_mod._flush_logs()


# ---------------------------------------------------------------------------
# Real process: exit codes
# ---------------------------------------------------------------------------


def _run_child(mode: str, tmp_path: Path) -> tuple[int, dict[str, Any]]:
    state = tmp_path / "state.json"
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), mode, str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, SHUTDOWN_GRACE_ENV: "0.5", "JUDGEMIND_EXTRACTION": "optional"},
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:  # pragma: no branch - READY always comes
        if line.strip() == "READY":
            break
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - the bug this guards
        proc.kill()
        raise
    assert state.exists(), out
    return proc.returncode, json.loads(state.read_text())


@pytest.mark.parametrize("mode", ["idle", "busy", "slow"])
def test_child_process_exits_zero_on_sigterm(mode: str, tmp_path: Path) -> None:
    code, state = _run_child(mode, tmp_path)
    assert code == 0
    assert all(v <= 0 for v in state["kv"].values()), state  # no crash charge
    if mode == "busy":
        assert "1-0" not in state["pending"]  # finished and acked
    if mode == "slow":
        assert state["pending"] == ["1-0"]  # released unacked
        assert state["kv"] == {_key("1-0"): 0}
