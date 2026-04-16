"""Judgemind scraper framework — public API."""

from .base import BaseScraper
from .browser import apply_stealth
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
from .extraction_config import (
    CountyExtractionConfig,
    ExtractionMethod,
    get_county_extraction_config,
)
from .hashing import content_changed, sha256_hex
from .llm_enrichment import (
    ENRICHMENT_SYSTEM_PROMPT,
    VALID_MOTION_TYPES,
    VALID_OUTCOMES,
    EnrichmentParties,
    LlmEnrichmentResult,
    MotionType,
    OutcomeType,
    enrich_ruling,
)
from .llm_extractor import LlmExtractor
from .llm_schema import (
    EXTRACTION_SYSTEM_PROMPT,
    ConfidenceLevel,
    ExtractedParty,
    ExtractedRuling,
    ExtractionCaseType,
    ExtractionOutcome,
    ExtractionResult,
    FieldConfidence,
)
from .logging import configure_structlog
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
from .s3_cache import CachedS3Client, make_s3_client
from .search import IndexingConsumer, create_index
from .storage import S3Archiver, build_s3_key

__all__ = [
    "BaseScraper",
    "apply_stealth",
    "ConfidenceLevel",
    "CountyExtractionConfig",
    "ExtractionMethod",
    "LlmExtractor",
    "CourtDirectory",
    "CourtPortalLookup",
    "CapturedDocument",
    "ContentFormat",
    "EXTRACTION_SYSTEM_PROMPT",
    "ExtractionCaseType",
    "ExtractionOutcome",
    "ExtractionResult",
    "ExtractedParty",
    "ExtractedRuling",
    "FieldConfidence",
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
    "ENRICHMENT_SYSTEM_PROMPT",
    "EnrichmentEngine",
    "EnrichmentParties",
    "EnrichmentResult",
    "LlmEnrichmentResult",
    "MotionType",
    "OutcomeType",
    "VALID_MOTION_TYPES",
    "VALID_OUTCOMES",
    "enrich_ruling",
    "CachedS3Client",
    "build_s3_key",
    "make_s3_client",
    "compute_party_overlap",
    "configure_structlog",
    "content_changed",
    "inline_css",
    "create_index",
    "levenshtein_distance",
    "normalize_case_number",
    "get_county_extraction_config",
    "get_scraper_ids",
    "run_scrapers",
    "retry_async",
    "retry_sync",
    "sha256_hex",
]
