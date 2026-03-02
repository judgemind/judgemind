"""Judgemind scraper framework — public API."""

from .base import BaseScraper
from .events import EventBus
from .hashing import content_changed, sha256_hex
from .models import (
    CapturedDocument,
    ContentFormat,
    DocumentCapturedEvent,
    ScraperConfig,
    ScraperHealthEvent,
    ScraperPhase,
    ScheduleWindow,
    ValidationStatus,
)
from .retry import retry_async, retry_sync
from .storage import S3Archiver, build_s3_key

__all__ = [
    "BaseScraper",
    "CapturedDocument",
    "ContentFormat",
    "DocumentCapturedEvent",
    "EventBus",
    "S3Archiver",
    "ScraperConfig",
    "ScraperHealthEvent",
    "ScraperPhase",
    "ScheduleWindow",
    "ValidationStatus",
    "build_s3_key",
    "content_changed",
    "retry_async",
    "retry_sync",
    "sha256_hex",
]
