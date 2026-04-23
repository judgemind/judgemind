"""Exponential backoff retry utility per Architecture Spec §3.1."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def retry_sync[T](
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retry fn synchronously with exponential backoff. Raises last exception on exhaustion."""
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            wait = min(delay, max_delay)
            logger.warning(
                "Attempt %d/%d failed (%s). Retrying in %.1fs.",
                attempt,
                max_attempts,
                exc,
                wait,
            )
            time.sleep(wait)
            delay *= 2
    raise last_exc  # type: ignore[misc]


async def retry_async(
    fn: Callable[[], object],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> object:
    """Retry an async callable with exponential backoff."""
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            wait = min(delay, max_delay)
            logger.warning(
                "Attempt %d/%d failed (%s). Retrying in %.1fs.",
                attempt,
                max_attempts,
                exc,
                wait,
            )
            await asyncio.sleep(wait)
            delay *= 2
    raise last_exc  # type: ignore[misc]
