"""Judgemind scraper framework — public API."""

from .base import BaseScraper
from .court_directory import CourtDirectory
from .css_inliner import inline_css
from .encoding import decode_response, detect_encoding
from .enrichment import (
    CourtPortalLookup,
    EnrichmentEngine,
    EnrichmentResult,
    compute_party_overlap,
    levenshtein_distance,
    normalize_case_number,
)
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
from .search import IndexingConsumer, create_index
from .storage import S3Archiver, build_s3_key

__all__ = [
    "BaseScraper",
    "CourtDirectory",
    "CourtPortalLookup",
    "CapturedDocument",
    "ContentFormat",
    "decode_response",
    "detect_encoding",
    "DocumentCapturedEvent",
    "EventBus",
    "IndexingConsumer",
    "RedisEventBus",
    "S3Archiver",
    "ScraperConfig",
    "ScraperHealthEvent",
    "ScraperPhase",
    "ScheduleWindow",
    "ValidationStatus",
    "EnrichmentEngine",
    "EnrichmentResult",
    "build_s3_key",
    "compute_party_overlap",
    "content_changed",
    "inline_css",
    "create_index",
    "levenshtein_distance",
    "normalize_case_number",
    "get_scraper_ids",
    "run_scrapers",
    "retry_async",
    "retry_sync",
    "sha256_hex",
]
