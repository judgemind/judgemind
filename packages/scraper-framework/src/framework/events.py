"""Redis Streams event emission per Architecture Spec §2.1."""

from __future__ import annotations

import json
import logging
import os

import redis

from .models import CapturedDocument, DocumentCapturedEvent, ScraperHealthEvent

logger = logging.getLogger(__name__)

STREAM_DOCUMENT_CAPTURED = "document.captured"
STREAM_SCRAPER_HEALTH = "scraper.health"

#: Cap on ``document.captured`` length, checked before every XADD.
#:
#: This is NOT the normal retention mechanism.  The ingestion worker trims
#: entries every consumer group has already acknowledged (see
#: :func:`trim_consumed_entries`), so in steady state the stream holds only
#: the unconsumed backlog.  The cap only bites when the worker is down long
#: enough for the backlog to reach it.
#:
#: At the cap the producer never trims unconsumed events (#5009).  It first
#: trims acknowledged entries itself; if the stream is still at the cap, the
#: XADD is refused with :class:`StreamBacklogFull`.  The scraper counts that
#: as a failed emit and the run records ``success=False``, so the loss is
#: visible and the archived key is named in ``scraper_runs.error_details``.
#: (Until #5009 every XADD carried ``MAXLEN ~``, which silently dropped the
#: oldest unconsumed events once the backlog reached the cap.)
#:
#: Size it in bytes, not counts.  Measured on dev in 2026-11 (#4474), entries
#: are ~22 KB on average (p50 14 KB, p90 60 KB, max 278 KB) because the
#: payload carries ``ruling_text`` and ``ruling_text_html``.  The old 50 000
#: cap with no consumer-side trimming kept 50 000 already-consumed entries
#: (1.1 GB) and filled the 1.03 GB cache.t4g.small, after which every XADD
#: was rejected with OOM.  20 000 × 22 KB ≈ 440 MB, under half the node.
#: Override at runtime with the ``STREAM_MAXLEN`` environment variable.
DEFAULT_STREAM_MAXLEN = 20_000

#: Cap on ``scraper.health``.  Nothing consumes this stream (no consumer
#: group), so the cap is its only retention.  Entries are ~0.5 KB, so 1 000
#: entries ≈ 0.5 MB — a few weeks of per-run health events.
#: Override at runtime with the ``HEALTH_STREAM_MAXLEN`` environment variable.
DEFAULT_HEALTH_STREAM_MAXLEN = 1_000

#: Upper bound on entries one :func:`trim_consumed_entries` call removes, so a
#: large backlog is freed over several calls instead of one long XTRIM that
#: blocks Redis (a single exact XTRIM of 50 000 entries outlasted the
#: client's 5 s socket timeout during the #4474 recovery).
DEFAULT_TRIM_LIMIT = 1_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw.strip():
        return int(raw)
    return default


def _stream_maxlen() -> int:
    """Return the configured ``document.captured`` MAXLEN (env var or default)."""
    return _env_int("STREAM_MAXLEN", DEFAULT_STREAM_MAXLEN)


def _health_stream_maxlen() -> int:
    """Return the configured ``scraper.health`` MAXLEN (env var or default)."""
    return _env_int("HEALTH_STREAM_MAXLEN", DEFAULT_HEALTH_STREAM_MAXLEN)


def _as_str(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse_stream_id(stream_id: str) -> tuple[int, int]:
    ms, _, seq = stream_id.partition("-")
    return int(ms), int(seq or 0)


def _get(mapping: dict, key: str) -> object:
    """Read ``key`` from a redis-py reply that may use str or bytes keys."""
    if key in mapping:
        return mapping[key]
    return mapping.get(key.encode())


def consumed_floor_id(redis_client: redis.Redis, stream: str) -> str | None:
    """Return the lowest entry ID that some consumer group may still need.

    Every entry with an ID below the returned value has been delivered to and
    acknowledged by every consumer group on ``stream``, so ``XTRIM MINID`` at
    this ID drops only dead weight.  For each group the floor is its oldest
    pending (delivered but unacked) entry, or else its ``last-delivered-id``;
    the stream floor is the minimum across groups.

    Returns ``None`` when the stream has no consumer groups (nothing tracks
    consumption, so nothing is provably safe to trim) or does not exist.
    """
    try:
        groups = redis_client.xinfo_groups(stream)
    except redis.ResponseError:
        return None  # stream does not exist
    if not groups:
        return None

    floors: list[str] = []
    for group in groups:
        floor = _as_str(_get(group, "last-delivered-id"))
        if int(_get(group, "pending") or 0) > 0:
            summary = redis_client.xpending(stream, _as_str(_get(group, "name")))
            oldest = summary.get("min") if isinstance(summary, dict) else None
            if oldest is not None:
                oldest_str = _as_str(oldest)
                if _parse_stream_id(oldest_str) < _parse_stream_id(floor):
                    floor = oldest_str
        floors.append(floor)
    return min(floors, key=_parse_stream_id)


def trim_consumed_entries(
    redis_client: redis.Redis, stream: str, *, limit: int = DEFAULT_TRIM_LIMIT
) -> tuple[int, str | None]:
    """Trim entries every consumer group has already acknowledged.

    Uses ``XTRIM <stream> MINID ~ <floor> LIMIT <limit>``: approximate
    trimming frees whole radix-tree nodes, and ``LIMIT`` bounds the work done
    per call so Redis is never blocked for long.  Unconsumed and pending
    entries are never removed.

    Returns ``(entries_trimmed, floor_id)``.  ``floor_id`` is ``None`` (and
    nothing is trimmed) when the stream has no consumer groups.
    """
    floor = consumed_floor_id(redis_client, stream)
    if floor is None or floor == "0-0":
        return 0, floor
    trimmed = redis_client.xtrim(stream, minid=floor, approximate=True, limit=limit)
    return int(trimmed or 0), floor


class StreamBacklogFull(RuntimeError):  # noqa: N818
    """``document.captured`` already holds ``maxlen`` unconsumed events.

    Raised instead of an XADD that would push the oldest unconsumed events
    out of the stream (#5009).  Not retryable within a run: the backlog
    drains only when the ingestion worker consumes it.
    """


class EventBus:
    """Thin wrapper around Redis Streams for event emission."""

    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        maxlen: int | None = None,
        health_maxlen: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._maxlen = maxlen if maxlen is not None else _stream_maxlen()
        self._health_maxlen = (
            health_maxlen if health_maxlen is not None else _health_stream_maxlen()
        )

    def emit_document_captured(
        self, doc: CapturedDocument, producer_id: str, correlation_id: str | None = None
    ) -> str:
        event = DocumentCapturedEvent(
            producer_id=producer_id,
            correlation_id=correlation_id or doc.document_id,
            document_id=doc.document_id,
            scraper_id=doc.scraper_id,
            state=doc.state,
            county=doc.county,
            court=doc.court,
            source_url=doc.source_url,
            content_format=doc.content_format,
            content_hash=doc.content_hash,
            s3_key=doc.s3_key,
            s3_bucket=doc.s3_bucket,
            ruling_text=doc.ruling_text,
            ruling_text_html=doc.ruling_text_html,
            outcome=doc.outcome,
            motion_type=doc.motion_type,
            case_number=doc.case_number,
            case_title=doc.case_title,
            courthouse=doc.courthouse,
            department=doc.department,
            judge_name=doc.judge_name,
            hearing_date=doc.hearing_date,
            capture_timestamp=doc.capture_timestamp,
            parties=doc.parties,
            extra=doc.extra,
        )
        # Use model_dump(mode="json") instead of json.loads(model_dump_json())
        # to avoid Pydantic v2 serialization edge cases with
        # `from __future__ import annotations` and `datetime | None` unions.
        # model_dump(mode="json") returns a dict with all values already
        # JSON-compatible (datetimes as ISO strings, enums as values, etc.)
        # without the redundant JSON string round-trip.  (#191)
        payload = event.model_dump(mode="json")
        self._ensure_backlog_room()
        # No MAXLEN here: an approximate MAXLEN trims the oldest entries
        # whether or not a consumer has read them (#5009).
        msg_id = self._redis.xadd(STREAM_DOCUMENT_CAPTURED, {"data": json.dumps(payload)})
        logger.debug("Emitted %s → %s", STREAM_DOCUMENT_CAPTURED, msg_id)
        return msg_id

    def _ensure_backlog_room(self) -> None:
        """Raise :class:`StreamBacklogFull` when the stream is at its cap.

        XLEN is O(1), so the common case costs one round trip.  At the cap,
        acknowledged entries are trimmed first (``MINID`` at the consumed
        floor, never past it); only a backlog that is still at the cap after
        that is refused.
        """
        length = int(self._redis.xlen(STREAM_DOCUMENT_CAPTURED) or 0)
        if length < self._maxlen:
            return
        trimmed, floor = trim_consumed_entries(self._redis, STREAM_DOCUMENT_CAPTURED)
        length = int(self._redis.xlen(STREAM_DOCUMENT_CAPTURED) or 0)
        if length < self._maxlen:
            logger.info(
                "document.captured at cap: trimmed %d acknowledged entries (floor %s)",
                trimmed,
                floor,
            )
            return
        logger.error(
            "document.captured backlog full: %d entries >= cap %d after trimming %d "
            "acknowledged entries (floor %s); refusing XADD rather than dropping "
            "unconsumed events",
            length,
            self._maxlen,
            trimmed,
            floor,
        )
        raise StreamBacklogFull(
            f"{STREAM_DOCUMENT_CAPTURED} backlog full: {length} unconsumed entries "
            f">= cap {self._maxlen} (ingestion worker not consuming)"
        )

    def emit_health(self, event: ScraperHealthEvent) -> str:
        payload = event.model_dump(mode="json")
        msg_id = self._redis.xadd(
            STREAM_SCRAPER_HEALTH,
            {"data": json.dumps(payload)},
            maxlen=self._health_maxlen,
            approximate=True,
        )
        logger.debug("Emitted %s → %s", STREAM_SCRAPER_HEALTH, msg_id)
        return msg_id
