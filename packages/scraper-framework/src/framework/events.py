"""Redis Streams event emission per Architecture Spec §2.1."""

from __future__ import annotations

import json
import logging

import redis

from .models import CapturedDocument, DocumentCapturedEvent, ScraperHealthEvent

logger = logging.getLogger(__name__)

STREAM_DOCUMENT_CAPTURED = "document.captured"
STREAM_SCRAPER_HEALTH = "scraper.health"


class EventBus:
    """Thin wrapper around Redis Streams for event emission."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

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
        msg_id = self._redis.xadd(STREAM_DOCUMENT_CAPTURED, {"data": json.dumps(payload)})
        logger.debug("Emitted %s → %s", STREAM_DOCUMENT_CAPTURED, msg_id)
        return msg_id

    def emit_health(self, event: ScraperHealthEvent) -> str:
        payload = event.model_dump(mode="json")
        msg_id = self._redis.xadd(STREAM_SCRAPER_HEALTH, {"data": json.dumps(payload)})
        logger.debug("Emitted %s → %s", STREAM_SCRAPER_HEALTH, msg_id)
        return msg_id
