"""Pydantic models for the scraper framework — events, config, and captured documents."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ScraperPhase(str, Enum):
    """Lifecycle phase of a scraper per Architecture Spec §3.5.1."""

    DEVELOPMENT = "development"
    BURN_IN = "burn_in"
    PRODUCTION = "production"
    STEADY_STATE = "steady_state"


class ValidationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    FLAGGED = "flagged"


class ContentFormat(str, Enum):
    HTML = "html"
    PDF = "pdf"
    DOCX = "docx"
    TEXT = "text"


# ---------------------------------------------------------------------------
# Scraper configuration
# ---------------------------------------------------------------------------


class ScheduleWindow(BaseModel):
    """Time-of-day window during which a scraper is allowed to run."""

    start: time
    end: time
    timezone: str = "America/Los_Angeles"


class ScraperConfig(BaseModel):
    """Configuration for a single scraper instance."""

    scraper_id: str
    state: str
    county: str
    court: str
    target_urls: list[str]

    # Scheduling
    poll_interval_seconds: int = 86400  # daily by default
    schedule_windows: list[ScheduleWindow] = Field(default_factory=list)

    # Rate limiting / courtesy
    request_delay_seconds: float = 1.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    respect_robots_txt: bool = True

    # Lifecycle
    phase: ScraperPhase = ScraperPhase.DEVELOPMENT

    # S3 archival
    s3_bucket: str = ""
    s3_prefix: str = ""  # overrides default path convention if set


# ---------------------------------------------------------------------------
# Captured document
# ---------------------------------------------------------------------------


class CapturedDocument(BaseModel):
    """A single document captured by a scraper, before event emission."""

    document_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    scraper_id: str
    state: str
    county: str
    court: str

    # Source
    source_url: str
    capture_timestamp: datetime
    content_format: ContentFormat

    # Content
    raw_content: bytes
    content_hash: str  # SHA-256 hex digest of raw_content

    # Parsed fields (populated by the scraper, may be partial)
    case_number: str | None = None
    courthouse: str | None = None
    department: str | None = None
    judge_name: str | None = None
    hearing_date: datetime | None = None
    ruling_text: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    # Archival
    s3_key: str | None = None  # set after archiving

    # Validation
    validation_status: ValidationStatus = ValidationStatus.PENDING


# ---------------------------------------------------------------------------
# Events (Architecture Spec §2.1)
# ---------------------------------------------------------------------------


class EventEnvelope(BaseModel):
    """Common envelope for all events per Architecture Spec §2.1.3."""

    event_type: str
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    producer_id: str
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class DocumentCapturedEvent(EventEnvelope):
    """document.captured — emitted after a document is captured and archived."""

    event_type: str = "document.captured"
    document_id: str
    scraper_id: str
    state: str
    county: str
    court: str
    source_url: str
    content_format: ContentFormat
    content_hash: str
    s3_key: str | None
    case_number: str | None
    courthouse: str | None
    department: str | None
    judge_name: str | None
    hearing_date: datetime | None
    capture_timestamp: datetime


class ScraperHealthEvent(EventEnvelope):
    """scraper.health — emitted after every scraper run."""

    event_type: str = "scraper.health"
    scraper_id: str
    success: bool
    records_captured: int
    response_time_seconds: float
    error_message: str | None = None
    run_timestamp: datetime = Field(default_factory=datetime.utcnow)
