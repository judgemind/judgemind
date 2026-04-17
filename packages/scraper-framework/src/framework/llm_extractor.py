"""LLM-based structured extraction of court rulings.

The ``LlmExtractor`` class is the framework-level entry point for
converting raw court calendar text into structured ``ExtractedRuling``
models via a configurable LLM provider (Anthropic or Google GenAI).

Design principles:

- **Stateless**: no DB access, no side effects.  Pure function:
  text in, structured data out.
- **Configurable**: provider, model, and API key are configurable;
  defaults to Anthropic Claude Haiku 4.5 for cost efficiency.
- **Multimodal**: supports both text extraction (``extract()``) and
  PDF image extraction (``extract_from_pdf()``).  PDF extraction uses
  **per-page** LLM calls (one page per call, then joins results) to
  stay within output token limits and improve accuracy.
- **Resilient**: retries on transient API errors (429, 500, 529) with
  exponential backoff.
- **Observable**: logs token usage per call for cost monitoring.
- **Chunking-aware**: automatically splits large documents and merges
  results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

import anthropic
import structlog
from judgemind_config import DEFAULT_HAIKU_MODEL

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
from .llm_utils import strip_llm_json_fences

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Accumulated token usage across one or more LLM calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

# HTTP status codes that are considered transient and should be retried.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 529})

# Default retry parameters.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 2.0
_DEFAULT_MAX_DELAY = 60.0

# ---------------------------------------------------------------------------
# Chunking constants
# ---------------------------------------------------------------------------

# Default per-chunk character budget.  80K chars leaves room for the
# system prompt and output tokens within the model's context window.
_DEFAULT_MAX_CHARS = 80_000

# Maximum number of chunks per document.
_MAX_CHUNKS = 10

# Overlap characters between consecutive chunks.
_CHUNK_OVERLAP = 500

# Retry-with-smaller-chunks limits (#2136).
# Maximum recursion depth for splitting failed chunks.
_MAX_RETRY_DEPTH = 1
# Minimum chunk size (chars) to attempt a retry split.
_MIN_RETRY_CHUNK_CHARS = 2000

# Patterns for natural split boundaries.
_PAGE_BREAK_RE = re.compile(r"\f")
_CASE_BOUNDARY_RE = re.compile(
    r"\n(?=\s*(?:Case\s+(?:Number|No\.?)|CASE\s+(?:NUMBER|NO\.?))\s*[:\s])",
    re.IGNORECASE,
)

# OC-style county prefix: "30-2024-01393434" -> "2024-01393434"
_COUNTY_PREFIX_RE = re.compile(r"^\d{2,4}-(\d{4}-\d+)$")

# ---------------------------------------------------------------------------
# LLM result cache
# ---------------------------------------------------------------------------

# Cache lives in S3 at llm-cache/{provider}-{model}/prompt-{hash}/{content_hash}.json
# Locally, flows through S3_CACHE_DIR via CachedS3Client for fast reads.
# On ECS, reads/writes go directly to S3.


def _prompt_hash(prompt: str) -> str:
    """Short hash of the system prompt for cache key."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def _content_hash_for_cache(content: str | bytes, metadata: dict[str, str] | None = None) -> str:
    """Hash content + metadata for cache lookup.

    Metadata (judge_name, department, etc.) is included because the LLM
    output may differ when metadata changes, even for the same content.
    """
    h = hashlib.sha256()
    if isinstance(content, str):
        h.update(content.encode())
    else:
        h.update(content)
    if metadata:
        h.update(json.dumps(metadata, sort_keys=True).encode())
    return h.hexdigest()


class _LlmCache:
    """S3-backed cache for LLM extraction results.

    S3 key: llm-cache/{provider}-{model}/prompt-{hash}/{content_hash}.json

    When S3_CACHE_DIR is set locally, reads are served from local disk
    via CachedS3Client (fast). On ECS, reads/writes go directly to S3.
    Either way, the cache is shared across all environments.
    """

    def __init__(self, s3_client: object, bucket: str, provider: str, model: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._provider = provider
        self._model = model

    def _key(self, prompt: str, content_key: str) -> str:
        return (
            f"llm-cache/{self._provider}-{self._model}"
            f"/prompt-{_prompt_hash(prompt)}"
            f"/{content_key}.json"
        )

    def get(self, prompt: str, content_key: str) -> list[dict] | None:
        """Return cached extraction result from S3, or None if not cached."""
        key = self._key(prompt, content_key)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(response["Body"].read())
        except Exception:
            return None

    def put(self, prompt: str, content_key: str, rulings: list[dict]) -> None:
        """Cache an extraction result to S3."""
        key = self._key(prompt, content_key)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(rulings, indent=2).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            logger.warning("llm_cache.write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Per-page PDF extraction prompt and join patterns (#1590)
# ---------------------------------------------------------------------------

# System prompt for per-page multimodal extraction from court ruling PDFs.
# Each page is sent individually.  The LLM returns a JSON object with
# per-case entries and optional page-level header info.  Supports all
# California court PDF layouts: OC 3-column tables, Riverside mini-tables,
# Santa Clara 4-column tables, Contra Costa bordered boxes, San Bernardino/
# Ventura/San Francisco free-form prose, Fresno label-value forms, etc.
#
# Note: page_header extraction (department, judge, hearing_date) straddles
# the transcription/enrichment boundary — it extracts metadata from page
# visual structure rather than from ruling text content.  This is pragmatic:
# the data is visible on the page image and avoids a second pass.  The
# authoritative case_title should come from enrichment (#2212).
PDF_PER_PAGE_PROMPT = (
    "You are a court ruling transcriber. You will receive a single page "
    "image from a California court tentative ruling PDF.\n\n"
    "## Your task\n\n"
    "Extract every tentative ruling on this page into a JSON object.\n\n"
    "## Output format\n\n"
    "Return a JSON object with two keys:\n\n"
    '**"page_header"** (object or null): If this page has a document '
    "header with the department, judge name, or hearing date, extract "
    "them here. Return null if none are visible on this page.\n"
    "  - **department**: The department NUMBER or CODE only (e.g. '16', "
    "'C25', 'PS1', 'R17'), NOT the word 'Department'.\n"
    "  - **judge_name**: The judge's full name WITHOUT titles like "
    "'Hon.', 'Judge', 'Honorable', or 'Commissioner'.\n"
    "  - **hearing_date**: In ISO format 'YYYY-MM-DD'.\n\n"
    '**"rulings"** (array): One object per case ruling on this page. '
    "Each object has these fields:\n\n"
    "- **entry_number** (string): Calendar line number, item number, "
    "or sequence number (e.g. '1', '101', '(47)', 'Line 2'). "
    "Empty string if none.\n"
    "- **case_number** (string): The FULL case number exactly as "
    "printed, including any letter prefix (e.g. 'C22-01971', "
    "'2024-01393434', 'CVRI2401570', '22SMCV01940', 'N25-2112'). "
    "The letter prefix (C, N, etc.) is part of the case number, not "
    "part of the case title. Empty string if not visible.\n"
    "- **case_title** (string): ONLY the party names from the case "
    "caption (e.g. 'Constantina Marquez vs. Kohl\\'s Department "
    "Stores, Inc.'). This is JUST the names — plaintiff vs. "
    "defendant. Do NOT include case numbers, letter prefixes (C, N), "
    "hearing times, motion descriptions, 'HEARING ON...', "
    "'PETITION OF...', cause of action descriptions, or any other "
    "text. Empty string if not visible.\n"
    "- **ruling_text** (string): The COMPLETE text the judge wrote "
    "about this case. Include EVERYTHING — motion type/description, "
    "procedural background, legal standard, analysis, discussion, "
    "conclusion, disposition, orders, and any other content the "
    "judge wrote about this case. Do not summarize or omit anything. "
    "Empty string if not visible.\n\n"
    "## How to handle different page layouts\n\n"
    "California courts use many different PDF formats. Identify which "
    "layout this page uses and extract accordingly:\n\n"
    "**Tables** (columns separated by vertical lines): "
    "Return one object per table row. The narrow column(s) have the "
    "entry number and/or case info; the wide column has the ruling "
    "text.\n\n"
    "**Bordered boxes** (each case in a rectangular border): "
    "Return one object per box. The box header has the item number, "
    "time, case number, and case name. The ruling text is everything "
    "the judge wrote — typically everything after '*TENTATIVE "
    "RULING:*' or similar marker, but also include any motion "
    "description header that precedes the marker.\n\n"
    "**Free-form prose** (no table, no boxes — plain document text): "
    "Identify case boundaries by case number headers, bold case "
    "captions, centered titles, horizontal rules, or signature blocks. "
    "Return one object per case.\n\n"
    "**Label-value forms** (fields like 'Re:', 'Motion:', "
    "'Tentative Ruling:', 'Explanation:'): The case_number and "
    "case_title come from the Re: and case number fields. The "
    "ruling_text is EVERYTHING from the tentative ruling/explanation "
    "onward, including the explanation.\n\n"
    "**Continuation pages** (no new case header — just ongoing text "
    "from a previous page): Return one object with empty entry_number, "
    "empty case_number, empty case_title, and the continuation text "
    "in ruling_text.\n\n"
    "**Boilerplate-only pages** (instructions about contesting "
    "rulings, Zoom links, court reporter notices, appearance "
    "procedures — no actual case rulings): Return an empty rulings "
    "array. Still extract page_header if the department, judge, or "
    "hearing date is visible.\n\n"
    "## Formatting rules\n\n"
    "- Transcribe ruling_text as **Markdown** preserving formatting:\n"
    "  - Use **bold** for bold text and headings\n"
    "  - Use *italic* for italic text\n"
    "  - Use numbered lists (1. 2. 3.) for numbered paragraphs\n"
    "  - Use blank lines between paragraphs\n"
    "  - Preserve ALL content — do not summarize or omit\n"
    "- SKIP page headers/footers, page numbers, and watermarks.\n"
    "- SKIP boilerplate (instructions for contesting rulings, Zoom "
    "info, court reporter notices, appearance procedures).\n"
    "- If a field is blank or not applicable, use empty string.\n\n"
    "## Example output\n\n"
    "{\n"
    '  "page_header": {\n'
    '    "department": "C25",\n'
    '    "judge_name": "Gassia Apkarian",\n'
    '    "hearing_date": "2026-03-25"\n'
    "  },\n"
    '  "rulings": [\n'
    "    {\n"
    '      "entry_number": "101",\n'
    '      "case_number": "2024-01393434",\n'
    '      "case_title": "Smith vs Jones",\n'
    '      "ruling_text": "**MOTION FOR SUMMARY JUDGMENT**\\n\\n'
    "**Background**\\n\\nThis is a personal injury action arising from "
    "a slip and fall at defendant's commercial property.\\n\\n"
    "**Analysis**\\n\\nDefendant's motion for summary judgment is "
    "**GRANTED**.\\n\\n"
    "1. The moving party has met its initial burden...\\n"
    '2. Plaintiff fails to raise a triable issue..."\n'
    "    },\n"
    "    {\n"
    '      "entry_number": "",\n'
    '      "case_number": "",\n'
    '      "case_title": "",\n'
    '      "ruling_text": "continuation from previous page..."\n'
    "    }\n"
    "  ]\n"
    "}"
)

# Pattern to detect case numbers across all California county formats:
#   OC:          2024-01393434, 25D006297
#   Riverside:   CVRI2401570, CVPS2305159, CVSW2303829
#   Santa Clara: 26CV484550, 22CV407249, 2010-1-CV-163328
#   Fresno:      18CECG00898, 24CECG04476
#   SF:          FPT-24-378499, FDI-22-796758, FMS-15-386703
#   CC:          C22-01971, N25-2112
#   SB:          CIVSB2116995, CIVRS2510003
_CASE_NUMBER_RE = re.compile(
    r"\b[A-Z]{0,4}\d{2,4}-\d{1,2}-[A-Z]{2}-\d{5,8}\b"  # 2010-1-CV-163328
    r"|\b[A-Z]{1,4}-\d{2,4}-\d{5,8}\b"  # FPT-24-378499
    r"|\b[A-Z]{1,2}\d{2,4}-\d{4,8}\b"  # C22-01971, N25-2112
    r"|\b\d{2,4}-\d{5,8}\b"  # 2024-01393434, 22-02520
    r"|\b[A-Z]{2,6}\d{7,10}\b"  # CVRI2401570, CIVSB2116995, 18CECG00898
    r"|\b\d{2,4}[A-Z]{1,4}\d{5,8}\b"  # 26CV484550, 25D006297, 2024CUOR027466
    r"|\b\d{7,8}\b"  # bare 7-8 digit numbers
)

# Pattern to detect case titles (vs / v.).
_VS_RE = re.compile(r"\bv(?:s)?\.?\s", re.IGNORECASE)

# Patterns for cleaning case title fragments from multimodal extraction.
# OC case numbers follow the format: {county}-{year}-{seq}-{type}-{division}
# e.g., 30-2024-01393434-CU-OR-CJC.  The LLM sometimes splits these across
# newlines in case_info, leaving orphaned fragments.
_CASE_NUMBER_FRAGMENT_RE = re.compile(
    r"""
    \b\d{2,4}-\d{4}-     # county-year prefix with trailing dash (e.g. "30-2024-")
    | \b\d{2,4}-(?=\s|$)  # bare county prefix residual (e.g. "30-")
    | -[A-Z]{2,4}-        # type code fragment (e.g. "-CU-", "-CL-", "-PR-")
    | \b[A-Z]{2}-[A-Z]{3}\b  # division-court suffix (e.g. "OR-CJC")
    """,
    re.VERBOSE,
)

# Court and county name fragments that sometimes appear in case_info.
_COURT_NAME_RE = re.compile(
    r"Superior\s+Court\s+of\s+(?:the\s+)?(?:State\s+of\s+)?California"
    r"|County\s+of\s+\w+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Post-processing: calendar header detection and deduplication (#2096)
# ---------------------------------------------------------------------------

# Pattern to detect calendar header / boilerplate text that the LLM sometimes
# returns as ruling_text.  These contain the court name + "TENTATIVE RULINGS"
# + department info, and are not actual case rulings.
_CALENDAR_HEADER_RE = re.compile(
    r"TENTATIVE\s+RULINGS?\s+"
    r"(?:DEPARTMENT|DEPT\.?)\s+[A-Z]?\d+",
    re.IGNORECASE,
)

# Minimum ruling text length to consider for deduplication.  Very short texts
# (e.g. "GRANTED." or "DENIED.") may legitimately appear on multiple cases.
_DEDUP_MIN_LENGTH = 200

# Minimum substantive ruling text length for a ruling to be considered real
# when its case title matches a citation pattern.  Real tentative rulings are
# multi-paragraph analyses; citation artifacts are short blurbs summarising
# the cited order's outcome.  See #2448.
_CITATION_MIN_RULING_LENGTH = 400

# Signals that an ``extracted_case_title`` is a legal citation to another
# court's order rather than a primary case on the calendar.  Matches anywhere
# in the title because the LLM sometimes prepends or interleaves citator
# tokens with the caption.  See #2448.
_CITATION_TITLE_SUFFIX_RE = re.compile(
    r"("
    # Federal district-court reporters: C.D. Cal., N.D. Cal., E.D. Cal., S.D. Cal.
    r"\b[CNES]\.?\s*D\.?\s*Cal\.?\b"
    # California appellate reporter: "Cal.App.5th", "Cal. App. 4th", etc.
    r"|\bCal\.?\s*App\b"
    # California supreme-court reporter: "9 Cal.5th 1", "123 Cal.4th 456".
    r"|\b\d{1,3}\s+Cal\.?\s*\d+(?:st|nd|rd|th)?\b"
    # Federal supplement reporter: "F.Supp", "F. Supp. 2d", "F.Supp.3d".
    r"|\bF\.?\s*Supp\b"
    # Federal reporter volumes: "F.2d", "F.3d", "F.4th".
    r"|\bF\.\s*\d+(?:d|th)?\b"
    # California reporter: "Cal.Rptr.", "Cal. Rptr. 3d".
    r"|\bCal\.?\s*Rptr\b"
    # Federal case-number form: "8:22-cv-01092", "5:21-cv-01346".
    r"|\b\d+:\d{2}-cv-\d+"
    # LA Superior Court old-format BC number (e.g. BC722351).
    r"|\bBC\d{6,}\b"
    # San Diego Superior Court compact form: "18CV475JM(BGS)".
    r"|\b\d{2}CV\d{3,}[A-Z]{2,3}\s*\([A-Z]{2,3}\)"
    r")",
    re.IGNORECASE,
)

# Signals that an ``extracted_case_number`` is a citation to another
# court's case rather than a primary case on the calendar.  When the LLM
# enumerates cited orders, the case-number field often reveals the other
# county even when the title is clean.  These patterns match LA Superior
# Court formats — LASC is the single most common source of cited cases in
# Song-Beverly Act attorney-fee orders.  Other counties' formats are
# intentionally not enumerated here to keep the filter conservative;
# title-based detection will catch most of them.  See #2448.
_CITATION_CASE_NUMBER_RE = re.compile(
    r"("
    # LASC modern form: 21STCV23811, 22VECV01398, 18LBCV04321, etc.
    # Two-digit year + LASC district code + 5-6 digit serial.
    # LASC cases are never primary on the non-LA scrapers that run through
    # this LLM path (LA uses REGEX extraction), so filtering these never
    # drops legitimate primary cases.
    r"\b\d{2}(?:STCV|STCP|STLC|STPB|STPR|VECV|VECP|LBCV|LBCP|"
    r"SMCV|SMCP|CMCV|CMCP|NWCV|NWCP|PSCV|PSCP|BBCV|BBCP|CHCV|CHCP|"
    r"GDCV|GDCP|TRCV|TRCP|AVCV|AVCP)\d{4,6}\b"
    # LASC old format: BC\d{6,}, BS\d{6,}, SC\d{6,}, VC\d{6,}, PC\d{6,}, LC\d{6,}.
    r"|\b(?:BC|BS|SC|VC|PC|LC|KC|NC|GC|MC|EC|YC)\d{6,}\b"
    r")",
    re.IGNORECASE,
)


def _is_citation_artifact(ruling: ExtractedRuling) -> bool:
    """Return True if ``ruling`` looks like a citation to another court's order.

    The LLM occasionally misinterprets inline citations inside a Request for
    Judicial Notice (common in Song-Beverly attorney-fee orders) as separate
    cases on the calendar, producing 10-24 "rulings" for a PDF that has only
    one primary case.  See #2448.

    A ruling is flagged as a citation artifact when BOTH signals fire:

    1. Either its ``extracted_case_title`` contains a citator pattern
       (``_CITATION_TITLE_SUFFIX_RE``) OR its ``extracted_case_number``
       matches an out-of-county LASC pattern (``_CITATION_CASE_NUMBER_RE``).
    2. Its ``ruling_text`` is shorter than ``_CITATION_MIN_RULING_LENGTH``
       (or is ``None``).

    The length conjunction is deliberately conservative — legitimate short
    rulings (e.g. ``outcome=off_calendar`` with a one-sentence disposition)
    have clean case captions without citator suffixes and are not filtered.
    Legitimate long rulings with an unusually-formatted title are also
    exempted to avoid false positives on real cases.
    """
    title = ruling.extracted_case_title
    case_number = ruling.extracted_case_number

    title_hit = bool(title and _CITATION_TITLE_SUFFIX_RE.search(title))
    case_number_hit = bool(case_number and _CITATION_CASE_NUMBER_RE.search(case_number))
    if not title_hit and not case_number_hit:
        return False

    text = ruling.ruling_text
    if text is None:
        return True
    return len(text) < _CITATION_MIN_RULING_LENGTH


def _filter_citation_artifacts(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Remove citation artifacts from a list of extracted rulings (#2448).

    Applies :func:`_is_citation_artifact` to each ruling.  Preserves the
    relative order of kept rulings.

    Safeguards:

    * If the input has 0 or 1 entries, return it unchanged.  A single-ruling
      document is never filtered down to zero — even if the one ruling
      happens to match the citation pattern, emitting zero rulings would
      produce an orphan document downstream.
    * If every ruling in a multi-entry list is flagged as a citation
      artifact, keep the single longest-text candidate as a fallback.  This
      prevents ambiguous signals from emptying out a document.

    Emits one ``llm_extractor.citation_filtered`` structured log per
    removed ruling, plus a ``llm_extractor.citation_filter_fallback`` log
    when the fallback path fires.
    """
    if len(rulings) <= 1:
        return rulings

    kept: list[ExtractedRuling] = []
    removed: list[ExtractedRuling] = []
    for ruling in rulings:
        if _is_citation_artifact(ruling):
            removed.append(ruling)
            logger.info(
                "llm_extractor.citation_filtered",
                case_title=ruling.extracted_case_title,
                case_number=ruling.extracted_case_number,
                text_length=len(ruling.ruling_text) if ruling.ruling_text else 0,
            )
        else:
            kept.append(ruling)

    if not kept:
        # Fallback: every candidate looked like a citation.  Keep the one
        # with the longest ruling_text — it's the most likely real ruling.
        longest = max(
            removed,
            key=lambda r: len(r.ruling_text) if r.ruling_text else 0,
        )
        logger.warning(
            "llm_extractor.citation_filter_fallback",
            original_count=len(rulings),
            kept_case_title=longest.extracted_case_title,
            kept_text_length=len(longest.ruling_text) if longest.ruling_text else 0,
        )
        return [longest]

    return kept


def _is_calendar_header(text: str | None) -> bool:
    """Return True if ``text`` looks like a calendar header, not a ruling.

    Calendar headers contain boilerplate like "TENTATIVE RULINGS DEPARTMENT C27"
    and are not actual case ruling text.  The LLM sometimes assigns these as
    ``ruling_text`` for individual cases (#2096).
    """
    if not text:
        return False
    return bool(_CALENDAR_HEADER_RE.search(text))


def _deduplicate_ruling_texts(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Null out duplicate ruling_text across cases in the same PDF (#2096).

    When the LLM produces the same ruling text for multiple different cases,
    only the first occurrence keeps its text.  Subsequent duplicates get
    ``ruling_text = None`` so they are not stored as cross-contaminated.

    Short texts (< ``_DEDUP_MIN_LENGTH`` chars) are exempt because legitimate
    identical rulings (e.g. "GRANTED." or "Motion is denied.") can appear
    on multiple cases.

    Entries with ``cross_reference_source`` set are exempt because they
    intentionally share ruling text via cross-reference resolution (#2317).
    """
    import hashlib

    seen_hashes: dict[str, int] = {}  # hash -> index of first occurrence
    for i, ruling in enumerate(rulings):
        text = ruling.ruling_text
        if not text or len(text) < _DEDUP_MIN_LENGTH:
            continue
        # Skip cross-reference entries — they share text by design (#2317).
        if ruling.cross_reference_source is not None:
            continue
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if text_hash in seen_hashes:
            # Duplicate — null out the text on this ruling.
            # Use model_copy to preserve all fields (extracted_parties,
            # motion_type, outcome, case_type, confidence, etc.).
            rulings[i] = ruling.model_copy(update={"ruling_text": None})
            logger.warning(
                "llm_extractor.dedup_ruling_text",
                case_number=ruling.extracted_case_number,
                duplicate_of_index=seen_hashes[text_hash],
                text_length=len(text),
            )
        else:
            seen_hashes[text_hash] = i

    return rulings


# ---------------------------------------------------------------------------
# Post-processing: cross-reference resolution (#2317)
# ---------------------------------------------------------------------------

# Santa Clara PDFs use calendar line numbers.  When multiple motions share
# one ruling, the PDF lists the full ruling under one line number and uses
# cross-references for the others.  These patterns match the stub text.

# Patterns that contain an explicit line number reference.
_XREF_LINE_RE = re.compile(
    r"(?:See\s+Line\s+(\d+))"
    r"|(?:[Ss]croll\s+down\s+to\s+Lines?\s+(\d+))"
    r"|(?:Ctrl\s+Click\b.*?\bon\s+Line\s+(\d+))"
    r"|(?:Click\s+on\s+Line\s+(\d+))"
    r"|(?:\[The\s+Court\s+addressed\b.*?\bat\s+Line\s+(\d+)\s+above\])",
    re.IGNORECASE,
)

# Pattern for implicit "previous entry" references (no explicit line number).
_XREF_ORDER_ABOVE_RE = re.compile(
    r"\[Order\s+above\]",
    re.IGNORECASE,
)

# Minimum ruling_text length on the *referenced* entry for it to be considered
# substantive enough to copy.  Short referenced text (< 100 chars) usually means
# the reference target is itself a stub and copying would propagate the stub.
_XREF_REF_MIN_TEXT_LENGTH = 100

# Maximum ruling_text length on the *stub* entry for us to still consider it a
# stub.  Santa Clara stubs commonly run 300-500 chars because the LLM prefixes
# the caption header (e.g. ``**Order on Defendants' Demurrer...**``) before the
# cross-reference phrase.  Texts beyond 2000 chars almost certainly contain a
# real ruling and should not be overwritten even if they happen to mention
# another line number.  See #2416.
_XREF_STUB_MAX_TEXT_LENGTH = 2000


def _resolve_cross_references(
    rulings: list[ExtractedRuling],
    entry_number_to_index: dict[int, int],
) -> list[ExtractedRuling]:
    """Resolve cross-reference entries by copying ruling_text from the referenced entry (#2317).

    Santa Clara tentative ruling PDFs use calendar line numbers.  When multiple
    cases share one ruling, the PDF lists the full ruling under one line number
    and uses cross-reference text for the others (e.g., "See Line 4 for
    tentative ruling").  These entries end up with only the referral text as
    their ``ruling_text``, resulting in null outcome after enrichment.

    This function:

    1. Detects cross-reference patterns in ``ruling_text`` regardless of the
       stub text's length (up to ``_XREF_STUB_MAX_TEXT_LENGTH``).  Santa Clara
       stubs frequently include a caption-header prefix that pushes the stub
       text past a few hundred chars — an aggressive length gate was filtering
       those out previously (#2416).
    2. Extracts the referenced line number (or uses the previous entry for
       ``[Order above]``).
    3. Copies ``ruling_text`` from the referenced entry when (a) the referenced
       entry has substantial text (>= ``_XREF_REF_MIN_TEXT_LENGTH`` chars), and
       (b) the referenced text is longer than the current stub text so we never
       replace a longer, presumably-more-complete ruling with a shorter one.
    4. Sets ``cross_reference_source`` to the referenced entry_number.

    Because ``_join_page_rows`` is invoked on the rows from *all* pages joined
    together (see :func:`extract_from_pdf`), cross-references that span pages
    (e.g. stub on page 2, target on page 1) resolve naturally — the joined
    ``entry_number_to_index`` contains every line number from every page.

    Parameters
    ----------
    rulings:
        List of ``ExtractedRuling`` objects from ``_join_page_rows``.
    entry_number_to_index:
        Mapping from calendar line entry_number to index in ``rulings``.
    """
    # Build reverse map: index -> entry_number (for "order above" lookups).
    index_to_entry: dict[int, int] = {v: k for k, v in entry_number_to_index.items()}

    for i, ruling in enumerate(rulings):
        text = ruling.ruling_text
        if not text:
            continue

        # Cap the stub length.  Texts much longer than this almost certainly
        # contain a real ruling — even if they happen to mention another line
        # number in passing.  Do not overwrite them.
        if len(text) > _XREF_STUB_MAX_TEXT_LENGTH:
            continue

        # Try explicit line number patterns.
        m = _XREF_LINE_RE.search(text)
        if m:
            # Extract the first non-None group (the line number).
            ref_line = next((int(g) for g in m.groups() if g is not None), None)
            if ref_line is not None:
                ref_idx = entry_number_to_index.get(ref_line)
                if ref_idx is not None and ref_idx != i:
                    ref_ruling = rulings[ref_idx]
                    ref_text = ref_ruling.ruling_text
                    if (
                        ref_text
                        and len(ref_text) >= _XREF_REF_MIN_TEXT_LENGTH
                        and len(ref_text) > len(text)
                    ):
                        rulings[i] = ruling.model_copy(
                            update={
                                "ruling_text": ref_text,
                                "cross_reference_source": ref_line,
                            }
                        )
                        logger.info(
                            "llm_extractor.xref_resolved",
                            case_number=ruling.extracted_case_number,
                            ref_line=ref_line,
                            stub_length=len(text),
                            ref_length=len(ref_text),
                        )
                continue

        # Try implicit "order above" pattern.
        if _XREF_ORDER_ABOVE_RE.search(text):
            # Find the immediately preceding entry in the calendar.
            prev_idx: int | None = None
            if i > 0:
                prev_idx = i - 1
            if prev_idx is not None:
                ref_ruling = rulings[prev_idx]
                ref_entry = index_to_entry.get(prev_idx)
                ref_text = ref_ruling.ruling_text
                if (
                    ref_text
                    and len(ref_text) >= _XREF_REF_MIN_TEXT_LENGTH
                    and len(ref_text) > len(text)
                ):
                    rulings[i] = ruling.model_copy(
                        update={
                            "ruling_text": ref_text,
                            "cross_reference_source": ref_entry,
                        }
                    )
                    logger.info(
                        "llm_extractor.xref_resolved_order_above",
                        case_number=ruling.extracted_case_number,
                        prev_entry=ref_entry,
                        stub_length=len(text),
                        ref_length=len(ref_text),
                    )

    return rulings


# ---------------------------------------------------------------------------
# LlmExtractor
# ---------------------------------------------------------------------------


class LlmExtractor:
    """Stateless extractor that converts raw court calendar text to structured data.

    Supports both text-based extraction via ``extract()`` and multimodal
    PDF image extraction via ``extract_from_pdf()``.  The provider can be
    ``"anthropic"`` (default) or ``"google"``.

    Args:
        provider: LLM provider — ``"anthropic"`` (default) or ``"google"``.
        model: Model ID.  Defaults to Claude Haiku 4.5 for Anthropic or
            ``"gemini-2.5-flash-lite"`` for Google.
        api_key: Provider API key.  For Anthropic, if ``None`` the
            ``ANTHROPIC_API_KEY`` env var is used.  For Google, the
            ``GOOGLE_API_KEY`` env var is used.
        max_retries: Number of retries on transient API failures (429, 500, 529).
        base_delay: Initial backoff delay in seconds.
        max_delay: Maximum backoff delay in seconds.
        max_output_tokens: Maximum tokens in the model response.
        max_chars_per_chunk: Per-chunk character limit for large documents.

    Example::

        extractor = LlmExtractor()
        rulings = extractor.extract("Case No. 22SMCV01940 ...")

        # Multimodal extraction from PDF images
        extractor = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")
        rulings = extractor.extract_from_pdf(pdf_bytes)
    """

    # Default models per provider.
    _PROVIDER_DEFAULT_MODELS: dict[str, str] = {
        "anthropic": DEFAULT_HAIKU_MODEL,
        "google": "gemini-2.5-flash-lite",
    }

    def __init__(
        self,
        *,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        max_delay: float = _DEFAULT_MAX_DELAY,
        max_output_tokens: int = 4096,
        max_chars_per_chunk: int = _DEFAULT_MAX_CHARS,
        bust_cache: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model or self._PROVIDER_DEFAULT_MODELS.get(provider, DEFAULT_HAIKU_MODEL)
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_output_tokens = max_output_tokens
        self._max_chars_per_chunk = max_chars_per_chunk
        # Instance-level default for cache-bust mode (#2424).  When True,
        # all ``extract()`` / ``extract_from_pdf()`` calls on this
        # instance skip cache reads unless explicitly overridden.  Cache
        # writes still happen so subsequent runs without bust_cache
        # benefit from the fresh extraction.
        self._bust_cache = bust_cache

        # Create provider-specific client.
        if provider == "google":
            self._client = _create_google_client(api_key=api_key)
        else:
            # Default to Anthropic.
            client_kwargs: dict[str, str] = {}
            if api_key is not None:
                client_kwargs["api_key"] = api_key
            self._client = anthropic.Anthropic(**client_kwargs)

        # LLM result cache — stored in S3, served locally via CachedS3Client.
        cache_bucket = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")
        self._cache: _LlmCache | None = (
            _LlmCache(
                s3_client=self._get_cache_s3_client(),
                bucket=cache_bucket,
                provider=self._provider,
                model=self._model,
            )
            if cache_bucket
            else None
        )

    @staticmethod
    def _get_cache_s3_client() -> object:
        """Return an S3 client for the LLM cache.

        Uses make_s3_client() which returns a CachedS3Client when
        S3_CACHE_DIR is set (local dev), or plain boto3 (ECS).
        """
        from .s3_cache import make_s3_client

        return make_s3_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        system_prompt: str | None = None,
        bust_cache: bool = False,
    ) -> list[ExtractedRuling]:
        """Extract structured rulings from raw calendar page text.

        This is the main entry point.  It handles chunking for large
        documents, calls the LLM API with the extraction prompt,
        parses the JSON response into ``ExtractedRuling`` models, and
        deduplicates rulings across chunks.

        Args:
            text: Raw text content from a court calendar page (PDF or HTML,
                already converted to plain text).
            metadata: Optional dict with authoritative scraper-provided
                context.  Supported keys: ``judge_name``, ``department``,
                ``hearing_date``.
            system_prompt: Custom system prompt to use instead of the
                default ``EXTRACTION_SYSTEM_PROMPT``.  Used for
                county-specific prompts (e.g. Riverside).
            bust_cache: When ``True``, skip the cache read on this call.
                Cache writes still happen so subsequent calls benefit
                from the fresh result.  See ``self._bust_cache`` for a
                persistent instance-level default.  Used by the
                ``--bust-llm-cache`` reingest flag (#2424).

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the text is empty or the API call fails after retries.
        """
        if not text or not text.strip():
            return []

        effective_prompt = system_prompt or EXTRACTION_SYSTEM_PROMPT
        content_key = _content_hash_for_cache(text, metadata)
        effective_bust = bust_cache or self._bust_cache

        # Check cache
        if self._cache is not None and not effective_bust:
            cached = self._cache.get(effective_prompt, content_key)
            if cached is not None:
                logger.debug("llm_cache.hit", content_key=content_key[:12])
                return [ExtractedRuling(**r) for r in cached]

        chunks = self._split_into_chunks(text)
        usage = TokenUsage()

        if len(chunks) == 1:
            results = self._extract_chunk_with_retry(
                chunks[0], metadata=metadata, usage=usage, system_prompt=system_prompt
            )
            self._log_usage(usage)
            if not results:
                return []
            merged = self._merge_results(results)
            rulings = self._propagate_document_fields(merged)
        else:
            # Multiple chunks: extract each and merge.
            logger.info(
                "llm_extractor.chunked",
                num_chunks=len(chunks),
                chunk_sizes=[len(c) for c in chunks],
            )
            all_results: list[ExtractionResult] = []
            for i, chunk in enumerate(chunks):
                results = self._extract_chunk_with_retry(
                    chunk,
                    metadata=metadata,
                    usage=usage,
                    chunk_index=i,
                    system_prompt=system_prompt,
                )
                all_results.extend(results)

            self._log_usage(usage)

            if not all_results:
                return []

            merged = self._merge_results(all_results)
            rulings = self._propagate_document_fields(merged)

        # Post-processing: remove citation artifacts produced by LLM
        # misinterpreting inline citations (Requests for Judicial Notice)
        # as separate rulings.  See #2448.
        rulings = _filter_citation_artifacts(rulings)

        # Write to cache
        if self._cache is not None and rulings:
            self._cache.put(
                effective_prompt,
                content_key,
                [r.model_dump(mode="json") for r in rulings],
            )

        return rulings

    def extract_from_pdf(
        self,
        pdf_bytes: bytes,
        *,
        metadata: dict[str, str] | None = None,
        max_pages: int = 50,
        bust_cache: bool = False,
    ) -> list[ExtractedRuling]:
        """Extract structured rulings from PDF page images (multimodal).

        Renders each PDF page to a PNG image, sends **one page per LLM
        call** with a per-page prompt, collects all table rows, and joins
        cross-page results into ``ExtractedRuling`` models using
        entry-number + case-info heuristics.

        This per-page approach (a) stays within the Flash Lite 8192 output
        token limit, (b) produces more reliable row extraction than
        multi-page calls, and (c) enables accurate cross-page continuation
        detection via the entry_number field.

        Args:
            pdf_bytes: Raw PDF file content.
            metadata: Optional dict with authoritative scraper-provided
                context.  Supported keys: ``judge_name``, ``department``,
                ``hearing_date``.
            max_pages: Maximum number of PDF pages to render.  Pages beyond
                this limit are silently skipped.
            bust_cache: When ``True``, skip the cache read on this call.
                Cache writes still happen so subsequent calls benefit
                from the fresh result.  See ``self._bust_cache`` for a
                persistent instance-level default.  Used by the
                ``--bust-llm-cache`` reingest flag (#2424).

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the PDF is empty, rendering fails, or the API call fails
            after retries.
        """
        if not pdf_bytes:
            return []

        content_key = _content_hash_for_cache(pdf_bytes, metadata)
        effective_bust = bust_cache or self._bust_cache

        # Check cache
        if self._cache is not None and not effective_bust:
            cached = self._cache.get(PDF_PER_PAGE_PROMPT, content_key)
            if cached is not None:
                logger.debug("llm_cache.hit_pdf", content_key=content_key[:12])
                return [ExtractedRuling(**r) for r in cached]

        page_images = _render_pdf_pages(pdf_bytes, max_pages)
        if not page_images:
            logger.warning("llm_extractor.no_pages_rendered")
            return []

        usage = TokenUsage()

        # Per-page extraction: one LLM call per page.
        all_rows: list[dict] = []
        for page_idx, (img_bytes, media_type) in enumerate(page_images):
            page_rows = self._extract_single_page(
                img_bytes, media_type, metadata=metadata, usage=usage, page_index=page_idx
            )
            all_rows.extend(page_rows)

        self._log_usage(usage)

        if not all_rows:
            logger.warning("llm_extractor.no_rows_extracted", page_count=len(page_images))
            return []

        # Join rows into cases and convert to ExtractedRuling objects.
        rulings = _join_page_rows(all_rows, metadata=metadata)

        # Write to cache
        if self._cache is not None and rulings:
            self._cache.put(
                PDF_PER_PAGE_PROMPT,
                content_key,
                [r.model_dump(mode="json") for r in rulings],
            )

        return rulings

    # ------------------------------------------------------------------
    # Internal: API call with retries
    # ------------------------------------------------------------------

    def _extract_chunk(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        usage: TokenUsage,
        chunk_index: int = 0,
        system_prompt: str | None = None,
    ) -> ExtractionResult | None:
        """Call the LLM API for a single text chunk and parse the result.

        Supports both Anthropic and Google providers.  When the provider
        is ``"google"``, delegates to ``call_llm`` from
        ``ingestion.llm_providers`` which handles the Google GenAI SDK.

        Retries on transient errors (429, 500, 529) with exponential backoff.
        """
        effective_prompt = system_prompt or EXTRACTION_SYSTEM_PROMPT
        user_message = self._build_user_message(text, metadata)
        delay = self._base_delay

        # Google provider: delegate to the provider-agnostic call_llm helper
        # which handles the Google GenAI SDK and retry logic.
        if self._provider == "google":
            return self._extract_chunk_google(
                user_message,
                effective_prompt,
                usage=usage,
                chunk_index=chunk_index,
                metadata=metadata,
            )

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_output_tokens,
                    temperature=0,
                    system=effective_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )

                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens
                usage.api_calls += 1

                raw_text = response.content[0].text.strip()
                return self._parse_response(raw_text, metadata)

            except anthropic.RateLimitError:
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.rate_limit",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.rate_limit_exhausted",
                    chunk_index=chunk_index,
                )
                return None

            except anthropic.InternalServerError:
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.server_error",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.server_error_exhausted",
                    chunk_index=chunk_index,
                )
                return None

            except anthropic.APIStatusError as exc:
                # 529 is Anthropic's "overloaded" status code.
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.retryable_error",
                        status_code=exc.status_code,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.api_error",
                    status_code=exc.status_code,
                    error=str(exc),
                    chunk_index=chunk_index,
                )
                return None

            except anthropic.APIConnectionError as exc:
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.connection_error",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.connection_error_exhausted",
                    error=str(exc),
                    chunk_index=chunk_index,
                )
                return None

        return None  # pragma: no cover — defensive

    def _extract_chunk_google(
        self,
        user_message: str,
        system_prompt: str,
        *,
        usage: TokenUsage,
        chunk_index: int = 0,
        metadata: dict[str, str] | None = None,
    ) -> ExtractionResult | None:
        """Call the Google GenAI API for a single text chunk.

        Delegates to ``call_llm`` from ``ingestion.llm_providers`` which
        handles the Google GenAI SDK, retry logic, and timeout.
        """
        from ingestion.llm_providers import call_llm

        response = call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            provider="google",
            model=self._model,
            max_tokens=self._max_output_tokens,
            timeout=60.0,
        )

        if response is None:
            logger.warning(
                "llm_extractor.google_api_failure",
                chunk_index=chunk_index,
            )
            return None

        usage.input_tokens += response.input_tokens
        usage.output_tokens += response.output_tokens
        usage.api_calls += 1

        return self._parse_response(response.text, metadata)

    def _extract_chunk_with_retry(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        usage: TokenUsage,
        chunk_index: int = 0,
        system_prompt: str | None = None,
        _depth: int = 0,
    ) -> list[ExtractionResult]:
        """Extract a chunk, retrying with smaller sub-chunks on parse failure.

        When the LLM response is truncated (e.g. output exceeds
        ``max_output_tokens``), ``_extract_chunk`` returns ``None``
        because the truncated JSON cannot be parsed.  This method
        catches that failure and retries by splitting the chunk in
        half at a natural boundary, extracting each sub-chunk
        independently.

        Recursion is limited to one level (``_depth <= 1``).  At depth 0,
        a failed chunk is split into two halves and each half is retried
        (depth 1).  At depth 1, failures are final — no further splitting.

        Returns a list of ``ExtractionResult`` (one per successful
        sub-chunk, or one for the original chunk if it succeeds).  The
        caller merges all results.
        """
        result = self._extract_chunk(
            text,
            metadata=metadata,
            usage=usage,
            chunk_index=chunk_index,
            system_prompt=system_prompt,
        )
        if result is not None:
            return [result]

        # Extraction failed (likely truncated JSON).  If we have room to
        # retry and the chunk is large enough to split meaningfully
        # (> 2000 chars), split it in half and try each sub-chunk.
        if _depth >= _MAX_RETRY_DEPTH or len(text) < _MIN_RETRY_CHUNK_CHARS:
            logger.warning(
                "llm_extractor.chunk_failed_no_retry",
                chunk_index=chunk_index,
                chunk_len=len(text),
                depth=_depth,
            )
            return []

        logger.info(
            "llm_extractor.retry_with_smaller_chunks",
            chunk_index=chunk_index,
            chunk_len=len(text),
            depth=_depth,
        )

        sub_chunks = self._split_chunk_in_half(text)
        sub_results: list[ExtractionResult] = []
        for i, sub_chunk in enumerate(sub_chunks):
            sub_results.extend(
                self._extract_chunk_with_retry(
                    sub_chunk,
                    metadata=metadata,
                    usage=usage,
                    chunk_index=chunk_index * 10 + i,
                    system_prompt=system_prompt,
                    _depth=_depth + 1,
                )
            )
        return sub_results

    @staticmethod
    def _split_chunk_in_half(text: str) -> list[str]:
        """Split a text chunk roughly in half at a natural boundary.

        Prefers case boundaries (``SUPERIOR COURT``, ``Case Number``),
        then page breaks, then paragraph breaks.  Falls back to the
        midpoint if no natural boundary is found.
        """
        midpoint = len(text) // 2
        search_start = midpoint - len(text) // 4
        search_end = midpoint + len(text) // 4

        # Search for natural boundaries near the midpoint.
        best_split: int | None = None
        best_distance = len(text)

        # Case boundaries (SF family law: "SUPERIOR COURT" header).
        for m in _CASE_BOUNDARY_RE.finditer(text, search_start, search_end):
            dist = abs(m.start() - midpoint)
            if dist < best_distance:
                best_distance = dist
                best_split = m.start()

        # Page breaks.
        if best_split is None:
            for m in _PAGE_BREAK_RE.finditer(text, search_start, search_end):
                dist = abs(m.start() - midpoint)
                if dist < best_distance:
                    best_distance = dist
                    best_split = m.start()

        # Paragraph breaks.
        if best_split is None:
            for m in re.finditer(r"\n\n", text[search_start:search_end]):
                pos = search_start + m.start()
                dist = abs(pos - midpoint)
                if dist < best_distance:
                    best_distance = dist
                    best_split = pos

        if best_split is None:
            best_split = midpoint

        first_half = text[:best_split]
        second_half = text[max(best_split - _CHUNK_OVERLAP, 0) :]

        return [first_half, second_half]

    def _extract_single_page(
        self,
        img_bytes: bytes,
        media_type: str,
        *,
        metadata: dict[str, str] | None = None,
        usage: TokenUsage,
        page_index: int = 0,
    ) -> list[dict]:
        """Send a single page image to the LLM and return extracted table rows.

        Uses the ``PDF_PER_PAGE_PROMPT`` to extract rows from OC-style
        three-column table PDFs.  Each row is a dict with keys
        ``entry_number``, ``case_info``, and ``ruling_text``.

        Returns an empty list if the API call fails or the page has no rows.
        """
        from ingestion.llm_providers import call_llm_with_images

        text_message = self._build_user_message_for_page(metadata)
        delay = self._base_delay

        for attempt in range(1, self._max_retries + 1):
            try:
                response = call_llm_with_images(
                    system_prompt=PDF_PER_PAGE_PROMPT,
                    text_message=text_message,
                    images=[(img_bytes, media_type)],
                    provider=self._provider,
                    model=self._model,
                    client=self._client,
                    max_retries=0,  # We handle retries ourselves.
                    max_tokens=self._max_output_tokens,
                    timeout=20.0,
                )

                if response is None:
                    if attempt < self._max_retries:
                        wait = min(delay, self._max_delay)
                        logger.warning(
                            "llm_extractor.page_api_failure",
                            attempt=attempt,
                            max_retries=self._max_retries,
                            retry_in=wait,
                            page_index=page_index,
                        )
                        time.sleep(wait)
                        delay *= 2
                        continue
                    logger.error(
                        "llm_extractor.page_api_exhausted",
                        page_index=page_index,
                    )
                    return []

                usage.input_tokens += response.input_tokens
                usage.output_tokens += response.output_tokens
                usage.api_calls += 1

                return _parse_page_rows(response.text, page_index)

            except Exception:  # noqa: BLE001
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.page_extract_error",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        page_index=page_index,
                        exc_info=True,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.page_extract_exhausted",
                    page_index=page_index,
                    exc_info=True,
                )
                return []

        return []  # pragma: no cover — defensive

    # ------------------------------------------------------------------
    # Internal: message building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(
        text: str,
        metadata: dict[str, str] | None,
    ) -> str:
        """Build the user message for the extraction prompt."""
        parts: list[str] = []
        if metadata:
            meta_lines: list[str] = []
            if metadata.get("judge_name"):
                meta_lines.append(f"Judge name (authoritative): {metadata['judge_name']}")
            if metadata.get("department"):
                meta_lines.append(f"Department (authoritative): {metadata['department']}")
            if metadata.get("hearing_date"):
                meta_lines.append(f"Hearing date (authoritative): {metadata['hearing_date']}")
            if meta_lines:
                parts.append("Metadata from scraper:\n" + "\n".join(meta_lines))

        parts.append(f"Document:\n\n{text}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_user_message_for_page(
        metadata: dict[str, str] | None,
    ) -> str:
        """Build the text portion of the user message for per-page extraction."""
        parts: list[str] = []
        if metadata:
            meta_lines: list[str] = []
            if metadata.get("judge_name"):
                meta_lines.append(f"Judge name: {metadata['judge_name']}")
            if metadata.get("department"):
                meta_lines.append(f"Department: {metadata['department']}")
            if metadata.get("hearing_date"):
                meta_lines.append(f"Hearing date: {metadata['hearing_date']}")
            if meta_lines:
                parts.append("Context:\n" + "\n".join(meta_lines))

        parts.append(
            "Extract all tentative rulings from this page. "
            "One entry per case. Skip page headers and footers."
        )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Internal: response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(
        raw_text: str,
        metadata: dict[str, str] | None,
    ) -> ExtractionResult | None:
        """Parse a JSON response from the model into an ``ExtractionResult``.

        Handles markdown code fences, validates enum values, normalizes
        case numbers, and applies authoritative metadata overrides.
        """
        try:
            cleaned = strip_llm_json_fences(raw_text)
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(
                "llm_extractor.json_parse_error",
                error=str(exc),
                raw_preview=raw_text[:200],
            )
            return None

        # Top-level fields — support both extracted_ prefix and legacy names.
        judge_name = parsed.get("extracted_judge_name") or parsed.get("judge_name")
        hearing_date = parsed.get("hearing_date")
        department = parsed.get("department")

        # Apply metadata overrides.
        if metadata:
            if metadata.get("judge_name"):
                judge_name = metadata["judge_name"]
            if metadata.get("department"):
                department = metadata["department"]
            if metadata.get("hearing_date"):
                hearing_date = metadata["hearing_date"]

        # Parse rulings array.
        raw_rulings = parsed.get("rulings", [])
        if not isinstance(raw_rulings, list):
            raw_rulings = []

        rulings: list[ExtractedRuling] = []
        for r in raw_rulings:
            if not isinstance(r, dict):
                continue
            rulings.append(_parse_single_ruling(r))

        return ExtractionResult(
            extracted_judge_name=judge_name,
            hearing_date=hearing_date,
            department=str(department) if department else None,
            rulings=rulings,
        )

    # ------------------------------------------------------------------
    # Internal: chunking
    # ------------------------------------------------------------------

    def _split_into_chunks(self, text: str) -> list[str]:
        """Split text into chunks of at most ``_max_chars_per_chunk`` characters.

        Uses natural boundaries (page breaks, case-number headers) when
        available; falls back to double-newline paragraph breaks.
        """
        max_chars = self._max_chars_per_chunk
        if len(text) <= max_chars:
            return [text]

        # Try page breaks first, then case boundaries, then paragraphs.
        boundaries = [m.start() for m in _PAGE_BREAK_RE.finditer(text)]
        if not boundaries:
            boundaries = [m.start() for m in _CASE_BOUNDARY_RE.finditer(text)]
        if not boundaries:
            boundaries = [m.start() for m in re.finditer(r"\n\n", text)]
        if not boundaries:
            return _force_split(text, max_chars)

        chunks: list[str] = []
        chunk_start = 0
        last_boundary = 0

        for boundary in boundaries:
            span = boundary - chunk_start
            if span > max_chars:
                split_at = last_boundary if last_boundary > chunk_start else boundary
                chunks.append(text[chunk_start:split_at])
                chunk_start = max(split_at - _CHUNK_OVERLAP, 0)
                if len(chunks) >= _MAX_CHUNKS:
                    break
            last_boundary = boundary

        if len(chunks) < _MAX_CHUNKS and chunk_start < len(text):
            remaining = text[chunk_start:]
            if len(remaining) > max_chars:
                for sc in _force_split(remaining, max_chars):
                    if len(chunks) >= _MAX_CHUNKS:
                        break
                    chunks.append(sc)
            else:
                chunks.append(remaining)

        if not chunks:
            chunks = [text[:max_chars]]

        return chunks

    # ------------------------------------------------------------------
    # Internal: merge multi-chunk results
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _propagate_document_fields(result: ExtractionResult) -> list[ExtractedRuling]:
        """Propagate document-level judge/department to rulings missing them.

        The LLM extracts judge_name and department at the document level
        (from the PDF header). Per-ruling fields are often null because the
        judge isn't repeated in each ruling's text. This fills in the gaps.
        """
        doc_judge = result.extracted_judge_name
        doc_dept = result.department
        for ruling in result.rulings:
            if not ruling.extracted_judge_name and doc_judge:
                ruling.extracted_judge_name = doc_judge
            if not ruling.department and doc_dept:
                ruling.department = doc_dept
        return result.rulings

    @staticmethod
    def _merge_results(results: list[ExtractionResult]) -> ExtractionResult:
        """Merge extraction results from multiple chunks.

        Document-level fields come from the first chunk (headers are at the
        top).  Rulings are deduplicated by ``extracted_case_number``.
        """
        if not results:
            return ExtractionResult()

        first = results[0]
        seen: set[str] = set()
        unique_rulings: list[ExtractedRuling] = []

        for result in results:
            for ruling in result.rulings:
                key = ruling.extracted_case_number
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                unique_rulings.append(ruling)

        return ExtractionResult(
            extracted_judge_name=first.extracted_judge_name,
            hearing_date=first.hearing_date,
            department=first.department,
            rulings=unique_rulings,
        )

    # ------------------------------------------------------------------
    # Internal: logging
    # ------------------------------------------------------------------

    @staticmethod
    def _log_usage(usage: TokenUsage) -> None:
        """Log accumulated token usage for cost monitoring."""
        logger.info(
            "llm_extractor.token_usage",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            api_calls=usage.api_calls,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _normalize_case_number(raw: str) -> str:
    """Strip county prefix from case numbers (e.g. OC format)."""
    m = _COUNTY_PREFIX_RE.match(raw.strip())
    return m.group(1) if m else raw.strip()


def _parse_single_ruling(r: dict) -> ExtractedRuling:  # noqa: ANN001
    """Parse a single ruling dict from the LLM JSON response into an ``ExtractedRuling``."""
    # Case number — support both old and new field names.
    case_number = r.get("extracted_case_number") or r.get("case_number")
    if case_number:
        case_number = _normalize_case_number(str(case_number))

    # Case title.
    case_title = r.get("extracted_case_title") or r.get("case_title")

    # Case type — validate against enum.
    case_type: ExtractionCaseType | None = None
    raw_case_type = r.get("case_type")
    if raw_case_type:
        try:
            case_type = ExtractionCaseType(raw_case_type)
        except ValueError:
            case_type = ExtractionCaseType.OTHER

    # Outcome — validate against enum.
    outcome: ExtractionOutcome | None = None
    raw_outcome = r.get("outcome")
    if raw_outcome:
        try:
            outcome = ExtractionOutcome(raw_outcome)
        except ValueError:
            outcome = ExtractionOutcome.OTHER

    # Judge name.
    judge_name = r.get("extracted_judge_name") or r.get("judge_name")

    # Parties.
    raw_parties = r.get("extracted_parties") or r.get("parties", [])
    parties: list[ExtractedParty] = []
    if isinstance(raw_parties, list):
        for p in raw_parties:
            if isinstance(p, dict) and p.get("name") and p.get("role"):
                name = str(p["name"]).strip()
                # Validate party name is plausible.
                if len(name) > 200 or "\n" in name or "\r" in name:
                    logger.warning(
                        "llm_extractor.invalid_party_name",
                        length=len(name),
                        preview=name[:80],
                    )
                    continue
                raw_conf = p.get("confidence", "high")
                try:
                    conf = ConfidenceLevel(raw_conf)
                except ValueError:
                    conf = ConfidenceLevel.HIGH
                parties.append(
                    ExtractedParty(
                        name=name,
                        role=str(p["role"]),
                        confidence=conf,
                    )
                )

    # Confidence scores.
    raw_confidence = r.get("confidence", {})
    confidence = _parse_confidence(raw_confidence)

    return ExtractedRuling(
        extracted_case_number=case_number,
        extracted_case_title=case_title,
        extracted_parties=parties,
        extracted_judge_name=judge_name,
        department=r.get("department"),
        motion_type=r.get("motion_type"),
        outcome=outcome,
        ruling_text=r.get("ruling_text"),
        hearing_date=r.get("hearing_date"),
        case_type=case_type,
        confidence=confidence,
    )


_VALID_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})


def _parse_confidence(raw: dict | None) -> FieldConfidence:  # noqa: ANN001
    """Parse a confidence dict from the LLM response."""
    if not raw or not isinstance(raw, dict):
        return FieldConfidence()

    def _level(key: str) -> ConfidenceLevel:
        val = raw.get(key, "high")
        if val in _VALID_CONFIDENCE_LEVELS:
            return ConfidenceLevel(val)
        return ConfidenceLevel.HIGH

    return FieldConfidence(
        case_number=_level("case_number"),
        case_title=_level("case_title"),
        parties=_level("parties"),
        judge=_level("judge"),
        ruling_text=_level("ruling_text"),
        outcome=_level("outcome"),
    )


def _force_split(text: str, max_chars: int) -> list[str]:
    """Split text into fixed-size chunks with overlap."""
    chunks: list[str] = []
    pos = 0
    while pos < len(text) and len(chunks) < _MAX_CHUNKS:
        end = min(pos + max_chars, len(text))
        chunks.append(text[pos:end])
        pos = end - _CHUNK_OVERLAP if end < len(text) else end
    return chunks


# ---------------------------------------------------------------------------
# Per-page row parsing and join logic (#1590)
# ---------------------------------------------------------------------------


def _parse_page_rows(raw_text: str, page_index: int) -> list[dict]:
    """Parse LLM response for a single page into a list of row dicts.

    Each row has ``entry_number`` (int or None), ``case_info`` (str),
    and ``ruling_text`` (str).  Supports both the legacy 3-field format
    (entry_number/case_info/ruling_text) and the newer 4-field format
    (entry_number/case_number/case_title/ruling_text).

    When the newer format is detected (``case_number`` or ``case_title``
    present), these are combined into ``case_info`` for downstream
    compatibility with ``_join_page_rows``.

    Also extracts ``page_header`` metadata if present.
    """
    cleaned = strip_llm_json_fences(raw_text)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object or array in the response.
        obj_start = cleaned.find("{")
        arr_start = cleaned.find("[")
        if obj_start >= 0 and (arr_start < 0 or obj_start < arr_start):
            obj_end = cleaned.rfind("}") + 1
            if obj_end > obj_start:
                try:
                    parsed = json.loads(cleaned[obj_start:obj_end])
                except json.JSONDecodeError:
                    parsed = None
            else:
                parsed = None
        elif arr_start >= 0:
            arr_end = cleaned.rfind("]") + 1
            if arr_end > arr_start:
                try:
                    parsed = json.loads(cleaned[arr_start:arr_end])
                except json.JSONDecodeError:
                    parsed = None
            else:
                parsed = None
        else:
            parsed = None

        if parsed is None:
            logger.warning(
                "llm_extractor.page_parse_error",
                page_index=page_index,
                raw_preview=raw_text[:200],
            )
            return []

    # Handle both list and dict responses.
    page_header: dict | None = None
    if isinstance(parsed, dict):
        # Extract page_header if present.
        page_header = parsed.get("page_header")
        # Get the rulings array.
        rows_raw = parsed.get("rulings", parsed.get("rows", parsed.get("entries", [parsed])))
    elif isinstance(parsed, list):
        rows_raw = parsed
    else:
        return []

    rows: list[dict] = []

    # If page_header contains metadata, emit a synthetic header row so
    # _join_page_rows can extract judge/department/hearing_date.
    if page_header and isinstance(page_header, dict):
        header_parts: list[str] = []
        if page_header.get("department"):
            header_parts.append(f"Department {page_header['department']}")
        if page_header.get("judge_name"):
            header_parts.append(f"JUDGE {page_header['judge_name']}")
        if page_header.get("hearing_date"):
            header_parts.append(f"Hearing Date: {page_header['hearing_date']}")
        if header_parts:
            rows.append(
                {
                    "entry_number": None,
                    "case_info": "\n".join(header_parts),
                    "ruling_text": "",
                }
            )

    for item in rows_raw:
        if not isinstance(item, dict):
            continue
        entry_number = item.get("entry_number")
        if entry_number is not None:
            entry_str = str(entry_number).rstrip(".").strip()
            # Strip parentheses from Fresno-style "(47)" numbers.
            entry_str = entry_str.strip("()")
            try:
                entry_number = int(entry_str)
            except (ValueError, TypeError):
                # Extract numeric portion from "Line 2", "Item 3", etc.
                num_match = re.search(r"\d+", entry_str)
                entry_number = int(num_match.group()) if num_match else None

        # Support both legacy (case_info) and new (case_number + case_title)
        # formats.  Combine new-format fields into case_info for downstream
        # compatibility with _join_page_rows.
        if "case_number" in item or "case_title" in item:
            case_number = str(item.get("case_number") or "").strip()
            case_title = str(item.get("case_title") or "").strip()
            # Post-process: strip trailing artifacts from case_title.
            # The LLM sometimes appends county prefix letters, motion
            # descriptions, or cause of action text to the case title.
            # Strip trailing single-letter county prefixes (C, N, etc.)
            case_title = re.sub(r"\s+[A-Z]$", "", case_title)
            # Strip anything after common artifact patterns.
            # These are specific enough to avoid truncating legitimate
            # party names like "Johnson C Smith".
            for pattern in [
                r"\s+[A-Z]\s+(?:HEARING|PETITION|FURTHER|MOTION)",
                r"\s+[A-Z]\s+(?:Third|Fourth|Fifth|Sixth|Seventh|First|Second)",
                r"\s*\*HEARING",
                r"\s+PETITION OF:",
            ]:
                case_title = re.sub(pattern, "", case_title)
            parts = [p for p in [case_title, case_number] if p]
            case_info = "\n".join(parts)
        else:
            case_info = str(item.get("case_info", "")).strip()

        rows.append(
            {
                "entry_number": entry_number,
                "case_info": case_info,
                "ruling_text": str(item.get("ruling_text", "")).strip(),
            }
        )

    return rows


def _is_new_case(row: dict) -> bool:
    """Determine if a row starts a new case based on entry_number and case_info.

    A new case is detected when:
    1. ``entry_number`` is a valid integer, AND
    2. ``case_info`` contains a case number pattern or "vs"/"v."
    """
    if row["entry_number"] is None:
        return False

    case_info = row["case_info"]
    if not case_info:
        return False

    # Check for case number pattern.
    if _CASE_NUMBER_RE.search(case_info):
        return True

    # Check for "vs" / "v." pattern.
    if _VS_RE.search(case_info):
        return True

    return False


def _extract_case_number_from_info(case_info: str) -> str | None:
    """Extract a case number from the case_info string."""
    m = _CASE_NUMBER_RE.search(case_info)
    if m:
        raw = m.group(0)
        return _normalize_case_number(raw)
    return None


def _extract_case_title_from_info(case_info: str) -> str | None:
    """Extract a case title from the case_info string.

    Looks for patterns like "Smith v. Jones" or "Smith vs Jones" after
    the case number.  Cleans up common artifacts from multimodal
    extraction: embedded newlines, case number fragments, and court
    name fragments.
    """
    # 1. Replace newlines with spaces so multi-line case_info is joined.
    cleaned = case_info.replace("\n", " ")
    # 2. Remove complete case numbers.
    cleaned = _CASE_NUMBER_RE.sub("", cleaned)
    # 3. Remove case number fragments (partial prefixes, type codes, suffixes).
    cleaned = _CASE_NUMBER_FRAGMENT_RE.sub("", cleaned)
    # 4. Remove court / county name fragments.
    cleaned = _COURT_NAME_RE.sub("", cleaned)
    # 5. Remove trailing case-number county prefix letters (e.g. "C" from
    #    "C22-01971" where the numeric part was already stripped in step 2).
    #    Also strip trailing artifacts like "C Fraud" or "C PETITION OF:".
    cleaned = re.sub(r"\s+[CN]\s*$", "", cleaned)
    cleaned = re.sub(
        r"\s+[CN]\s+(?:HEARING|PETITION|MOTION|FURTHER"
        r"|Third|Fourth|Fifth|Sixth|Seventh|First|Second|Fraud).*$",
        "",
        cleaned,
    )
    # 6. Remove other non-title artifacts.
    cleaned = re.sub(r"\s*\*?HEARING ON\b.*$", "", cleaned)
    cleaned = re.sub(r"\s+PETITION OF:.*$", "", cleaned)
    # 7. Collapse multiple whitespace to single space.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # 8. Remove "None" artifacts from null fields being stringified.
    cleaned = re.sub(r"\bNone\b", "", cleaned)
    # 9. Collapse multiple whitespace to single space (again after removals).
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # 10. Remove leading/trailing punctuation and whitespace.
    cleaned = cleaned.strip(" -;,\t")
    # 10b. Dual-"vs." contamination guard (#2369).  When adjacent cases on
    #     a multimodal PDF page bleed into each other, the LLM sometimes
    #     returns a case_info containing TWO "vs."/"v." clauses with the
    #     first plaintiff's name repeating at the start of the second
    #     case, e.g. "Anh-Vu Nguyen vs. Freedom Medical Group, LLC
    #     Anh-Vu Nguyen vs. Seed4Planet, LLC".  When that pattern is
    #     detected, keep only the first plaintiff-vs-defendant pair by
    #     truncating at the whitespace immediately before the repeated
    #     plaintiff name.  Cases where the plaintiff does NOT repeat
    #     (genuinely unusual titles with two "vs." clauses) are left
    #     untouched to avoid mistruncation.
    vs_iter = list(_VS_RE.finditer(cleaned))
    if len(vs_iter) >= 2:
        first_vs_start = vs_iter[0].start()
        first_vs_end = vs_iter[0].end()
        second_vs_start = vs_iter[1].start()
        # First plaintiff text — everything before the first "vs.".
        first_plaintiff = cleaned[:first_vs_start].strip(" ,;-")
        # First word of the first plaintiff.  The heuristic: if this word
        # reappears between the end of the first "vs." clause and the
        # start of the second "vs.", we're seeing the repeated plaintiff
        # contamination pattern.
        first_word = first_plaintiff.split(" ", 1)[0] if first_plaintiff else ""
        if first_word:
            needle = " " + first_word
            candidate = cleaned.find(needle, first_vs_end, second_vs_start)
            if candidate > first_vs_end:
                cleaned = cleaned[:candidate].rstrip(" ,;-")
    # 11. Truncate overly long titles.  Titles over 150 chars typically
    #     contain multiple case names or full court caption text jammed
    #     together.
    if len(cleaned) > 150:
        # For "v./vs." titles, try to truncate after the first party clause.
        vs_match = _VS_RE.search(cleaned)
        if vs_match:
            after_vs = cleaned[vs_match.end() :]
            term_match = re.search(
                r"\b(?:[A-Z]{2,6}\d{5,}|\d{2,4}[A-Z]{1,4}\d{5,}"
                r"|\d{2,4}-\d{5,}|[A-Z]{1,4}-\d{2,4}-\d{5,})\b"
                r"|" + _VS_RE.pattern,
                after_vs,
                re.IGNORECASE,
            )
            if term_match:
                cleaned = cleaned[: vs_match.end() + term_match.start()].strip(" ,;")
        # For probate/estate/conservatorship titles (no "v."), truncate
        # at the start of the next case pattern.
        if len(cleaned) > 150:
            next_case = re.search(
                r"\s(?:CONSERVATORSHIP|ESTATE|GUARDIANSHIP|IN THE MATTER)"
                r"\s+OF[:.]",
                cleaned[30:],  # skip past the first one
            )
            if next_case:
                cleaned = cleaned[: 30 + next_case.start()].strip(" ,;")
        # Hard-truncate at a word boundary if still too long.
        if len(cleaned) > 150:
            space_idx = cleaned.rfind(" ", 0, 150)
            if space_idx > 50:
                cleaned = cleaned[:space_idx].rstrip(" ,;")
    if cleaned:
        return cleaned
    return None


def _join_page_rows(
    rows: list[dict],
    *,
    metadata: dict[str, str] | None = None,
) -> list[ExtractedRuling]:
    """Join per-page rows into ``ExtractedRuling`` objects.

    Uses the entry_number + case_info heuristic to detect case boundaries.
    Rows without a valid entry_number or without a case identifier in
    case_info are treated as continuations of the previous case.
    """
    if not rows:
        return []

    # Group rows into cases.
    cases: list[dict] = []  # Each: {case_info, ruling_text}

    # Extract judge/department/hearing_date from header rows before
    # skipping them.  Header rows typically have entry_number=null,
    # empty ruling_text, and case_info containing metadata.  These
    # are emitted by _parse_page_rows when it encounters a page_header.
    header_judge: str | None = None
    header_dept: str | None = None
    header_date: str | None = None
    for row in rows:
        if row["entry_number"] is None and not row["ruling_text"] and row.get("case_info"):
            info = row["case_info"]
            # Extract judge from "JUDGE WILLIAM D. CLASTER" pattern
            judge_match = re.search(r"JUDGE\s+(.+?)(?:\n|$)", info, re.IGNORECASE)
            if judge_match and not header_judge:
                header_judge = judge_match.group(1).strip()
            # Extract department from "Department CX101" or "Dept. C25" pattern
            dept_match = re.search(r"(?:Department|Dept\.?)\s+([A-Z0-9]+)", info, re.IGNORECASE)
            if dept_match and not header_dept:
                header_dept = dept_match.group(1).strip()
            # Extract hearing date from "Hearing Date: YYYY-MM-DD" pattern
            date_match = re.search(r"Hearing Date:\s*(\d{4}-\d{2}-\d{2})", info)
            if date_match and not header_date:
                header_date = date_match.group(1)

    # Track entry_number -> case_index for cross-reference resolution (#2317).
    entry_number_to_index: dict[int, int] = {}

    for row in rows:
        # Skip header rows (e.g., department/judge headers) that the LLM
        # may include with entry_number=null and empty ruling_text.  These
        # would otherwise be merged into adjacent cases, corrupting data.
        if row["entry_number"] is None and not row["ruling_text"]:
            continue

        if _is_new_case(row):
            cases.append(
                {
                    "case_info": row["case_info"],
                    "ruling_text": row["ruling_text"],
                }
            )
            # Record entry_number -> case index for cross-reference lookups.
            if row["entry_number"] is not None:
                entry_number_to_index[row["entry_number"]] = len(cases) - 1
        elif cases:
            # Continuation: merge into previous case.
            if row["case_info"]:
                cases[-1]["case_info"] += "\n" + row["case_info"]
            if row["ruling_text"]:
                cases[-1]["ruling_text"] += "\n" + row["ruling_text"]
        else:
            # No previous case to merge into — start a new case if there's
            # meaningful content.  This handles continuations from a previous
            # page's last case or header rows.
            if row["case_info"] or row["ruling_text"]:
                cases.append(
                    {
                        "case_info": row["case_info"],
                        "ruling_text": row["ruling_text"],
                    }
                )

    # Convert to ExtractedRuling objects.
    rulings: list[ExtractedRuling] = []
    for case in cases:
        case_number = _extract_case_number_from_info(case["case_info"])
        case_title = _extract_case_title_from_info(case["case_info"])
        ruling_text = case["ruling_text"].strip() or None

        # Post-processing: filter calendar header text (#2096).
        # The LLM sometimes returns calendar header/boilerplate as the
        # ruling_text for individual cases.  Detect and null it out.
        if _is_calendar_header(ruling_text):
            logger.warning(
                "llm_extractor.calendar_header_filtered",
                case_number=case_number,
                text_preview=(ruling_text or "")[:100],
            )
            ruling_text = None

        # Apply metadata overrides, then header extraction, for judge/dept/date.
        judge_name: str | None = None
        department: str | None = None
        hearing_date: str | None = None
        if metadata:
            judge_name = metadata.get("judge_name")
            department = metadata.get("department")
            hearing_date = metadata.get("hearing_date")
        if not judge_name:
            judge_name = header_judge
        if not department:
            department = header_dept
        if not hearing_date:
            hearing_date = header_date

        rulings.append(
            ExtractedRuling(
                extracted_case_number=case_number,
                extracted_case_title=case_title,
                extracted_judge_name=judge_name,
                department=department,
                hearing_date=hearing_date,
                ruling_text=ruling_text,
            )
        )

    # Post-processing: resolve cross-reference entries (#2317).
    # Santa Clara PDFs use cross-references like "See Line 4 for tentative
    # ruling" when multiple motions share one ruling.  Copy the ruling text
    # from the referenced entry so enrichment can extract an outcome.
    if entry_number_to_index:
        rulings = _resolve_cross_references(rulings, entry_number_to_index)

    # Post-processing: deduplicate identical ruling texts (#2096).
    # The LLM sometimes produces the same ruling text for multiple cases
    # in the same PDF.  Keep only the first occurrence; null out duplicates.
    rulings = _deduplicate_ruling_texts(rulings)

    # Post-processing: remove citation artifacts (#2448).
    # When a PDF contains a Request for Judicial Notice citing many other
    # courts' orders, the LLM may return each citation as a separate ruling.
    # Filter those out using title+length heuristics.
    rulings = _filter_citation_artifacts(rulings)

    return rulings


# ---------------------------------------------------------------------------
# PDF page rendering
# ---------------------------------------------------------------------------

# Resolution for rendering PDF pages to images (DPI).
_PDF_RENDER_RESOLUTION = 150


def _render_pdf_pages(
    pdf_bytes: bytes,
    max_pages: int,
) -> list[tuple[bytes, str]]:
    """Render PDF pages to PNG images using pymupdf.

    Uses pymupdf (fitz) for higher quality image rendering compared to
    pdfplumber, which was validated in the OC multimodal eval (PR #1692).

    Returns a list of ``(png_bytes, media_type)`` tuples.
    """
    import pymupdf

    results: list[tuple[bytes, str]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        zoom = _PDF_RENDER_RESOLUTION / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)

        for i, page in enumerate(doc):
            if i >= max_pages:
                logger.info(
                    "llm_extractor.page_limit_reached",
                    max_pages=max_pages,
                    total_pages=len(doc),
                )
                break

            pix = page.get_pixmap(matrix=matrix)
            png_bytes = pix.tobytes("png")
            results.append((png_bytes, "image/png"))

    return results


# ---------------------------------------------------------------------------
# Google client factory
# ---------------------------------------------------------------------------


def _create_google_client(*, api_key: str | None = None) -> object:
    """Create a Google GenAI client.

    If *api_key* is provided, uses it directly.  Otherwise falls back to
    the ``GOOGLE_API_KEY`` environment variable.

    Raises ``ValueError`` if no API key is available.
    """
    import os

    from google import genai

    resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not resolved_key:
        msg = (
            "No Google API key provided — pass api_key= or set "
            "the GOOGLE_API_KEY environment variable."
        )
        raise ValueError(msg)
    return genai.Client(api_key=resolved_key)
