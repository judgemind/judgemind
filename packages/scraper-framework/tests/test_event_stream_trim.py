"""Tests for consumer-driven stream trimming (#4474).

The ``document.captured`` stream was bounded only by the producer's
``MAXLEN ~ 50000``.  The ingestion worker acked every entry but nothing ever
removed acked entries, so the stream held 50 000 already-consumed events
(~22 KB each, 1.1 GB) and filled the 1.03 GB ElastiCache node; every XADD was
then rejected with ``used memory > 'maxmemory'``.

These tests use a small in-memory stream model that implements the Redis
semantics the trim relies on (XINFO GROUPS, XPENDING summary, XTRIM MINID),
so they check *which entries survive*, not only which arguments were passed.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
import redis

from framework.events import (
    DEFAULT_HEALTH_STREAM_MAXLEN,
    DEFAULT_STREAM_MAXLEN,
    DEFAULT_TRIM_LIMIT,
    STREAM_DOCUMENT_CAPTURED,
    STREAM_SCRAPER_HEALTH,
    EventBus,
    consumed_floor_id,
    trim_consumed_entries,
)
from framework.models import ScraperHealthEvent
from ingestion import worker as worker_mod
from ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# In-memory stream model
# ---------------------------------------------------------------------------


def _sid(n: int) -> str:
    return f"{1000 + n}-0"


def _key(stream_id: str) -> tuple[int, int]:
    ms, seq = stream_id.split("-")
    return int(ms), int(seq)


class FakeStreamRedis:
    """One Redis stream with consumer groups, modelled just enough to trim.

    ``groups`` maps a group name to ``(last_delivered_index, pending_indexes)``
    where indexes refer to the entries created in ``__init__``.  ``as_bytes`` makes the
    replies look like a ``decode_responses=False`` client (the worker's).
    """

    def __init__(self, n_entries: int, *, as_bytes: bool = False) -> None:
        self.entries = [_sid(i) for i in range(n_entries)]
        self.groups: dict[str, tuple[int | None, list[int]]] = {}
        self.as_bytes = as_bytes
        self.exists = True
        self.xtrim_calls: list[dict] = []

    def _v(self, value: str) -> str | bytes:
        return value.encode() if self.as_bytes else value

    def xinfo_groups(self, stream: str) -> list[dict]:
        if not self.exists:
            raise redis.ResponseError("ERR no such key")
        out = []
        for name, (last, pending) in self.groups.items():
            out.append(
                {
                    "name": self._v(name),
                    "consumers": 1,
                    "pending": len(pending),
                    "last-delivered-id": self._v("0-0" if last is None else _sid(last)),
                }
            )
        return out

    def xpending(self, stream: str, group: str) -> dict:
        _, pending = self.groups[group]
        if not pending:
            return {"pending": 0, "min": None, "max": None, "consumers": []}
        ids = sorted(_sid(i) for i in pending)
        return {"pending": len(ids), "min": self._v(ids[0]), "max": self._v(ids[-1])}

    def xtrim(self, stream: str, *, minid: str, approximate: bool, limit: int) -> int:
        self.xtrim_calls.append({"minid": minid, "approximate": approximate, "limit": limit})
        doomed = [e for e in self.entries if _key(e) < _key(minid)][:limit]
        self.entries = [e for e in self.entries if e not in doomed]
        return len(doomed)

    def xlen(self, stream: str) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# consumed_floor_id
# ---------------------------------------------------------------------------


class TestConsumedFloorId:
    def test_floor_is_last_delivered_when_nothing_pending(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["ingestion-workers"] = (6, [])
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) == _sid(6)

    def test_floor_is_oldest_pending_entry(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["ingestion-workers"] = (8, [3, 5])
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) == _sid(3)

    def test_floor_is_minimum_across_groups(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["fast"] = (9, [])
        r.groups["slow"] = (2, [])
        r.groups["stuck"] = (9, [4])
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) == _sid(2)

    def test_no_groups_returns_none(self) -> None:
        assert consumed_floor_id(FakeStreamRedis(10), STREAM_SCRAPER_HEALTH) is None

    def test_missing_stream_returns_none(self) -> None:
        r = FakeStreamRedis(0)
        r.exists = False
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) is None

    def test_bytes_replies_are_decoded(self) -> None:
        r = FakeStreamRedis(10, as_bytes=True)
        r.groups["ingestion-workers"] = (8, [4])
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) == _sid(4)

    def test_bytes_keys_are_accepted(self) -> None:
        r = MagicMock()
        r.xinfo_groups.return_value = [
            {b"name": b"g", b"pending": 0, b"last-delivered-id": b"1700-3"}
        ]
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) == "1700-3"

    def test_floor_compares_ids_numerically_not_lexically(self) -> None:
        # "999-0" sorts after "1000-0" as a string but is the older entry.
        r = MagicMock()
        r.xinfo_groups.return_value = [
            {"name": "a", "pending": 0, "last-delivered-id": "1000-0"},
            {"name": "b", "pending": 0, "last-delivered-id": "999-0"},
        ]
        assert consumed_floor_id(r, STREAM_DOCUMENT_CAPTURED) == "999-0"


# ---------------------------------------------------------------------------
# trim_consumed_entries
# ---------------------------------------------------------------------------


class TestTrimConsumedEntries:
    def test_trims_only_acknowledged_entries(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["ingestion-workers"] = (6, [])
        trimmed, floor = trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED)
        assert (trimmed, floor) == (6, _sid(6))
        # Entry 6 (last delivered) and the undelivered 7-9 survive.
        assert r.entries == [_sid(i) for i in range(6, 10)]

    def test_never_trims_pending_entries(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["ingestion-workers"] = (9, [2, 7])
        trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED)
        assert _sid(2) in r.entries
        assert _sid(7) in r.entries
        assert r.entries[0] == _sid(2)

    def test_never_trims_entries_a_slow_group_still_needs(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["fast"] = (9, [])
        r.groups["slow"] = (3, [])
        trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED)
        assert r.entries == [_sid(i) for i in range(3, 10)]

    def test_uses_approximate_minid_with_limit(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["ingestion-workers"] = (6, [])
        trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED)
        assert r.xtrim_calls == [
            {"minid": _sid(6), "approximate": True, "limit": DEFAULT_TRIM_LIMIT}
        ]

    def test_limit_bounds_work_per_call_and_repeats_converge(self) -> None:
        r = FakeStreamRedis(25)
        r.groups["ingestion-workers"] = (24, [])
        assert trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED, limit=10)[0] == 10
        assert trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED, limit=10)[0] == 10
        assert trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED, limit=10)[0] == 4
        assert trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED, limit=10)[0] == 0
        assert r.entries == [_sid(24)]

    def test_no_groups_is_a_noop(self) -> None:
        r = FakeStreamRedis(10)
        assert trim_consumed_entries(r, STREAM_SCRAPER_HEALTH) == (0, None)
        assert r.xtrim_calls == []
        assert len(r.entries) == 10

    def test_group_that_never_read_is_a_noop(self) -> None:
        r = FakeStreamRedis(10)
        r.groups["new"] = (None, [])
        assert trim_consumed_entries(r, STREAM_DOCUMENT_CAPTURED) == (0, "0-0")
        assert r.xtrim_calls == []


# ---------------------------------------------------------------------------
# Producer-side MAXLEN backstops
# ---------------------------------------------------------------------------


def _health() -> ScraperHealthEvent:
    return ScraperHealthEvent(
        producer_id="test",
        scraper_id="test",
        success=True,
        records_captured=1,
        response_time_seconds=1.0,
    )


class TestProducerCaps:
    def test_document_cap_fits_cache_at_measured_entry_size(self) -> None:
        # ~22 KB/entry measured on dev (#4474); the backstop must leave the
        # 1.03 GB cache.t4g.small at least half free.
        assert DEFAULT_STREAM_MAXLEN * 22_000 < 1_103_269_725 / 2

    def test_health_stream_uses_its_own_cap(self) -> None:
        r = MagicMock()
        r.xadd.return_value = b"1-0"
        EventBus(r).emit_health(_health())
        assert r.xadd.call_args.kwargs["maxlen"] == DEFAULT_HEALTH_STREAM_MAXLEN
        assert r.xadd.call_args.kwargs["approximate"] is True

    def test_health_cap_env_override(self) -> None:
        r = MagicMock()
        r.xadd.return_value = b"1-0"
        with patch.dict(os.environ, {"HEALTH_STREAM_MAXLEN": "42"}):
            bus = EventBus(r)
        bus.emit_health(_health())
        assert r.xadd.call_args.kwargs["maxlen"] == 42

    def test_health_cap_constructor_override(self) -> None:
        r = MagicMock()
        r.xadd.return_value = b"1-0"
        EventBus(r, health_maxlen=7).emit_health(_health())
        assert r.xadd.call_args.kwargs["maxlen"] == 7


# ---------------------------------------------------------------------------
# IngestionWorker periodic trim
# ---------------------------------------------------------------------------


def _worker(redis_client: object) -> IngestionWorker:
    return IngestionWorker(
        redis_client=redis_client,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )


class TestWorkerPeriodicTrim:
    def test_first_call_trims_and_logs_stream_length(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        r = FakeStreamRedis(10)
        r.groups[worker_mod.CONSUMER_GROUP] = (9, [])
        w = _worker(r)
        with caplog.at_level(logging.INFO, logger="ingestion.worker"):
            assert w._maybe_trim_consumed_entries() == 9
        rec = next(x for x in caplog.records if x.getMessage() == "Event stream trim pass")
        assert rec.trimmed == 9
        assert rec.stream_length == 1
        assert rec.floor_id == _sid(9)

    def test_trim_is_rate_limited(self) -> None:
        r = FakeStreamRedis(10)
        r.groups[worker_mod.CONSUMER_GROUP] = (5, [])
        w = _worker(r)
        with patch.object(worker_mod.time, "monotonic", side_effect=[100.0, 130.0, 161.0]):
            w._maybe_trim_consumed_entries()
            w._maybe_trim_consumed_entries()  # 30 s later: skipped
            w._maybe_trim_consumed_entries()  # 61 s after the first: runs
        assert len(r.xtrim_calls) == 2

    def test_trim_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        r = MagicMock()
        r.xinfo_groups.side_effect = redis.ConnectionError("down")
        w = _worker(r)
        with caplog.at_level(logging.WARNING, logger="ingestion.worker"):
            assert w._maybe_trim_consumed_entries() == 0
        assert any("Stream trim failed" in x.getMessage() for x in caplog.records)

    def test_run_loop_trims(self) -> None:
        w = _worker(MagicMock())
        calls: list[str] = []

        def fake_batch(**_kwargs: object) -> None:
            if calls:
                raise KeyboardInterrupt
            calls.append("batch")

        with (
            patch.object(w, "health_check"),
            patch.object(w, "_ensure_consumer_group"),
            patch.object(w, "_cleanup_stale_consumers"),
            patch.object(w, "_reclaim_pending_messages", return_value=0),
            patch.object(w, "_process_batch", side_effect=fake_batch),
            patch.object(w, "_maybe_trim_consumed_entries") as trim,
            patch.object(w, "close"),
        ):
            w.run()
        trim.assert_called()
