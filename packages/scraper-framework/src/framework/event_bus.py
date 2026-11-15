"""RedisEventBus — REDIS_URL-aware wrapper around EventBus.

When REDIS_URL is set, emits events to Redis Streams via the underlying
EventBus.  When REDIS_URL is absent (local dev, tests), all emit calls
are skipped so scrapers can run without a Redis instance.

When REDIS_URL *is* set, a ``document.captured`` emit never fails silently
(#5009).  A failed XADD is retried with backoff; if it still fails, or if
Redis could not be reached at all, :meth:`emit_document_captured` raises
:class:`EventEmitError`.  ``BaseScraper.run()`` counts that document as a
failed emit and records the run as failed, naming the archived S3 key, so
the raw can be ingested later instead of sitting unreferenced in S3.

Usage:
    bus = RedisEventBus.from_env()   # reads REDIS_URL from os.environ
    bus.emit_document_captured(doc, producer_id="ca-la-tentatives")
"""

from __future__ import annotations

import os
import time

import redis
import structlog

from .events import EventBus, StreamBacklogFull
from .models import CapturedDocument, ScraperHealthEvent

logger = structlog.get_logger(__name__)

#: Attempts per ``document.captured`` emit (the first try plus retries).
EMIT_ATTEMPTS = 3
#: Delay before the first retry; doubled before each later retry.
EMIT_RETRY_BASE_DELAY_SECONDS = 0.5
#: While Redis is unreachable, reconnect at most this often.  Between
#: attempts each emit fails at once, so a run with Redis down does not spend
#: a connect timeout on every document.
RECONNECT_INTERVAL_SECONDS = 30.0
#: Connect / socket timeouts for the Redis client, so an unreachable host
#: fails in seconds instead of hanging the scraper.
REDIS_CONNECT_TIMEOUT_SECONDS = 5.0
REDIS_SOCKET_TIMEOUT_SECONDS = 30.0


class EventEmitError(RuntimeError):
    """A ``document.captured`` event could not be emitted (#5009)."""


class EventBusUnavailable(EventEmitError):  # noqa: N818
    """``REDIS_URL`` is set but Redis cannot be reached."""


class RedisEventBus:
    """EventBus backed by Redis Streams.

    With no ``redis_url`` (``REDIS_URL`` unset), every emit is a no-op so the
    scraper framework can run without Redis.  With a ``redis_url``, emits are
    retried and a final failure raises :class:`EventEmitError`.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or None
        self._inner: EventBus | None = None
        self._last_connect_attempt: float | None = None
        self._last_connect_error: str | None = None

        if self._redis_url:
            if not self._connect():
                logger.error(
                    "Redis event bus unavailable at startup; document.captured emits "
                    "will fail and the run will be recorded as failed",
                    redis_url=self._redis_url,
                    error=self._last_connect_error,
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
    # Connection
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when ``REDIS_URL`` is set (emits are expected to reach Redis)."""
        return self._redis_url is not None

    def _connect(self) -> bool:
        """Try to connect and ping.  Returns True on success."""
        self._last_connect_attempt = time.monotonic()
        try:
            client = redis.Redis.from_url(
                self._redis_url,
                decode_responses=False,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            )
            client.ping()
        except redis.RedisError as exc:
            self._inner = None
            self._last_connect_error = str(exc)
            return False
        self._inner = EventBus(client)
        self._last_connect_error = None
        logger.info("Redis event bus connected", redis_url=self._redis_url)
        return True

    def _require_inner(self) -> EventBus:
        """Return the connected bus, reconnecting at most once per interval."""
        if self._inner is not None:
            return self._inner
        last = self._last_connect_attempt
        if last is None or time.monotonic() - last >= RECONNECT_INTERVAL_SECONDS:
            if self._connect():
                assert self._inner is not None
                return self._inner
        raise EventBusUnavailable(
            f"Redis event bus unavailable ({self._last_connect_error or 'not connected'})"
        )

    # ------------------------------------------------------------------
    # EventBus protocol
    # ------------------------------------------------------------------

    def emit_document_captured(
        self, doc: CapturedDocument, producer_id: str, correlation_id: str | None = None
    ) -> str | None:
        """Emit a ``document.captured`` event and return its stream message ID.

        Returns ``None`` only when ``REDIS_URL`` is unset.  Otherwise a
        Redis error is retried up to :data:`EMIT_ATTEMPTS` times with
        exponential backoff, and a final failure raises
        :class:`EventEmitError`.  A full backlog (:class:`StreamBacklogFull`)
        is not retried: it clears only when the worker consumes.
        """
        if not self.enabled:
            return None
        inner = self._require_inner()
        delay = EMIT_RETRY_BASE_DELAY_SECONDS
        last_exc: Exception | None = None
        for attempt in range(1, EMIT_ATTEMPTS + 1):
            try:
                return inner.emit_document_captured(
                    doc, producer_id=producer_id, correlation_id=correlation_id
                )
            except StreamBacklogFull as exc:
                raise EventEmitError(str(exc)) from exc
            except Exception as exc:
                last_exc = exc
                if attempt == EMIT_ATTEMPTS:
                    break
                logger.warning(
                    "document.captured emit failed, retrying",
                    attempt=attempt,
                    max_attempts=EMIT_ATTEMPTS,
                    wait_seconds=delay,
                    s3_key=doc.s3_key,
                    error=str(exc),
                )
                time.sleep(delay)
                delay *= 2
        raise EventEmitError(
            f"document.captured emit failed after {EMIT_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    def emit_health(self, event: ScraperHealthEvent) -> str | None:
        """Emit a ``scraper.health`` event.  Returns the stream message ID, or
        ``None`` if Redis is unavailable.

        Best-effort: nothing consumes ``scraper.health`` (the run is recorded
        in ``telemetry.scraper_runs``), so a failure here is only a warning.
        """
        if self._inner is None:
            return None
        try:
            return self._inner.emit_health(event)
        except Exception as exc:
            logger.warning("Failed to emit scraper.health event", error=str(exc))
            return None
