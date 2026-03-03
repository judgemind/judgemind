"""Judgemind scraper framework — public API."""

from .base import BaseScraper
from .event_bus import RedisEventBus
from .events import EventBus
from .hashing import content_changed, sha256_hex
from .models import (
    CapturedDocument,
    ContentFormat,
    DocumentCapturedEvent,
    ScheduleWindow,
    ScraperConfig,
    ScraperHealthEvent,
    ScraperPhase,
    ValidationStatus,
)
from .retry import retry_async, retry_sync
from .runner import get_scraper_ids, run_scrapers
from .storage import S3Archiver, build_s3_key

__all__ = [
    "BaseScraper",
    "CapturedDocument",
    "ContentFormat",
    "DocumentCapturedEvent",
    "EventBus",
    "RedisEventBus",
    "S3Archiver",
    "ScraperConfig",
    "ScraperHealthEvent",
    "ScraperPhase",
    "ScheduleWindow",
    "ValidationStatus",
    "build_s3_key",
    "content_changed",
    "get_scraper_ids",
    "run_scrapers",
    "retry_async",
    "retry_sync",
    "sha256_hex",
]
