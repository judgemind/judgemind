"""Message leases: a live worker's messages are never reclaimed (#5091).

XAUTOCLAIM measures a pending message's idle time from its last delivery or
claim; processing does not refresh it.  During a rolling deploy the old and
new ingestion tasks overlap, and the new task's startup reclaim (min-idle
30 s) took over the old task's in-flight message (p95 52 s), charging a
false #5055 crash and processing the message twice.

The worker now renews the lease (pending idle time) of every message it
holds from a heartbeat thread, and the reclaim min-idle is derived from the
renewal interval.  These tests run two workers against one fake stream with
a fake clock:

* overlapping workers: one processing, no charge, batch-mates not stolen;
* a SIGKILLed holder: its lease lapses, one charge on reclaim;
* a long message (well past 30 s) is never stolen;
* after SIGTERM, only the in-flight message keeps its lease.

``TestRealRedis`` runs the renewal Lua script against a real Redis when
``JUDGEMIND_TEST_REDIS_URL`` is set.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from helpers.message_charges import ChargesFake, crash_field

from ingestion import worker as worker_mod
from ingestion.shutdown import ShutdownCoordinator
from ingestion.worker import (
    LEASE_MISSED_RENEWALS,
    LEASE_RENEW_INTERVAL_S,
    PENDING_RECLAIM_MIN_IDLE_MS,
    IngestionWorker,
)


class _Killed(BaseException):
    """Stands in for SIGKILL: the process stops mid-call."""


class _StreamRedis(ChargesFake):
    """One stream + consumer group with a pending entries list (PEL) and a
    fake clock, shared by several workers (consumers).

    ``kill()`` makes every later settling call a no-op, the way a SIGKILLed
    worker runs no cleanup; ``restart()`` revives Redis for the others."""

    def __init__(self, n: int) -> None:
        self.now_ms = 0
        self.entries = {
            f"{i}-0".encode(): {b"data": json.dumps({"document_id": f"doc-{i}"}).encode()}
            for i in range(1, n + 1)
        }
        self.undelivered = list(self.entries)
        #: msg id -> [owner, last delivery/claim ms, deliveries]
        self.pel: dict[bytes, list[Any]] = {}
        self.kv: dict[str, int] = {}
        self.alive = True
        self.evals = 0

    # -- test controls ---------------------------------------------------------

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)

    def idle_ms(self, msg_id: bytes) -> int:
        return int(self.now_ms - self.pel[msg_id][1])

    def kill(self) -> None:
        self.alive = False

    def restart(self) -> None:
        self.alive = True

    # -- stream commands -------------------------------------------------------

    def xreadgroup(self, group: str, consumer: str, streams: Any, **kw: Any) -> Any:
        ids = self.undelivered[: kw.get("count", 10)]
        del self.undelivered[: len(ids)]
        for msg_id in ids:
            self.pel[msg_id] = [consumer, self.now_ms, 1]
        if not ids:
            return []
        return [(b"document.captured", [(i, self.entries[i]) for i in ids])]

    def xautoclaim(
        self, stream: str, group: str, consumer: str, *, min_idle_time: int, **kw: Any
    ) -> Any:
        claimed = [
            msg_id
            for msg_id, (_owner, last, _n) in sorted(self.pel.items())
            if self.now_ms - last >= min_idle_time
        ][: kw.get("count", 100)]
        for msg_id in claimed:
            self.pel[msg_id] = [consumer, self.now_ms, self.pel[msg_id][2] + 1]
        return (b"0-0", [(i, self.entries[i]) for i in claimed], [])

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        """Emulates ``_RENEW_LEASES_LUA`` (run for real in ``TestRealRedis``)
        and ``_REFUND_CHARGE_LUA``."""
        if script is worker_mod._REFUND_CHARGE_LUA:
            return self.eval_charge(script, numkeys, *args)
        assert script is worker_mod._RENEW_LEASES_LUA
        assert numkeys == 1
        _stream, _group, consumer, *ids = args
        self.evals += 1
        lost = []
        for msg_id in ids:
            entry = self.pel.get(msg_id)
            if entry is not None and entry[0] == consumer:
                entry[1] = self.now_ms  # XCLAIM ... JUSTID: idle 0, no delivery
            else:
                lost.append(msg_id)
        return lost

    def xack(self, stream: str, group: str, msg_id: bytes) -> int:
        if not self.alive:
            return 0
        return 1 if self.pel.pop(msg_id, None) is not None else 0


def _key(msg_id: str) -> str:
    return crash_field(msg_id)


def _worker(redis: Any, name: str) -> IngestionWorker:
    worker = IngestionWorker(
        redis_client=redis,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
        consumer_name=name,
    )
    worker.process_event = MagicMock()  # type: ignore[method-assign]
    worker._record_dead_letter = MagicMock()  # type: ignore[method-assign]
    return worker


def _run_for(redis: _StreamRedis, holder: IngestionWorker, seconds: float, on_tick: Any) -> None:
    """Advance the clock *seconds* in heartbeat ticks, renewing *holder*'s
    leases each tick (as its heartbeat thread does), then ``on_tick(t)``."""
    elapsed = 0.0
    while elapsed < seconds:
        redis.advance(LEASE_RENEW_INTERVAL_S)
        elapsed += LEASE_RENEW_INTERVAL_S
        holder._renew_leases()
        on_tick(elapsed)


def _crash_retry_warnings(log: MagicMock) -> list[int]:
    return [
        call.kwargs["extra"]["crashes"]
        for call in log.warning.call_args_list
        if call.kwargs.get("extra", {}).get("telemetry_event") == "message_crash_retry"
    ]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_reclaim_min_idle_is_derived_from_the_renewal_interval() -> None:
    assert PENDING_RECLAIM_MIN_IDLE_MS == LEASE_RENEW_INTERVAL_S * LEASE_MISSED_RENEWALS * 1000
    # Several renewals may be missed (a Redis blip, a long GIL hold) before
    # a live worker's message looks abandoned.
    assert LEASE_MISSED_RENEWALS >= 3


# ---------------------------------------------------------------------------
# Overlapping workers (rolling deploy)
# ---------------------------------------------------------------------------


def test_overlapping_workers_process_each_message_once_with_no_charge() -> None:
    """Old task A is mid-message when new task B starts: B's startup reclaim
    and its periodic reclaims claim nothing, the batch-mate included."""
    redis = _StreamRedis(2)
    old, new = _worker(redis, "old"), _worker(redis, "new")
    reclaims: list[int] = []

    def process(event: dict[str, Any]) -> None:
        if event["document_id"] != "doc-1":
            return

        def tick(t: float) -> None:
            assert redis.kv == {_key("1-0"): 1}  # charged while it runs, once
            if t % 30 == 0:  # startup reclaim at 30 s, then periodic ones
                reclaims.append(new._reclaim_pending_messages())

        _run_for(redis, old, 120, tick)

    old.process_event.side_effect = process
    old._process_batch(batch_size=10, block_ms=0)

    assert reclaims == [0, 0, 0, 0]
    new.process_event.assert_not_called()
    assert old.process_event.call_count == 2  # message 1 and its batch-mate
    assert redis.kv == {}  # no crash charge left on anything
    assert redis.pel == {}  # both acked
    assert old._held == set()


def test_long_message_past_30s_is_not_stolen() -> None:
    """A 5-minute message keeps its lease; the old 30 s min-idle would have
    handed it to any other consumer's reclaim."""
    redis = _StreamRedis(1)
    holder, other = _worker(redis, "holder"), _worker(redis, "other")
    idles: list[int] = []

    def process(event: dict[str, Any]) -> None:
        def tick(t: float) -> None:
            idles.append(redis.idle_ms(b"1-0"))
            assert other._reclaim_pending_messages() == 0

        _run_for(redis, holder, 300, tick)

    holder.process_event.side_effect = process
    holder._process_batch(batch_size=10, block_ms=0)

    assert max(idles) < PENDING_RECLAIM_MIN_IDLE_MS
    other.process_event.assert_not_called()
    holder.process_event.assert_called_once()
    assert redis.kv == {}


def test_sigkilled_holder_lease_lapses_and_reclaim_charges_once() -> None:
    redis = _StreamRedis(1)
    dead, survivor = _worker(redis, "dead"), _worker(redis, "survivor")

    def oom(event: dict[str, Any]) -> None:
        redis.kill()  # SIGKILL: no Python runs, nothing settles
        raise _Killed

    dead.process_event.side_effect = oom
    with pytest.raises(_Killed):
        dead._process_batch(batch_size=10, block_ms=0)
    redis.restart()
    assert redis.kv == {_key("1-0"): 1}  # the charge of the death

    # No heartbeat any more.  Until the lease lapses nothing is claimed.
    redis.advance(PENDING_RECLAIM_MIN_IDLE_MS / 1000 - 1)
    assert survivor._reclaim_pending_messages() == 0

    redis.advance(1)
    with patch("ingestion.worker.logger") as log:
        assert survivor._reclaim_pending_messages() == 1

    survivor.process_event.assert_called_once()
    assert _crash_retry_warnings(log) == [1]  # exactly one death counted
    assert redis.kv == {}  # acked: count cleared
    assert redis.pel == {}


def test_sigkill_charges_accumulate_to_dead_letter_only_for_real_deaths() -> None:
    """#5050 kept: three real deaths dead-letter the message."""
    redis = _StreamRedis(1)
    redis.kv[_key("1-0")] = 3  # three earlier real deaths
    redis.xreadgroup("g", "dead", {}, count=10)
    redis.advance(PENDING_RECLAIM_MIN_IDLE_MS / 1000)
    survivor = _worker(redis, "survivor")
    assert survivor._reclaim_pending_messages() == 0  # dead-lettered, unprocessed
    survivor.process_event.assert_not_called()
    survivor._record_dead_letter.assert_called_once()
    assert redis.pel == {}


def test_repeated_unidentified_image_error_dead_letters_after_the_cap() -> None:
    """#5092: a corrupt image raises PIL's ``UnidentifiedImageError`` (an
    ``OSError``, so an exiting infrastructure error) on every attempt.  Each
    exit takes the crash charge back, but the handled-error cap still
    dead-letters it, through restarts and lease reclaims, instead of looping
    forever; its batch-mate is processed."""
    from PIL import UnidentifiedImageError

    redis = _StreamRedis(2)

    def corrupt_doc_1(event: dict[str, Any]) -> None:
        if event["document_id"] == "doc-1":
            raise UnidentifiedImageError("cannot identify image file")

    def restart(n: int) -> IngestionWorker:
        worker = _worker(redis, f"task-{n}")
        worker._get_connection = MagicMock()  # type: ignore[method-assign]
        worker.health_check = MagicMock()  # type: ignore[method-assign]  # DB is up
        worker.process_event.side_effect = corrupt_doc_1
        return worker

    with pytest.raises(worker_mod.InfrastructureError):
        restart(1)._process_batch(batch_size=10, block_ms=0)
    for n in range(2, worker_mod.MAX_MESSAGE_HANDLED_ERRORS + 1):
        redis.advance(PENDING_RECLAIM_MIN_IDLE_MS / 1000)  # the dead task's lease lapses
        worker = restart(n)
        if n < worker_mod.MAX_MESSAGE_HANDLED_ERRORS:
            with pytest.raises(worker_mod.InfrastructureError):
                worker._reclaim_pending_messages()
            worker._record_dead_letter.assert_not_called()
        else:
            assert worker._reclaim_pending_messages() == 1  # doc-2
    worker._record_dead_letter.assert_called_once()
    assert worker._record_dead_letter.call_args.kwargs["cause"] == (
        "repeated_error:UnidentifiedImageError"
    )
    assert redis.pel == {}
    assert redis.kv == {}


def test_after_sigterm_only_the_in_flight_message_keeps_its_lease() -> None:
    """The stopping task's unstarted batch-mates lapse and are reclaimed
    uncharged; its in-flight message is not."""
    redis = _StreamRedis(2)
    old, new = _worker(redis, "old"), _worker(redis, "new")
    reclaimed: list[int] = []

    def process(event: dict[str, Any]) -> None:
        old.request_shutdown()  # SIGTERM mid-message

        def tick(t: float) -> None:
            if t == 70:
                with patch("ingestion.worker.logger") as log:
                    reclaimed.append(new._reclaim_pending_messages())
                assert _crash_retry_warnings(log) == []  # no blame

        _run_for(redis, old, 90, tick)

    old.process_event.side_effect = process
    old._process_batch(batch_size=10, block_ms=0)

    assert reclaimed == [1]
    assert [c.args[0]["document_id"] for c in old.process_event.call_args_list] == ["doc-1"]
    assert [c.args[0]["document_id"] for c in new.process_event.call_args_list] == ["doc-2"]
    assert redis.kv == {}
    assert redis.pel == {}


def test_lost_lease_is_logged_and_the_message_is_not_started() -> None:
    """A holder that stopped renewing long enough to lose a batch-mate to
    another consumer does not process it too."""
    redis = _StreamRedis(2)
    holder = _worker(redis, "holder")

    def process(event: dict[str, Any]) -> None:
        redis.pel[b"2-0"][0] = "thief"
        with patch("ingestion.worker.logger") as log:
            assert holder._renew_leases() == 1
        lost = [
            call
            for call in log.warning.call_args_list
            if call.kwargs["extra"]["telemetry_event"] == "message_lease_lost"
        ]
        assert len(lost) == 1
        assert lost[0].kwargs["extra"]["in_flight"] is False

    holder.process_event.side_effect = process
    holder._process_batch(batch_size=10, block_ms=0)

    holder.process_event.assert_called_once()
    assert redis.pel == {b"2-0": ["thief", 0, 1]}  # left to its new owner


def test_reclaimed_message_lost_before_start_is_skipped() -> None:
    redis = _StreamRedis(2)
    redis.xreadgroup("g", "dead", {}, count=10)
    redis.advance(PENDING_RECLAIM_MIN_IDLE_MS / 1000)
    worker = _worker(redis, "w")

    def process(event: dict[str, Any]) -> None:
        redis.pel[b"2-0"][0] = "thief"
        worker._renew_leases()

    worker.process_event.side_effect = process
    assert worker._reclaim_pending_messages() == 1
    worker.process_event.assert_called_once()


def _explode_on_first(worker: IngestionWorker) -> None:
    """Make message 1-0 raise an ordinary exception out of
    ``_process_message`` (as a failed final XACK or a bad payload decode
    does); the consumer loop logs it and carries on."""
    real = worker._process_message

    def process_message(msg_id: bytes, data: Any) -> bool:
        if msg_id == b"1-0":
            raise RuntimeError("xack failed")
        return real(msg_id, data)

    worker._process_message = process_message  # type: ignore[method-assign]


def _assert_leftover_reclaimed_uncharged(redis: _StreamRedis) -> None:
    redis.advance(PENDING_RECLAIM_MIN_IDLE_MS / 1000)
    rescuer = _worker(redis, "rescuer")
    with patch("ingestion.worker.logger") as log:
        assert rescuer._reclaim_pending_messages() == 2
    assert _crash_retry_warnings(log) == []
    assert [c.args[0]["document_id"] for c in rescuer.process_event.call_args_list] == [
        "doc-1",
        "doc-2",
    ]
    assert redis.pel == {}


def test_batch_exception_stops_renewing_the_unstarted_rest() -> None:
    redis = _StreamRedis(2)
    holder = _worker(redis, "holder")
    _explode_on_first(holder)
    with pytest.raises(RuntimeError):
        holder._process_batch(batch_size=10, block_ms=0)
    assert holder._held == set()
    for _ in range(10):  # the heartbeat keeps running in a live worker
        redis.advance(LEASE_RENEW_INTERVAL_S)
        holder._renew_leases()
    _assert_leftover_reclaimed_uncharged(redis)


def test_reclaim_exception_stops_renewing_the_unstarted_rest() -> None:
    redis = _StreamRedis(2)
    redis.xreadgroup("g", "dead", {}, count=10)
    redis.advance(PENDING_RECLAIM_MIN_IDLE_MS / 1000)
    holder = _worker(redis, "holder")
    _explode_on_first(holder)
    with pytest.raises(RuntimeError):
        holder._reclaim_pending_messages()
    assert holder._held == set()
    for _ in range(10):
        redis.advance(LEASE_RENEW_INTERVAL_S)
        holder._renew_leases()
    _assert_leftover_reclaimed_uncharged(redis)


def test_failed_ack_keeps_the_lease_until_the_batch_ends() -> None:
    redis = _StreamRedis(1)
    holder = _worker(redis, "holder")
    holder._hold([b"1-0"])
    redis.xreadgroup("g", "holder", {}, count=10)
    redis.xack = MagicMock(side_effect=ConnectionError("down"))  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        holder._ack(b"1-0")
    assert holder._holds(b"1-0")


# ---------------------------------------------------------------------------
# Renewal edge cases
# ---------------------------------------------------------------------------


def test_renew_with_nothing_held_makes_no_call() -> None:
    redis = _StreamRedis(0)
    assert _worker(redis, "w")._renew_leases() == 0
    assert redis.evals == 0


def test_renew_failure_is_logged_not_raised() -> None:
    redis = MagicMock()
    redis.eval.side_effect = ConnectionError("down")
    worker = _worker(redis, "w")
    worker._hold([b"1-0"])
    with patch("ingestion.worker.logger") as log:
        assert worker._renew_leases() == 0
    log.warning.assert_called_once()
    assert worker._holds(b"1-0")  # kept: the next renewal retries


def test_renew_ignores_an_unexpected_reply() -> None:
    redis = MagicMock()  # eval returns a MagicMock, not a list
    worker = _worker(redis, "w")
    worker._hold([b"1-0"])
    assert worker._renew_leases() == 1
    assert worker._holds(b"1-0")


def test_renew_accepts_str_ids() -> None:
    redis = MagicMock()
    redis.eval.return_value = ["1-0"]
    worker = _worker(redis, "w")
    worker._hold([b"1-0", b"2-0"])
    assert worker._renew_leases() == 1
    assert worker._held == {b"2-0"}


def test_released_message_stops_renewing() -> None:
    """The #5069 grace-period release hands the message back for reclaim."""
    redis = _StreamRedis(1)
    worker = _worker(redis, "w")
    worker._hold([b"1-0"])
    redis.kv[_key("1-0")] = 1
    worker._release_in_flight(b"1-0")
    assert redis.kv == {}  # charge refunded (#5069), never below 0
    assert not worker._holds(b"1-0")


def test_exiting_exception_stops_renewing() -> None:
    redis = _StreamRedis(1)
    worker = _worker(redis, "w")
    worker.process_event.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        worker._process_batch(batch_size=10, block_ms=0)
    assert worker._held == set()
    assert redis.kv == {}  # #5055: not a crash


# ---------------------------------------------------------------------------
# The heartbeat thread
# ---------------------------------------------------------------------------


def test_run_renews_leases_from_a_heartbeat_thread() -> None:
    redis = _StreamRedis(1)
    worker = _worker(redis, "w")
    for name in (
        "health_check",
        "_ensure_consumer_group",
        "_cleanup_stale_consumers",
        "_maybe_trim_consumed_entries",
        "close",
    ):
        setattr(worker, name, MagicMock())
    worker._shutdown = ShutdownCoordinator(worker._release_in_flight)
    renewed: list[bool] = []

    def process(event: dict[str, Any]) -> None:
        redis.advance(45)
        deadline = time.monotonic() + 5
        while redis.idle_ms(b"1-0") and time.monotonic() < deadline:
            time.sleep(0.005)
        renewed.append(redis.idle_ms(b"1-0") == 0)
        worker.request_shutdown()

    worker.process_event.side_effect = process
    with patch.object(worker_mod, "LEASE_RENEW_INTERVAL_S", 0.01):
        worker.run(block_ms=0)

    assert renewed == [True]
    assert worker._lease_thread is None  # stopped with run()
    assert redis.pel == {}


def test_stop_heartbeat_without_start_is_a_no_op() -> None:
    worker = _worker(_StreamRedis(0), "w")
    worker._stop_lease_heartbeat()
    assert worker._lease_thread is None


# ---------------------------------------------------------------------------
# The Lua script against a real Redis
# ---------------------------------------------------------------------------


@pytest.fixture
def real_redis() -> Iterator[Any]:
    url = os.environ.get("JUDGEMIND_TEST_REDIS_URL")
    if not url:
        pytest.skip("JUDGEMIND_TEST_REDIS_URL not set")
    import redis as redis_lib

    client = redis_lib.Redis.from_url(url, decode_responses=False)
    stream = f"test-5091-{uuid.uuid4().hex}"
    group = "test-5091"
    client.xgroup_create(stream, group, id="0", mkstream=True)
    with (
        patch.object(worker_mod, "STREAM_DOCUMENT_CAPTURED", stream),
        patch.object(worker_mod, "CONSUMER_GROUP", group),
    ):
        yield client, stream, group
    client.delete(stream)


class TestRealRedis:
    def test_renewal_resets_idle_without_counting_a_delivery(self, real_redis: Any) -> None:
        client, stream, group = real_redis
        msg_id = client.xadd(stream, {b"data": b"{}"})
        holder, other = _worker(client, "holder"), _worker(client, "other")
        client.xreadgroup(group, "holder", {stream: ">"}, count=1)
        holder._hold([msg_id])
        time.sleep(0.3)

        assert holder._renew_leases() == 1
        [entry] = client.xpending_range(stream, group, "-", "+", 1)
        assert entry["consumer"] == b"holder"
        assert entry["time_since_delivered"] < 200
        assert entry["times_delivered"] == 1
        # A reclaim with a min-idle the renewal just reset claims nothing.
        assert client.xautoclaim(stream, group, "other", min_idle_time=250)[1] == []
        assert other._renew_leases() == 0

    def test_renewal_never_takes_back_a_reclaimed_message(self, real_redis: Any) -> None:
        client, stream, group = real_redis
        msg_id = client.xadd(stream, {b"data": b"{}"})
        holder = _worker(client, "holder")
        client.xreadgroup(group, "holder", {stream: ">"}, count=1)
        holder._hold([msg_id])
        client.xclaim(stream, group, "thief", 0, [msg_id], justid=True)

        assert holder._renew_leases() == 0
        assert not holder._holds(msg_id)
        [entry] = client.xpending_range(stream, group, "-", "+", 1)
        assert entry["consumer"] == b"thief"

    def test_renewal_skips_an_acked_message(self, real_redis: Any) -> None:
        client, stream, group = real_redis
        msg_id = client.xadd(stream, {b"data": b"{}"})
        holder = _worker(client, "holder")
        client.xreadgroup(group, "holder", {stream: ">"}, count=1)
        holder._hold([msg_id])
        client.xack(stream, group, msg_id)

        assert holder._renew_leases() == 0
        assert client.xpending(stream, group)["pending"] == 0

    def test_charges_have_no_ttl_and_refunds_never_go_negative(self, real_redis: Any) -> None:
        """#5092: the charge hash is never evictable under volatile-lru (no
        TTL), and ``_REFUND_CHARGE_LUA`` clamps at 0 and deletes the field."""
        client, stream, group = real_redis
        key = f"{stream}-charges"
        with patch.object(worker_mod, "MESSAGE_CHARGES_KEY", key):
            try:
                msg_id = client.xadd(stream, {b"data": b"{}"})
                client.xreadgroup(group, "w", {stream: ">"}, count=1)
                worker = _worker(client, "w")
                assert worker._begin_processing(msg_id) == 0
                assert worker._begin_processing(msg_id) == 1
                assert client.ttl(key) == -1  # no TTL
                worker._refund_crash(msg_id)
                assert client.hget(key, crash_field(msg_id.decode())) == b"1"
                worker._refund_crash(msg_id)
                assert client.hexists(key, crash_field(msg_id.decode())) == 0
                worker._refund_crash(msg_id)  # no charge left: no -1
                assert client.hexists(key, crash_field(msg_id.decode())) == 0

                # The sweep keeps a pending message's count, drops an acked one's.
                worker._begin_processing(msg_id)
                client.hset(key, "crashes:1-1", 2)
                assert worker._sweep_message_charges() == 1
                assert client.hkeys(key) == [crash_field(msg_id.decode()).encode()]
                client.xack(stream, group, msg_id)
                assert worker._sweep_message_charges() == 1
                assert client.exists(key) == 0
            finally:
                client.delete(key)
