"""RedisEventBus — REDIS_URL-aware wrapper around EventBus.

When REDIS_URL is set, emits events to Redis Streams via the underlying
EventBus.  When REDIS_URL is absent (local dev, tests), all emit calls
are silently skipped so scrapers can run without a Redis instance.

Usage:
    bus = RedisEventBus.from_env()   # reads REDIS_URL from os.environ
    bus.emit_document_captured(doc, producer_id="ca-la-tentatives")
"""

from __future__ import annotations

import os

import redis
import structlog

from .events import EventBus
from .models import CapturedDocument, ScraperHealthEvent

logger = structlog.get_logger(__name__)


class RedisEventBus:
    """EventBus backed by Redis Streams with graceful degradation.

    If no ``redis_url`` is provided (or ``REDIS_URL`` is unset), all emit
    methods become no-ops so the scraper framework can run without Redis.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._inner: EventBus | None = None

        if redis_url:
            try:
                client = redis.Redis.from_url(redis_url, decode_responses=False)
                client.ping()
                self._inner = EventBus(client)
                logger.info("Redis event bus connected", redis_url=redis_url)
            except redis.RedisError as exc:
                logger.warning(
                    "Redis event bus unavailable, events will be skipped",
                    redis_url=redis_url,
                    error=str(exc),
                )
        else:
            logger.info("REDIS_URL not set — event emission disabled")

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> RedisEventBus:
        """Create a RedisEventBus from the ``REDIS_URL`` environment variable."""
        return cls(redis_url=os.environ.get("REDIS_URL"))

    # ------------------------------------------------------------------
    # EventBus protocol
    # ------------------------------------------------------------------

    def emit_document_captured(
        self, doc: CapturedDocument, producer_id: str, correlation_id: str | None = None
    ) -> str | None:
        """Emit a ``document.captured`` event.  Returns the stream message ID, or
        ``None`` if Redis is unavailable."""
        if self._inner is None:
            return None
        try:
            return self._inner.emit_document_captured(
                doc, producer_id=producer_id, correlation_id=correlation_id
            )
        except Exception as exc:
            logger.warning("Failed to emit document.captured event", error=str(exc))
            return None

    def emit_health(self, event: ScraperHealthEvent) -> str | None:
        """Emit a ``scraper.health`` event.  Returns the stream message ID, or
        ``None`` if Redis is unavailable."""
        if self._inner is None:
            return None
        try:
            return self._inner.emit_health(event)
        except Exception as exc:
            logger.warning("Failed to emit scraper.health event", error=str(exc))
            return None
