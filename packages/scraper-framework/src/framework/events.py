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

#: Default maximum stream length.  Each event is ~2–5 KB, so 10 000 entries
#: ≈ 20–50 MB — well within the 512 MB available on cache.t4g.micro.
#: Override at runtime with the ``STREAM_MAXLEN`` environment variable.
DEFAULT_STREAM_MAXLEN = 10_000


def _stream_maxlen() -> int:
    """Return the configured stream MAXLEN (from env var or default)."""
    raw = os.environ.get("STREAM_MAXLEN", "")
    if raw.strip():
        return int(raw)
    return DEFAULT_STREAM_MAXLEN


class EventBus:
    """Thin wrapper around Redis Streams for event emission."""

    def __init__(self, redis_client: redis.Redis, *, maxlen: int | None = None) -> None:
        self._redis = redis_client
        self._maxlen = maxlen if maxlen is not None else _stream_maxlen()

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
            outcome=doc.outcome,
            motion_type=doc.motion_type,
            case_number=doc.case_number,
            courthouse=doc.courthouse,
            department=doc.department,
            judge_name=doc.judge_name,
            hearing_date=doc.hearing_date,
            capture_timestamp=doc.capture_timestamp,
        )
        # Use model_dump(mode="json") instead of json.loads(model_dump_json())
        # to avoid Pydantic v2 serialization edge cases with
        # `from __future__ import annotations` and `datetime | None` unions.
        # model_dump(mode="json") returns a dict with all values already
        # JSON-compatible (datetimes as ISO strings, enums as values, etc.)
        # without the redundant JSON string round-trip.  (#191)
        payload = event.model_dump(mode="json")
        msg_id = self._redis.xadd(
            STREAM_DOCUMENT_CAPTURED,
            {"data": json.dumps(payload)},
            maxlen=self._maxlen,
            approximate=True,
        )
        logger.debug("Emitted %s → %s", STREAM_DOCUMENT_CAPTURED, msg_id)
        return msg_id

    def emit_health(self, event: ScraperHealthEvent) -> str:
        payload = event.model_dump(mode="json")
        msg_id = self._redis.xadd(
            STREAM_SCRAPER_HEALTH,
            {"data": json.dumps(payload)},
            maxlen=self._maxlen,
            approximate=True,
        )
        logger.debug("Emitted %s → %s", STREAM_SCRAPER_HEALTH, msg_id)
        return msg_id
