"""LLM-based field extraction for court ruling documents.

Extracts structured fields (judge name, hearing date, case number, case title,
outcome, motion type, parties) from court ruling documents via a configurable
LLM provider (Anthropic, Google GenAI, etc.).  The provider and model are
selected via ``LLM_PROVIDER`` and ``LLM_MODEL`` environment variables.

This is the core extraction logic called by the ingestion worker for fields that
scrapers did not populate.

On any API failure, returns ``None`` so the caller can fall back to regex-based
extraction (``extract.py``).
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date

import structlog

from framework.llm_schema import (
    EXTRACTION_SYSTEM_PROMPT,
    ConfidenceLevel,
    FieldConfidence,
)

from .llm_providers import call_llm, call_llm_with_images

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Token tracking — thread-safe accumulator for cost monitoring
# ---------------------------------------------------------------------------


@dataclass
class TokenTracker:
    """Thread-safe accumulator for LLM token usage across multiple calls.

    Pass an instance to ``extract_fields_llm()`` via the *token_tracker*
    parameter.  After all calls complete, read the totals and call
    ``estimated_cost()`` to get an approximate USD cost.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, input_tokens: int, output_tokens: int) -> None:
        """Atomically add token counts from a single LLM call."""
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.api_calls += 1

    def estimated_cost(
        self,
        *,
        input_price_per_mtok: float | None = None,
        output_price_per_mtok: float | None = None,
        provider: str | None = None,
    ) -> float:
        """Estimate USD cost based on token counts and provider pricing.

        If *input_price_per_mtok* and *output_price_per_mtok* are given,
        they are used directly (dollars per 1M tokens).  Otherwise,
        pricing is looked up from ``_PROVIDER_PRICING`` using *provider*.

        Returns the estimated cost in USD.
        """
        if input_price_per_mtok is not None and output_price_per_mtok is not None:
            inp_price = input_price_per_mtok
            out_price = output_price_per_mtok
        else:
            inp_price, out_price = _PROVIDER_PRICING.get(
                provider or "google",
                _PROVIDER_PRICING["google"],
            )
        return (
            self.input_tokens * inp_price / 1_000_000 + self.output_tokens * out_price / 1_000_000
        )


# Provider pricing: (input $/1M tokens, output $/1M tokens)
# These are approximate list prices as of 2026-03.
_PROVIDER_PRICING: dict[str, tuple[float, float]] = {
    "google": (0.075, 0.30),  # Gemini 2.5 Flash Lite
    "anthropic": (0.80, 4.00),  # Claude Haiku 4.5
}


# ---------------------------------------------------------------------------
# Outcome taxonomy — matches the ``ruling_outcome`` PostgreSQL enum
# ---------------------------------------------------------------------------

OUTCOME_VALUES = frozenset(
    {
        "granted",
        "denied",
        "granted_in_part",
        "denied_in_part",
        "moot",
        "continued",
        "off_calendar",
        "submitted",
        "other",
    }
)

# ---------------------------------------------------------------------------
# Case type taxonomy — matches the ``cases.case_type`` TEXT column
# ---------------------------------------------------------------------------

CASE_TYPE_VALUES = frozenset(
    {
        "civil",
        "criminal",
        "family",
        "probate",
        "small_claims",
        "juvenile",
        "traffic",
        "other",
    }
)

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LLMRulingResult:
    """Extraction result for a single ruling within a document."""

    case_number: str | None = None
    case_title: str | None = None
    case_type: str | None = None  # Uses CASE_TYPE_VALUES
    outcome: str | None = None  # Uses ruling_outcome enum values
    motion_type: str | None = None
    parties: list[dict[str, str]] = field(default_factory=list)
    confidence: FieldConfidence = field(default_factory=FieldConfidence)


@dataclass
class LLMExtractionResult:
    """Extraction result for an entire document (may contain multiple rulings)."""

    judge_name: str | None = None
    hearing_date: date | None = None
    department: str | None = None
    case_count: int = 0
    rulings: list[LLMRulingResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTML preprocessing
# ---------------------------------------------------------------------------

# Tags to remove entirely (content and all)
_STRIP_TAGS_RE = re.compile(
    r"<(?:style|script)[^>]*>.*?</(?:style|script)>",
    re.DOTALL | re.IGNORECASE,
)

# All remaining HTML tags
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Collapse runs of whitespace (but preserve paragraph breaks)
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def preprocess_html(raw_html: str) -> str:
    """Strip HTML boilerplate and extract text content.

    For LA court rulings, extracts content from the ``speechSynthesis`` div
    (which contains the actual ruling text).  Strips ``<style>`` and
    ``<script>`` tags, removes all remaining HTML tags, decodes HTML entities,
    and collapses whitespace.

    This reduces a ~618K HTML file to ~5K of actual content, saving tokens.
    """
    # Try to extract the speechSynthesis div (LA court format)
    match = re.search(
        r'<div[^>]*id=["\']speechSynthesis["\'][^>]*>(.*?)</div>\s*</td>',
        raw_html,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        text = match.group(1)
    else:
        # Fallback: use the whole body
        body_match = re.search(r"<body[^>]*>(.*)</body>", raw_html, re.DOTALL | re.IGNORECASE)
        text = body_match.group(1) if body_match else raw_html

    # Strip <style> and <script> tags with their content
    text = _STRIP_TAGS_RE.sub("", text)

    # Strip all remaining HTML tags
    text = _HTML_TAG_RE.sub(" ", text)

    # Decode HTML entities
    text = html.unescape(text)

    # Collapse whitespace
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)

    return text.strip()


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

# PDF magic bytes: all valid PDFs start with "%PDF"
_PDF_MAGIC = b"%PDF"


def is_pdf_binary(content: str | bytes) -> bool:
    """Return True if *content* appears to be raw PDF binary data.

    Checks for the PDF magic bytes (``%PDF``) at the start of the content.
    Works with both ``str`` (which may contain mojibake from decoding binary
    PDF bytes as UTF-8) and ``bytes``.
    """
    if isinstance(content, bytes):
        return content[:4] == _PDF_MAGIC
    # When raw PDF bytes are decoded as UTF-8/latin-1, the string starts with "%PDF"
    return content.startswith("%PDF")


def extract_text_from_pdf(content: str | bytes) -> str | None:
    """Extract readable text from PDF binary content using pdfplumber.

    Accepts either raw ``bytes`` or a ``str`` that was produced by decoding
    raw PDF bytes (common when PDF content is stored as a text field).

    When pdfplumber returns no text (image-only PDF), falls back to OCR
    via the LLM vision API (see ``ocr_pdf_text``).

    Returns the extracted text, or ``None`` if extraction fails or produces
    no text.  On failure, logs a warning and returns ``None`` so callers can
    fall back gracefully.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — cannot extract PDF text")
        return None

    if isinstance(content, str):
        # Re-encode to bytes — PDF content decoded as latin-1 round-trips cleanly;
        # UTF-8 decoded content may have replacement chars but we try anyway.
        try:
            raw_bytes = content.encode("latin-1")
        except UnicodeEncodeError:
            raw_bytes = content.encode("utf-8", errors="replace")
    else:
        raw_bytes = content

    try:
        pages_text: list[str] = []
        has_pages = False
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            has_pages = len(pdf.pages) > 0
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
        if pages_text:
            return "\n\f\n".join(pages_text)

        # No text extracted but the PDF has pages — likely an image-only PDF.
        # Try OCR fallback via LLM vision API (#1332).
        if has_pages:
            logger.info(
                "pdf_extraction.no_text_layer",
                pdf_size=len(raw_bytes),
                fallback="ocr_llm_vision",
            )
            ocr_result = ocr_pdf_text(raw_bytes)
            if ocr_result:
                return ocr_result

        return None
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# OCR fallback for image-only PDFs (#1332)
# ---------------------------------------------------------------------------

# Maximum number of PDF pages to OCR.  Prevents excessive LLM costs on
# very large scanned documents.
_OCR_MAX_PAGES = 10

# Resolution (DPI) for rendering PDF pages to images for OCR.
# 150 DPI balances readability with image size (token cost).
_OCR_RESOLUTION = 150

# Per-page timeout for the LLM vision call (seconds).
_OCR_PAGE_TIMEOUT = 30.0

# Total timeout for all pages combined (seconds).
_OCR_TOTAL_TIMEOUT = 120.0

_OCR_SYSTEM_PROMPT = (
    "You are an OCR system for court documents. "
    "Extract ALL text from the provided image of a court document page. "
    "Preserve the original formatting as closely as possible: "
    "line breaks, paragraph breaks, indentation, and spacing. "
    "Do NOT interpret, summarize, or restructure the text. "
    "Output ONLY the raw text content of the page, nothing else. "
    "If the image is blank or unreadable, output an empty string."
)


def ocr_pdf_text(
    pdf_bytes: bytes,
    *,
    max_pages: int = _OCR_MAX_PAGES,
    page_timeout: float = _OCR_PAGE_TIMEOUT,
    total_timeout: float = _OCR_TOTAL_TIMEOUT,
) -> str | None:
    """Extract text from an image-only PDF using LLM vision API.

    Renders each PDF page to a PNG image using pymupdf, then sends the
    image to the configured LLM provider's vision API for OCR.

    This is a fallback for PDFs where ``pdfplumber.extract_text()`` returns
    no text (scanned/image-only documents).

    Args:
        pdf_bytes: Raw PDF file bytes.
        max_pages: Maximum number of pages to OCR (default: 10).
        page_timeout: Per-page LLM call timeout in seconds (default: 30).
        total_timeout: Total timeout across all pages in seconds (default: 120).

    Returns:
        Extracted text joined with form-feed separators, or ``None`` if OCR
        fails or returns no text.
    """
    import time as time_mod

    start_time = time_mod.monotonic()

    try:
        page_images = _render_pdf_pages(pdf_bytes, max_pages)
    except Exception:
        logger.warning("ocr_pdf_text.render_failed", exc_info=True)
        return None

    if not page_images:
        logger.warning("ocr_pdf_text.no_pages_rendered")
        return None

    logger.info(
        "ocr_pdf_text.starting",
        page_count=len(page_images),
        max_pages=max_pages,
    )

    pages_text: list[str] = []
    for i, (image_bytes, media_type) in enumerate(page_images):
        # Check total timeout
        elapsed = time_mod.monotonic() - start_time
        if elapsed >= total_timeout:
            logger.warning(
                "ocr_pdf_text.total_timeout",
                pages_completed=i,
                total_pages=len(page_images),
                elapsed_seconds=round(elapsed, 1),
            )
            break

        remaining = total_timeout - elapsed
        effective_timeout = min(page_timeout, remaining)

        text = _ocr_single_page(image_bytes, media_type, page_index=i, timeout=effective_timeout)
        if text:
            pages_text.append(text)

    if pages_text:
        result = "\n\f\n".join(pages_text)
        logger.info(
            "ocr_pdf_text.success",
            pages_ocrd=len(pages_text),
            total_pages=len(page_images),
            text_length=len(result),
        )
        return result

    logger.warning(
        "ocr_pdf_text.no_text_extracted",
        pages_attempted=len(page_images),
    )
    return None


def _render_pdf_pages(
    pdf_bytes: bytes,
    max_pages: int,
) -> list[tuple[bytes, str]]:
    """Render PDF pages to PNG images using pymupdf.

    Uses pymupdf (fitz) for higher quality image rendering compared to
    pdfplumber, matching the implementation in ``llm_extractor.py``
    (validated in the OC multimodal eval, PR #1692).

    Returns a list of ``(png_bytes, media_type)`` tuples.
    """
    import pymupdf

    results: list[tuple[bytes, str]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        zoom = _OCR_RESOLUTION / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)

        for i, page in enumerate(doc):
            if i >= max_pages:
                logger.info(
                    "ocr_pdf_text.page_limit_reached",
                    max_pages=max_pages,
                    total_pages=len(doc),
                )
                break

            pix = page.get_pixmap(matrix=matrix)
            png_bytes = pix.tobytes("png")
            results.append((png_bytes, "image/png"))

    return results


def _ocr_single_page(
    image_bytes: bytes,
    media_type: str,
    *,
    page_index: int,
    timeout: float,
) -> str | None:
    """OCR a single page image via the LLM vision API.

    Returns the extracted text, or ``None`` on failure.
    """
    try:
        response = call_llm_with_images(
            system_prompt=_OCR_SYSTEM_PROMPT,
            text_message=f"Extract all text from this court document page (page {page_index + 1}).",
            images=[(image_bytes, media_type)],
            timeout=timeout,
        )
        if response is None:
            logger.warning(
                "ocr_pdf_text.page_failed",
                page_index=page_index,
            )
            return None

        text = response.text.strip()
        if text:
            logger.debug(
                "ocr_pdf_text.page_success",
                page_index=page_index,
                text_length=len(text),
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        return text if text else None
    except Exception:
        logger.warning(
            "ocr_pdf_text.page_error",
            page_index=page_index,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# System prompt — now sourced from framework.llm_schema (co-located with the
# Pydantic schema so they stay in sync).  The ``_SYSTEM_PROMPT`` name is kept
# as a module-level alias for backward compatibility with existing tests and
# callers that import it directly.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = EXTRACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Case number normalization
# ---------------------------------------------------------------------------

# OC-style county prefix: "30-2024-01393434" → "2024-01393434"
_COUNTY_PREFIX_RE = re.compile(r"^\d{2,4}-(\d{4}-\d+)$")


def _normalize_case_number(raw: str) -> str:
    """Strip county prefix from case numbers (e.g. OC format)."""
    m = _COUNTY_PREFIX_RE.match(raw.strip())
    return m.group(1) if m else raw.strip()


# ---------------------------------------------------------------------------
# Department normalization — restore leading zeros
# ---------------------------------------------------------------------------

# Known department prefixes where the numeric suffix should be zero-padded
# to a specific width.  For example, OC Costa Mesa departments use "CM"
# followed by a 2-digit number (CM01–CM99).  When an LLM strips the
# leading zero (returning "CM2" instead of "CM02"), this mapping lets us
# restore it.
#
# Format: prefix -> expected digit width of the numeric suffix.
_DEPT_ZERO_PAD: dict[str, int] = {
    "CM": 2,  # OC Costa Mesa: CM01–CM99
    "CX": 2,  # OC Central Justice Center annex
    "CL": 2,  # OC Civil Complex Center
    "L": 2,  # OC Lamoreaux Justice Center
    "W": 2,  # OC West Justice Center
    "H": 2,  # OC Harbor Justice Center
}

# Regex: one or more ASCII letters, then one or more digits.
_DEPT_SPLIT_RE = re.compile(r"^([A-Za-z]+)(\d+)$")

# Regex: letter prefix, hyphen, digits — e.g. "S-17", "R-14".
# Used to strip spurious hyphens that LLMs insert between the prefix
# and numeric suffix (San Bernardino departments, etc.).
_DEPT_HYPHEN_RE = re.compile(r"^([A-Za-z]+)-(\d+)$")


def _normalize_department(raw: str) -> str:
    """Normalize department identifiers: strip hyphens, restore leading zeros.

    Two normalizations are applied in order:

    1. **Hyphen removal** — LLMs sometimes insert a hyphen between a
       letter prefix and numeric suffix (e.g. ``"S-17"`` instead of
       ``"S17"``).  This is common for San Bernardino departments.
       The hyphen is stripped so that ``"S-17"`` becomes ``"S17"``.

    2. **Zero-padding** — LLMs (especially Haiku) tend to drop leading
       zeros from department identifiers — e.g. returning ``"CM2"``
       instead of ``"CM02"``.  Known prefixes in ``_DEPT_ZERO_PAD``
       have their numeric suffix padded to the expected width.
    """
    cleaned = raw.strip()

    # Step 1: Strip hyphens between letter prefix and digits.
    hm = _DEPT_HYPHEN_RE.match(cleaned)
    if hm:
        cleaned = f"{hm.group(1)}{hm.group(2)}"

    # Step 2: Zero-pad known prefixes.
    m = _DEPT_SPLIT_RE.match(cleaned)
    if not m:
        return cleaned

    prefix = m.group(1).upper()
    digits = m.group(2)

    expected_width = _DEPT_ZERO_PAD.get(prefix)
    if expected_width is None:
        return cleaned

    # Preserve original casing of the prefix from the input.
    original_prefix = m.group(1)
    return f"{original_prefix}{digits.zfill(expected_width)}"


# ---------------------------------------------------------------------------
# Date parsing helper
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> date | None:
    """Parse a date string in YYYY-MM-DD format, returning None on failure."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------

# Hard cap on chunks per document to control cost.
# 10 chunks x 80K chars/chunk = ~800K effective coverage (minus overlap),
# sufficient for the largest LA Superior Court multi-ruling documents.
_MAX_CHUNKS = 10

# Overlap characters between consecutive chunks for context continuity.
_CHUNK_OVERLAP = 500

# Patterns that indicate natural split boundaries in text.
_PAGE_BREAK_RE = re.compile(r"\f")  # Form feed (pymupdf page separator)
_HR_RE = re.compile(r"<HR[^>]*>", re.IGNORECASE)
# Case number patterns used as split candidates in text.
_CASE_BOUNDARY_RE = re.compile(
    r"\n(?=\s*(?:Case\s+(?:Number|No\.?)|CASE\s+(?:NUMBER|NO\.?))\s*[:\s])",
    re.IGNORECASE,
)


def _split_text_into_chunks(
    text: str,
    max_chars: int,
    content_format: str,
) -> list[str]:
    """Split *text* into chunks of at most *max_chars* characters.

    Splitting strategy:
    - For PDFs: split at form-feed (``\\f``) page boundaries first.
    - For HTML (post-stripping): split at ``<HR>`` tags or case-number
      boundaries.
    - Fallback: split on double-newline paragraph boundaries.

    Each chunk (except the first) is prefixed with the last
    ``_CHUNK_OVERLAP`` characters of the previous chunk so the model has
    continuity context.

    Returns at most ``_MAX_CHUNKS`` chunks.  If the document is so large
    that even *_MAX_CHUNKS* chunks of *max_chars* cannot cover it, the
    tail is silently truncated and a warning is logged.
    """
    if len(text) <= max_chars:
        return [text]

    # Choose boundary pattern based on format.
    if content_format == "pdf":
        boundaries = [m.start() for m in _PAGE_BREAK_RE.finditer(text)]
    else:
        # HTML: try <HR> first, then case-number boundaries.
        boundaries = [m.start() for m in _HR_RE.finditer(text)]
        if not boundaries:
            boundaries = [m.start() for m in _CASE_BOUNDARY_RE.finditer(text)]

    # Fallback: double-newline paragraph breaks.
    if not boundaries:
        boundaries = [m.start() for m in re.finditer(r"\n\n", text)]

    # If still no boundaries (wall of text), force-split at max_chars.
    if not boundaries:
        return _force_split(text, max_chars)

    # Greedy packing: walk through boundaries, emitting a chunk whenever
    # the *next* boundary would push us past max_chars.  We also track
    # the last boundary that fits so we always split at a natural point.
    chunks: list[str] = []
    chunk_start = 0
    last_boundary = 0  # last boundary position within the current chunk

    for boundary in boundaries:
        span = boundary - chunk_start
        if span > max_chars:
            # The region from chunk_start to this boundary exceeds the
            # limit.  Split at the *previous* boundary if we have one,
            # otherwise at this boundary.
            split_at = last_boundary if last_boundary > chunk_start else boundary
            chunks.append(text[chunk_start:split_at])
            chunk_start = max(split_at - _CHUNK_OVERLAP, 0)
            if len(chunks) >= _MAX_CHUNKS:
                break
        last_boundary = boundary

    # Append the remaining text.
    if len(chunks) < _MAX_CHUNKS and chunk_start < len(text):
        remaining = text[chunk_start:]
        if len(remaining) > max_chars:
            # Still too large — split the remainder at boundaries or
            # force-split as a fallback.
            sub_chunks = _force_split(remaining, max_chars)
            for sc in sub_chunks:
                if len(chunks) >= _MAX_CHUNKS:
                    break
                chunks.append(sc)
        else:
            chunks.append(remaining)

    if not chunks:
        chunks = [text[:max_chars]]

    if len(chunks) >= _MAX_CHUNKS:
        # Check if we covered the full text.
        covered = sum(len(c) for c in chunks)
        if covered < len(text):
            logger.warning(
                "llm_extract.chunk_truncated",
                total_chars=len(text),
                chunks=len(chunks),
            )

    return chunks


def _force_split(text: str, max_chars: int) -> list[str]:
    """Split *text* into fixed-size chunks with overlap (no natural boundaries)."""
    chunks: list[str] = []
    pos = 0
    while pos < len(text) and len(chunks) < _MAX_CHUNKS:
        end = min(pos + max_chars, len(text))
        chunks.append(text[pos:end])
        pos = end - _CHUNK_OVERLAP if end < len(text) else end
    if len(text) > pos:
        logger.warning(
            "llm_extract.chunk_truncated",
            total_chars=len(text),
            chunks=len(chunks),
        )
    return chunks


def _merge_results(
    results: list[LLMExtractionResult],
) -> LLMExtractionResult:
    """Merge extraction results from multiple chunks into a single result.

    - Document-level fields (judge, department, hearing_date) are taken
      from the *first* chunk (headers appear at the top of documents).
    - Rulings are concatenated and deduplicated by ``case_number``.
    - ``case_count`` is the number of unique rulings after dedup.
    """
    if not results:
        return LLMExtractionResult()

    first = results[0]

    # Collect all rulings, dedup by case_number (keep first occurrence).
    seen_case_numbers: set[str] = set()
    unique_rulings: list[LLMRulingResult] = []

    for result in results:
        for ruling in result.rulings:
            key = ruling.case_number
            if key and key in seen_case_numbers:
                continue
            if key:
                seen_case_numbers.add(key)
            unique_rulings.append(ruling)

    return LLMExtractionResult(
        judge_name=first.judge_name,
        hearing_date=first.hearing_date,
        department=first.department,
        case_count=len(unique_rulings),
        rulings=unique_rulings,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Default per-chunk character budget.  80K chars leaves room for the
# system prompt and output tokens within the model's context window.
_DEFAULT_MAX_CHARS = 80_000


def extract_fields_llm(
    document_text: str,
    content_format: str,  # "html" or "pdf"
    metadata: dict[str, str] | None = None,
    client: object | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    timeout: float | None = None,
    max_total_chars: int | None = None,
    token_tracker: TokenTracker | None = None,
    max_tokens: int = 4096,
) -> LLMExtractionResult | None:
    """Extract structured fields from a court ruling via a configurable LLM.

    The provider and model are resolved from the explicit arguments, then from
    ``LLM_PROVIDER`` / ``LLM_MODEL`` environment variables, then from built-in
    defaults (Google Gemini 2.5 Flash Lite).

    If the document exceeds *max_chars* after preprocessing, it is
    automatically split into overlapping chunks, each extracted
    independently, and the results merged.  This is transparent to the
    caller — the return type is always a single ``LLMExtractionResult``.

    Supports documents up to ~800K characters via chunking (10 chunks x 80K
    chars/chunk).  Callers that want to enforce a hard size limit can pass
    *max_total_chars*; documents exceeding it are skipped (returns ``None``).

    Args:
        document_text: Raw document content (HTML or plain text from PDF).
        content_format: ``"html"`` or ``"pdf"``.
        metadata: Optional dict with authoritative scraper-provided context.
            May contain keys: ``link_text``, ``judge_name``, ``department``.
        client: Optional pre-created provider client for connection reuse.
            Accepts an ``anthropic.Anthropic`` or ``google.genai.Client``
            depending on the provider.  If ``None``, the provider adapter
            creates one from the appropriate env var.
        provider: LLM provider name (``"google"``, ``"anthropic"``).
            Falls back to ``LLM_PROVIDER`` env var, then ``"google"``.
        model: Model ID.  Falls back to ``LLM_MODEL`` env var, then a
            per-provider default.
        max_chars: Per-chunk character limit.  Documents under this size
            are processed in a single call (no chunking overhead).
        timeout: Per-call timeout in seconds for each LLM API call.
            If ``None``, no timeout is applied.  On timeout, the call
            returns ``None`` and the caller falls back to regex extraction.
        max_total_chars: Optional hard limit on total (preprocessed) text
            size.  Documents exceeding this are skipped entirely and the
            function returns ``None``.  Default: ``None`` (no limit —
            documents of any size are processed via chunking).
        token_tracker: Optional ``TokenTracker`` to accumulate token usage
            across multiple calls.  Thread-safe — can be shared across
            concurrent ``extract_fields_llm()`` invocations.
        max_tokens: Maximum output tokens for each LLM API call.  Defaults
            to 4096.  Counties with large documents (e.g. Santa Clara,
            130K+ chars) need a higher value (e.g. 32768) to avoid
            truncated JSON responses.

    Returns:
        An ``LLMExtractionResult`` with extracted fields, or ``None`` if
        the API call fails (caller should fall back to regex extraction).
    """
    if not document_text or not document_text.strip():
        return None

    # LLM result cache — check before doing any work.
    cache = _get_llm_cache(provider, model)
    cache_key = _compute_cache_key(document_text, metadata)
    if cache is not None:
        cached = cache.get(_SYSTEM_PROMPT, cache_key)
        if cached is not None:
            logger.debug("llm_extract.cache_hit", cache_key=cache_key[:12])
            return _deserialize_result(cached)

    # Preprocess based on content format
    if content_format == "html":
        text = preprocess_html(document_text)
    elif content_format == "pdf" and is_pdf_binary(document_text):
        # Raw PDF binary was passed instead of extracted text — extract it now.
        extracted = extract_text_from_pdf(document_text)
        if not extracted:
            logger.warning("llm_extract.pdf_extraction_empty")
            return None
        text = extracted
    else:
        text = document_text

    # Enforce optional hard size limit (after preprocessing, since HTML
    # stripping can dramatically reduce size — e.g. 618K HTML -> 5K text).
    if max_total_chars is not None and len(text) > max_total_chars:
        logger.warning(
            "llm_extract.text_too_large",
            total_chars=len(text),
            max_total_chars=max_total_chars,
        )
        return None

    # Split into chunks if needed
    chunks = _split_text_into_chunks(text, max_chars, content_format)

    if len(chunks) > 1:
        logger.info(
            "llm_extract.chunked",
            total_chars=len(text),
            num_chunks=len(chunks),
            chunk_sizes=[len(c) for c in chunks],
        )

    # Extract from each chunk — concurrently when multiple chunks exist.
    # Each chunk's LLM call is independent (shared only prompt + metadata),
    # so we use ThreadPoolExecutor to process them in parallel.  This
    # reduces wall-clock time for a 5-chunk document from ~15s to ~3s
    # (limited by the slowest single chunk).

    def _extract_single_chunk(
        chunk_index_and_text: tuple[int, str],
    ) -> LLMExtractionResult | None:
        """Process a single chunk through the LLM and parse the response."""
        i, chunk = chunk_index_and_text
        user_message = _build_user_message(chunk, content_format, metadata)
        llm_response = call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            provider=provider,
            model=model,
            client=client,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        if llm_response is None:
            logger.warning("llm_extract.chunk_api_failure", chunk_index=i)
            return None
        if token_tracker is not None:
            token_tracker.add(llm_response.input_tokens, llm_response.output_tokens)
        return _parse_response(llm_response.text, metadata)

    if len(chunks) == 1:
        # Single chunk — no thread overhead needed.
        result = _extract_single_chunk((0, chunks[0]))
        chunk_results: list[LLMExtractionResult] = [result] if result is not None else []
    else:
        # Multiple chunks — process concurrently.  executor.map preserves
        # input order, which is critical: _merge_results takes document-
        # level fields (judge, department, date) from the *first* result.
        max_workers = min(len(chunks), _MAX_CHUNKS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            ordered_results = list(executor.map(_extract_single_chunk, enumerate(chunks)))
        chunk_results = [r for r in ordered_results if r is not None]

    if not chunk_results:
        return None

    # Single chunk: return directly.  Multiple: merge.
    result = chunk_results[0] if len(chunk_results) == 1 else _merge_results(chunk_results)

    # Write to cache
    if cache is not None and result is not None:
        cache.put(_SYSTEM_PROMPT, cache_key, _serialize_result(result))

    return result


# ---------------------------------------------------------------------------
# LLM result cache helpers (shared S3 cache via framework.llm_extractor._LlmCache)
# ---------------------------------------------------------------------------

_cache_instance: object | None = None
_cache_lock = threading.Lock()


def _get_llm_cache(provider: str | None, model: str | None) -> object | None:
    """Return the shared _LlmCache instance, or None if no bucket configured."""
    global _cache_instance  # noqa: PLW0603
    if _cache_instance is not None:
        return _cache_instance

    bucket = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "")
    if not bucket:
        return None

    with _cache_lock:
        if _cache_instance is not None:
            return _cache_instance
        from framework.llm_extractor import _LlmCache
        from framework.s3_cache import make_s3_client

        resolved_provider = provider or os.environ.get("LLM_PROVIDER", "google")
        resolved_model = model or os.environ.get("LLM_MODEL", "gemini-2.5-flash-lite")
        _cache_instance = _LlmCache(
            s3_client=make_s3_client(),
            bucket=bucket,
            provider=resolved_provider,
            model=resolved_model,
        )
        return _cache_instance


def _compute_cache_key(text: str, metadata: dict[str, str] | None) -> str:
    """Hash text + metadata for cache lookup."""
    h = hashlib.sha256()
    h.update(text.encode())
    if metadata:
        h.update(json.dumps(metadata, sort_keys=True).encode())
    return h.hexdigest()


def _serialize_result(result: LLMExtractionResult) -> list[dict]:
    """Serialize LLMExtractionResult for cache storage.

    ``dataclasses.asdict()`` recursively converts dataclass fields to dicts,
    but it does **not** convert Pydantic ``BaseModel`` instances (like
    ``FieldConfidence``).  We explicitly call ``model_dump()`` on each
    ruling's confidence so the result is fully JSON-serializable.
    """
    d = asdict(result)
    # Convert date to string for JSON
    if d.get("hearing_date") is not None:
        d["hearing_date"] = d["hearing_date"].isoformat()
    # Convert FieldConfidence (Pydantic BaseModel) to plain dicts.
    # asdict() leaves Pydantic models as-is, which breaks json.dumps().
    for ruling_dict in d.get("rulings", []):
        conf = ruling_dict.get("confidence")
        if conf is not None and isinstance(conf, FieldConfidence):
            ruling_dict["confidence"] = conf.model_dump(mode="json")
    return [d]


def _deserialize_result(cached: list[dict]) -> LLMExtractionResult | None:
    """Deserialize cached data back to LLMExtractionResult."""
    if not cached:
        return None
    d = cached[0]
    # Convert hearing_date string back to date
    if d.get("hearing_date") is not None:
        d["hearing_date"] = date.fromisoformat(d["hearing_date"])
    # Reconstruct nested dataclasses, including FieldConfidence from dict.
    raw_rulings = d.pop("rulings", [])
    rulings: list[LLMRulingResult] = []
    for r in raw_rulings:
        conf_data = r.pop("confidence", None)
        confidence = FieldConfidence(**conf_data) if conf_data else FieldConfidence()
        rulings.append(LLMRulingResult(**r, confidence=confidence))
    return LLMExtractionResult(**d, rulings=rulings)


def _build_user_message(
    text: str,
    content_format: str,
    metadata: dict[str, str] | None,
) -> str:
    """Build the user message for a single chunk."""
    user_parts: list[str] = []
    if metadata:
        meta_lines: list[str] = []
        if metadata.get("judge_name"):
            meta_lines.append(f"Judge name (authoritative): {metadata['judge_name']}")
        if metadata.get("department"):
            meta_lines.append(f"Department (authoritative): {metadata['department']}")
        if metadata.get("link_text"):
            meta_lines.append(f"Link text: {metadata['link_text']}")
        if meta_lines:
            user_parts.append("Metadata from scraper:\n" + "\n".join(meta_lines))

    user_parts.append(f"Document ({content_format}):\n\n{text}")
    return "\n\n".join(user_parts)


_VALID_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})


def _parse_confidence(raw: dict[str, str] | None) -> FieldConfidence:
    """Parse a confidence dict from the LLM response into a ``FieldConfidence``.

    Unknown or invalid confidence values are defaulted to ``ConfidenceLevel.HIGH``
    (the default assumption when the LLM does not explicitly flag uncertainty).
    """
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


def _parse_response(
    raw_text: str,
    metadata: dict[str, str] | None,
) -> LLMExtractionResult | None:
    """Parse the JSON response from the model into an ``LLMExtractionResult``."""
    try:
        # Strip markdown code fences if present
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("llm_extract.json_parse_error", error=str(exc), raw=raw_text[:200])
        return None

    # Extract top-level fields — support both old field names (judge_name)
    # and new extracted_ prefix names (extracted_judge_name) for backward
    # compatibility during the transition.
    judge_name = parsed.get("extracted_judge_name") or parsed.get("judge_name")
    hearing_date = _parse_date(parsed.get("hearing_date"))
    department = parsed.get("department")

    # Normalize department leading zeros (fix LLM zero-stripping)
    if department and isinstance(department, str):
        department = _normalize_department(department)

    # Override with authoritative metadata
    if metadata:
        if metadata.get("judge_name"):
            judge_name = metadata["judge_name"]
        if metadata.get("department"):
            department = metadata["department"]

    # Parse rulings array
    raw_rulings = parsed.get("rulings", [])
    if not isinstance(raw_rulings, list):
        raw_rulings = []

    rulings: list[LLMRulingResult] = []
    for r in raw_rulings:
        if not isinstance(r, dict):
            continue

        # Normalize case number — support both old and new field names
        case_number = r.get("extracted_case_number") or r.get("case_number")
        if case_number:
            case_number = _normalize_case_number(str(case_number))

        # Case title — support both old and new field names
        case_title = r.get("extracted_case_title") or r.get("case_title")

        # Validate case_type against enum
        case_type = r.get("case_type")
        if case_type and case_type not in CASE_TYPE_VALUES:
            case_type = "other"

        # Validate outcome against enum
        outcome = r.get("outcome")
        if outcome and outcome not in OUTCOME_VALUES:
            outcome = "other"

        # Parse parties — support both old (parties) and new
        # (extracted_parties) field names.  Validate that names are
        # plausible (not garbage text blocks).
        raw_parties = r.get("extracted_parties") or r.get("parties", [])
        parties: list[dict[str, str]] = []
        if isinstance(raw_parties, list):
            for p in raw_parties:
                if isinstance(p, dict) and p.get("name") and p.get("role"):
                    name = str(p["name"]).strip()
                    if len(name) > 200 or "\n" in name or "\r" in name:
                        logger.warning(
                            "llm_extract.invalid_party_name",
                            length=len(name),
                            preview=name[:80],
                        )
                        continue
                    party_entry: dict[str, str] = {
                        "name": name,
                        "role": str(p["role"]),
                    }
                    # Preserve per-party confidence if provided by the LLM
                    raw_party_conf = p.get("confidence")
                    if raw_party_conf and raw_party_conf in _VALID_CONFIDENCE_LEVELS:
                        party_entry["confidence"] = raw_party_conf
                    parties.append(party_entry)

        # Parse confidence scores (new in schema v3)
        raw_confidence = r.get("confidence", {})
        confidence = _parse_confidence(raw_confidence)

        rulings.append(
            LLMRulingResult(
                case_number=case_number,
                case_title=case_title,
                case_type=case_type,
                outcome=outcome,
                motion_type=r.get("motion_type"),
                parties=parties,
                confidence=confidence,
            )
        )

    return LLMExtractionResult(
        judge_name=judge_name,
        hearing_date=hearing_date,
        department=str(department) if department else None,
        case_count=len(rulings),
        rulings=rulings,
    )
