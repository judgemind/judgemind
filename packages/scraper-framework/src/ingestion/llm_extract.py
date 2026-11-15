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
from framework.llm_utils import parse_llm_json, rulings_array_shape_error

from .llm_providers import call_llm, call_llm_with_images, response_diagnostics

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
    case_number_prefix: str | None = None


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


#: How ``extract_pdf_text`` joins a PDF's pages.
PAGE_SEPARATOR = "\n\f\n"

#: A page with fewer non-blank text-layer characters than this, and a
#: picture over at least ``_SCANNED_PAGE_IMAGE_SHARE`` of it, is a scanned
#: page (#5010): its text layer is at most a stamp or a footer, and its
#: content is the picture.  A page with no text layer and any picture is
#: one too.  A text page with a logo stays a text page.
_SCANNED_PAGE_MAX_TEXT_CHARS = 200
_SCANNED_PAGE_IMAGE_SHARE = 0.5

#: OCR that adds fewer non-blank characters than this to a scanned page's
#: own text layer found nothing the text layer lacks (a blank page, a page
#: of one footer), so the page keeps its text layer and no OCR span.
_MIN_OCR_GAIN_CHARS = 20

#: ``CapturedDocument.extra`` key a scraper sets when it read a PDF's text
#: layer (``scraper_pdf_text``) and some pages are scanned (#5010): the
#: 1-based page numbers.  The scraper's text lacks those pages, so the
#: worker reads the archived PDF again and OCRs them, as a reingest of the
#: key does.
SCANNED_PAGES_KEY = "pdf_scanned_pages"


@dataclass(frozen=True)
class PdfText:
    """A PDF's extracted text and where it came from (#4986, #5010).

    ``from_ocr`` marks text that is, wholly or in part, the LLM's
    transcription of page images (``ocr_pdf_document``): the PDF's scanned
    pages.  That text is LLM output, so it is never case-number evidence
    (``case_evidence``).  When only some pages were scanned and the text
    holds OCR text or lacks pages, ``text_layer`` holds the rest: each run
    of consecutive text-layer pages, joined as in ``text``.  A slice of
    ``text`` inside one of them is source text
    (``case_evidence.text_from_ocr``); the text as a whole is not.

    ``incomplete`` says why the text lacks part of the document (the OCR
    page cap, the time budget, pages whose LLM call failed, or, with OCR
    off, the scanned pages themselves), or is ``None`` when it has every
    page.  An extraction built on incomplete text is missing rulings, so
    the worker treats it as a failed extraction rather than a complete
    one.  ``page_count`` is the PDF's page count.
    """

    text: str
    from_ocr: bool = False
    page_count: int = 0
    incomplete: str | None = None
    text_layer: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PdfPage:
    """One page's text layer, and whether the page is scanned."""

    text: str
    scanned: bool


def _pdf_bytes(content: str | bytes) -> bytes:
    if isinstance(content, bytes):
        return content
    # Re-encode to bytes — PDF content decoded as latin-1 round-trips cleanly;
    # UTF-8 decoded content may have replacement chars but we try anyway.
    try:
        return content.encode("latin-1")
    except UnicodeEncodeError:
        return content.encode("utf-8", errors="replace")


def _non_blank(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def _image_share(page: object) -> float:
    """The share of *page* its pictures cover (overlaps counted twice)."""
    width = float(getattr(page, "width", 0) or 0)
    height = float(getattr(page, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return 0.0
    covered = 0.0
    for image in getattr(page, "images", None) or ():
        x0, x1 = max(0.0, float(image["x0"])), min(width, float(image["x1"]))
        top, bottom = max(0.0, float(image["top"])), min(height, float(image["bottom"]))
        covered += max(0.0, x1 - x0) * max(0.0, bottom - top)
    return covered / (width * height)


def _read_pdf_pages(raw_bytes: bytes) -> list[_PdfPage]:
    """Each page's pdfplumber text layer, and whether it is scanned.

    Raises when *raw_bytes* is not a readable PDF.  The one place a PDF's
    text layer is read (#5010): the worker, the scrapers and the audits all
    come through here, so a scanned page inside a text PDF is never
    silently skipped.
    """
    import pdfplumber

    pages: list[_PdfPage] = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chars = _non_blank(text)
            scanned = chars < _SCANNED_PAGE_MAX_TEXT_CHARS and (
                _image_share(page) >= _SCANNED_PAGE_IMAGE_SHARE
                or (chars == 0 and bool(page.images))
            )
            pages.append(_PdfPage(text, scanned))
    return pages


def _scanned_page_indexes(pages: list[_PdfPage]) -> list[int]:
    """0-based indexes of the pages whose content needs OCR.  A PDF with no
    text layer on any page is all scanned, pictures or not (#1332)."""
    if pages and not any(_non_blank(p.text) for p in pages):
        return list(range(len(pages)))
    return [i for i, p in enumerate(pages) if p.scanned]


def _page_list(indexes: list[int]) -> list[int]:
    return [i + 1 for i in indexes]


def extract_pdf_text(
    content: str | bytes,
    *,
    ocr: bool = True,
    ocr_max_pages: int | None = None,
    separator: str = PAGE_SEPARATOR,
) -> PdfText | None:
    """Extract a PDF's text, page by page, and say where it came from.

    Accepts either raw ``bytes`` or a ``str`` that was produced by decoding
    raw PDF bytes (common when PDF content is stored as a text field).

    Each page's text layer is read with pdfplumber.  A scanned page (no
    usable text layer, its content a picture, ``_read_pdf_pages``) is
    OCR'd with the LLM vision API when *ocr* is set (``ocr_pdf_document``,
    at most *ocr_max_pages* pages, default ``_OCR_MAX_PAGES``), and its
    text takes the page's place, in page order (#5010).  Text that holds
    OCR output has ``from_ocr`` set; the text-layer runs around it are in
    ``text_layer``.  Pages OCR could not read (the cap, the time budget,
    a failed call) leave the result ``incomplete``; with *ocr* off every
    scanned page does.  OCR that read nothing because pages failed is
    returned too, with empty text and ``incomplete`` set, so a caller can
    tell a transient failure from a PDF with nothing on it.

    Pages are joined with *separator*.

    Returns ``None`` when the content is not a readable PDF, or when there
    is no text at all and nothing went unread (or *ocr* is off).
    """
    raw_bytes = _pdf_bytes(content)
    try:
        pages = _read_pdf_pages(raw_bytes)
    except ImportError:
        logger.warning("pdfplumber not installed — cannot extract PDF text")
        return None
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return None
    page_count = len(pages)
    scanned = _scanned_page_indexes(pages)

    if scanned and len(scanned) == page_count and ocr:
        # No text layer at all: an image-only PDF.  OCR every page via the
        # LLM vision API (#1332).
        logger.info(
            "pdf_extraction.no_text_layer",
            pdf_size=len(raw_bytes),
            page_count=page_count,
            fallback="ocr_llm_vision",
        )
        max_pages = _OCR_MAX_PAGES if ocr_max_pages is None else ocr_max_pages
        try:
            result = ocr_pdf_document(raw_bytes, max_pages=max_pages)
        except Exception:
            logger.warning("PDF OCR fallback failed", exc_info=True)
            return None
        if result is None or (not result.text and result.incomplete is None):
            return None
        return result

    texts = [p.text for p in pages]
    ocr_read: set[int] = set()
    blank: set[int] = set()
    problems: list[str] = []
    if scanned and ocr:
        # A text PDF with scanned pages (a calendar with scanned orders
        # appended): OCR exactly those pages (#5010).
        logger.info(
            "pdf_extraction.scanned_pages",
            pdf_size=len(raw_bytes),
            page_count=page_count,
            scanned_pages=_page_list(scanned),
            fallback="ocr_llm_vision",
        )
        max_pages = _OCR_MAX_PAGES if ocr_max_pages is None else ocr_max_pages
        try:
            ocr_texts, ocr_problems = _ocr_pages(raw_bytes, scanned, max_pages=max_pages)
        except Exception:
            logger.warning("PDF OCR of scanned pages failed", exc_info=True)
            ocr_texts, ocr_problems = {}, [f"OCR of page(s) {_page_list(scanned)} failed"]
        problems.extend(ocr_problems)
        for index, text in ocr_texts.items():
            if text is None:
                continue
            if _non_blank(text) >= _non_blank(texts[index]) + _MIN_OCR_GAIN_CHARS:
                texts[index] = text
                ocr_read.add(index)
            else:
                # Read, and nothing on it the text layer lacks.
                blank.add(index)
    elif scanned:
        problems.append(f"page(s) {_page_list(scanned)} have no text layer")

    # The text-layer runs break at every scanned page that is not plain
    # text layer: OCR'd, or never read (#5010).
    breaks = set(scanned) - blank
    joined: list[str] = []
    layer_runs: list[list[str]] = [[]]
    for index, text in enumerate(texts):
        if index in breaks:
            layer_runs.append([])
        elif text:
            layer_runs[-1].append(text)
        if text:
            joined.append(text)
    incomplete = "; ".join(problems) or None
    if not joined and (incomplete is None or not ocr):
        return None
    from_ocr = bool(ocr_read)
    mixed = from_ocr or incomplete is not None
    return PdfText(
        separator.join(joined),
        from_ocr=from_ocr,
        page_count=page_count,
        incomplete=incomplete,
        text_layer=tuple(separator.join(run) for run in layer_runs if run) if mixed else (),
    )


def scraper_pdf_text(pdf_bytes: bytes, *, extra: dict | None = None) -> str:
    """A PDF's text layer for a scraper's parse, pages joined with ``"\\n"``
    as the scrapers always joined them (#5010).

    No OCR: a scraper runs no LLM.  When some pages are scanned, their
    1-based numbers go in *extra* (the document's ``extra``) under
    ``SCANNED_PAGES_KEY``, so the worker reads the archived PDF again and
    OCRs them; the text returned lacks them.  Raises when *pdf_bytes* is
    not a readable PDF.
    """
    pages = _read_pdf_pages(_pdf_bytes(pdf_bytes))
    scanned = _scanned_page_indexes(pages)
    if extra is not None:
        if scanned:
            extra[SCANNED_PAGES_KEY] = _page_list(scanned)
        else:
            extra.pop(SCANNED_PAGES_KEY, None)
    return "\n".join(p.text for p in pages if p.text)


def pdf_scanned_pages(content: str | bytes) -> list[int]:
    """The 1-based numbers of a PDF's scanned pages (``_read_pdf_pages``):
    the pages ``extract_pdf_text`` OCRs.  Raises when *content* is not a
    readable PDF.  For audits sizing the text PDFs with scanned pages
    (#5010)."""
    return _page_list(_scanned_page_indexes(_read_pdf_pages(_pdf_bytes(content))))


def extract_text_from_pdf(content: str | bytes, *, ocr_max_pages: int | None = None) -> str | None:
    """Extract readable text from PDF binary content (``extract_pdf_text``).

    Falls back to OCR for scanned pages.  Returns the text, or ``None``
    when extraction fails or produces no text.  A caller that judges case
    numbers by the text must use ``extract_pdf_text`` instead: OCR text is
    LLM output, not evidence (#4986).
    """
    result = extract_pdf_text(content, ocr_max_pages=ocr_max_pages)
    return result.text if result is not None and result.text else None


def extract_text_layer(content: str | bytes) -> str | None:
    """A PDF's own text layer (pdfplumber), never OCR.

    The text the case-number gate can count as evidence: OCR text never is
    (#4986), so a PDF with no text layer has none.  ``None`` when there is
    no text layer or the content is not a readable PDF.
    """
    result = extract_pdf_text(content, ocr=False)
    return result.text if result is not None and result.text else None


def pdf_page_count(content: str | bytes) -> int:
    """The PDF's page count (pymupdf); 0 when it cannot be read."""
    try:
        import pymupdf

        with pymupdf.open(stream=_pdf_bytes(content), filetype="pdf") as doc:
            return len(doc)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# OCR fallback for image-only PDFs (#1332)
# ---------------------------------------------------------------------------

# Maximum number of PDF pages to OCR (#4986).  The cap was 10 when the
# fallback landed (#1332), a cost guess that silently dropped every ruling
# past page 10 of a long scanned calendar (a 22-page Santa Clara scan).
# Court calendars run to a few dozen pages; this bounds a runaway document,
# and a PDF that hits it is OCR'd up to the cap and marked incomplete
# (``PdfText.incomplete``), with an ``ocr_pdf_text.page_limit_reached``
# warning, never silently truncated.
# page-cap: OCR past it is PdfText.incomplete; the worker refuses (#4986).
_OCR_MAX_PAGES = 100

# Pages OCR'd concurrently.  Each page is one vision call; a 22-page scan
# read one page at a time used to blow the old 120 s total budget.
_OCR_CONCURRENCY = 4

# Resolution (DPI) for rendering PDF pages to images for OCR.
# 150 DPI balances readability with image size (token cost).
_OCR_RESOLUTION = 150

# Per-page timeout for the LLM vision call (seconds).
_OCR_PAGE_TIMEOUT = 30.0

# Minimum total time budget for all pages (seconds).  The budget grows with
# the page count: every page gets its own timeout, ``_OCR_CONCURRENCY`` at
# a time (``_ocr_total_timeout``).
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


def _ocr_total_timeout(pages: int, page_timeout: float, concurrency: int) -> float:
    rounds = -(-pages // max(1, concurrency))
    return max(_OCR_TOTAL_TIMEOUT, rounds * page_timeout)


def ocr_pdf_text(
    pdf_bytes: bytes,
    *,
    max_pages: int = _OCR_MAX_PAGES,
    page_timeout: float = _OCR_PAGE_TIMEOUT,
    total_timeout: float | None = None,
) -> str | None:
    """Extract text from an image-only PDF using the LLM vision API.

    The text of ``ocr_pdf_document``, or ``None`` when OCR fails or returns
    no text.  It does not say whether every page was read; callers that
    need to know use ``ocr_pdf_document``.
    """
    result = ocr_pdf_document(
        pdf_bytes,
        max_pages=max_pages,
        page_timeout=page_timeout,
        total_timeout=total_timeout,
    )
    return result.text if result is not None and result.text else None


def ocr_pdf_document(
    pdf_bytes: bytes,
    *,
    max_pages: int = _OCR_MAX_PAGES,
    page_timeout: float = _OCR_PAGE_TIMEOUT,
    total_timeout: float | None = None,
    concurrency: int = _OCR_CONCURRENCY,
) -> PdfText | None:
    """OCR an image-only PDF with the LLM vision API (#1332, #4986).

    Every page goes through ``_ocr_pages``: at most *max_pages* pages,
    *concurrency* at a time, each with *page_timeout* seconds, the whole
    run *total_timeout* seconds.  Pages are joined in page order with
    form-feed separators.  The fallback for PDFs where
    ``pdfplumber.extract_text()`` returns no text (scanned / image-only
    documents).  A page cap hit, a timeout or a failed page call leaves the
    result ``incomplete`` (with a warning naming the pages read): a blank
    page is not a failure.

    Returns a ``PdfText`` with ``from_ocr=True`` (its text empty when no
    page produced text), or ``None`` when the PDF cannot be rendered.
    """
    try:
        texts, problems, total_pages = _ocr_pages_counted(
            pdf_bytes,
            None,
            max_pages=max_pages,
            page_timeout=page_timeout,
            total_timeout=total_timeout,
            concurrency=concurrency,
        )
    except Exception:
        logger.warning("ocr_pdf_text.render_failed", exc_info=True)
        return None
    ordered = [text for _, text in sorted(texts.items()) if text]
    result = PdfText(
        PAGE_SEPARATOR.join(ordered),
        from_ocr=True,
        page_count=total_pages,
        incomplete="; ".join(problems) or None,
    )
    if result.text:
        logger.info(
            "ocr_pdf_text.success",
            pages_ocrd=len(ordered),
            total_pages=total_pages,
            text_length=len(result.text),
            complete=result.incomplete is None,
        )
    elif texts:
        logger.warning("ocr_pdf_text.no_text_extracted", pages_attempted=len(texts))
    return result


def _ocr_pages(
    pdf_bytes: bytes,
    indexes: list[int],
    *,
    max_pages: int = _OCR_MAX_PAGES,
) -> tuple[dict[int, str | None], list[str]]:
    """OCR the 0-based pages *indexes* of a PDF (its scanned pages, #5010).

    Returns each answered page's text (``""`` blank, ``None`` a failed
    call) and the reasons any page went unread: the page cap (at most
    *max_pages* pages are read), the time budget, failed calls.  Raises
    when the PDF cannot be rendered.
    """
    texts, problems, _total = _ocr_pages_counted(pdf_bytes, indexes, max_pages=max_pages)
    return texts, problems


def _ocr_pages_counted(
    pdf_bytes: bytes,
    indexes: list[int] | None,
    *,
    max_pages: int = _OCR_MAX_PAGES,
    page_timeout: float = _OCR_PAGE_TIMEOUT,
    total_timeout: float | None = None,
    concurrency: int = _OCR_CONCURRENCY,
) -> tuple[dict[int, str | None], list[str], int]:
    """``_ocr_pages`` of *indexes* (every page when ``None``), plus the
    PDF's page count.

    Renders each page to a PNG with pymupdf and sends the pages to the
    configured provider's vision API, *concurrency* at a time, each with
    *page_timeout* seconds.  The whole run gets *total_timeout* seconds
    (default: one *page_timeout* per round of *concurrency* pages, at least
    ``_OCR_TOTAL_TIMEOUT``); pages not answered by then are abandoned.
    """
    import time as time_mod
    from concurrent.futures import FIRST_COMPLETED, wait

    start_time = time_mod.monotonic()
    page_images, total_pages = _render_pdf_pages_counted(pdf_bytes, max_pages, indexes)
    wanted = total_pages if indexes is None else len(indexes)

    problems: list[str] = []
    if wanted > len(page_images):
        logger.warning(
            "ocr_pdf_text.page_limit_reached",
            max_pages=max_pages,
            total_pages=total_pages,
            pages_to_read=wanted,
        )
        problems.append(f"page cap {max_pages} of {wanted} pages")

    if not page_images:
        logger.warning("ocr_pdf_text.no_pages_rendered", total_pages=total_pages)
        return {}, problems, total_pages

    budget = (
        _ocr_total_timeout(len(page_images), page_timeout, concurrency)
        if total_timeout is None
        else total_timeout
    )
    logger.info(
        "ocr_pdf_text.starting",
        page_count=len(page_images),
        total_pages=total_pages,
        max_pages=max_pages,
        concurrency=concurrency,
        total_timeout=budget,
    )

    texts: dict[int, str | None] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="ocr")
    try:
        futures = {}
        deadline = start_time + budget
        if budget > 0:
            futures = {
                pool.submit(
                    _ocr_single_page,
                    image_bytes,
                    media_type,
                    page_index=index,
                    timeout=min(page_timeout, budget),
                ): index
                for index, (image_bytes, media_type) in page_images
            }
        pending = set(futures)
        while pending:
            remaining = deadline - time_mod.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                texts[futures[future]] = future.result()
        for future in pending:
            future.cancel()
    finally:
        # Abandoned calls finish in the background; their results are
        # dropped.
        pool.shutdown(wait=False, cancel_futures=True)

    unanswered = len(page_images) - len(texts)
    if unanswered:
        logger.warning(
            "ocr_pdf_text.total_timeout",
            pages_completed=len(texts),
            total_pages=len(page_images),
            elapsed_seconds=round(time_mod.monotonic() - start_time, 1),
        )
        problems.append(f"{unanswered} page(s) unanswered after {budget:g}s")
    failed = sorted(i + 1 for i, text in texts.items() if text is None)
    if failed:
        problems.append(f"page(s) {failed} failed")
    if problems:
        logger.warning(
            "ocr_pdf_text.incomplete",
            pages_read=len(texts) - len(failed),
            total_pages=total_pages,
            reason="; ".join(problems),
        )
    return texts, problems, total_pages


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
    return [image for _, image in _render_pdf_pages_counted(pdf_bytes, max_pages)[0]]


def _render_pdf_pages_counted(
    pdf_bytes: bytes,
    max_pages: int,
    indexes: list[int] | None = None,
) -> tuple[list[tuple[int, tuple[bytes, str]]], int]:
    """The 0-based pages *indexes* (every page when ``None``), at most
    *max_pages* of them, rendered as ``(index, (png_bytes, media_type))``,
    plus the PDF's total page count."""
    import pymupdf

    results: list[tuple[int, tuple[bytes, str]]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_pages = len(doc)
        zoom = _OCR_RESOLUTION / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        wanted = range(total_pages) if indexes is None else indexes

        for index in wanted:
            # page-cap: _ocr_pages_counted marks the result incomplete (#4986).
            if len(results) >= max_pages:
                break
            pix = doc[index].get_pixmap(matrix=matrix)
            png_bytes = pix.tobytes("png")
            results.append((index, (png_bytes, "image/png")))

    return results, total_pages


def _ocr_single_page(
    image_bytes: bytes,
    media_type: str,
    *,
    page_index: int,
    timeout: float,
) -> str | None:
    """OCR a single page image via the LLM vision API.

    Returns the extracted text (``""`` for a blank page), or ``None`` when
    the call failed.
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

        if getattr(response, "incomplete", False) is True:
            # A withheld (RECITATION / SAFETY) answer comes back empty and a
            # cut-off one is partial: neither is the page's text, and an
            # empty one would pass for a blank page (#5007).
            logger.warning(
                "ocr_pdf_text.page_incomplete",
                page_index=page_index,
                **response_diagnostics(response),
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
        return text
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


def _extract_case_number_prefix(raw: str) -> str | None:
    """Extract the county prefix from an OC-style case number.

    Returns the numeric prefix (e.g. ``"30"``) when the raw case number
    starts with 2-3 digits followed by a ``-`` (e.g. ``"30-2025-01498771"``).
    Returns ``None`` for LA-style case numbers like ``"22SMCV01940"`` or for
    any other format that doesn't match.
    """
    raw = raw.strip()
    m = _COUNTY_PREFIX_RE.match(raw)
    if m:
        # Extract the prefix portion (everything before the first "-")
        prefix = raw.split("-", 1)[0]
        if prefix.isdigit() and 2 <= len(prefix) <= 3:
            return prefix
    return None


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
# page-cap: field fill, not a split; llm_extract.chunk_truncated warns.
_MAX_CHUNKS = 10

# Overlap characters between consecutive chunks for context continuity.
_CHUNK_OVERLAP = 500

# Number of leading chars of the failing chunk to include in the
# ``chunk_api_failure`` structured log event.  Bounded so the log line
# stays cheap to ingest.  See #4253 — this enrichment was previously
# gated behind a 3-failure threshold (``deterministic_chunk_failure``);
# the threshold was structurally too strict for typical 2-chunk docs and
# was removed.  Every chunk failure now gets the rich payload directly.
_CHUNK_FAILURE_PREVIEW_CHARS = 200


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
            # page-cap: field fill, not a split; llm_extract.chunk_truncated warns.
            if len(chunks) >= _MAX_CHUNKS:
                break
        last_boundary = boundary

    # Append the remaining text.
    # page-cap: field fill, not a split; llm_extract.chunk_truncated warns.
    if len(chunks) < _MAX_CHUNKS and chunk_start < len(text):
        remaining = text[chunk_start:]
        if len(remaining) > max_chars:
            # Still too large — split the remainder at boundaries or
            # force-split as a fallback.
            sub_chunks = _force_split(remaining, max_chars)
            for sc in sub_chunks:
                # page-cap: field fill, not a split; llm_extract.chunk_truncated warns.
                if len(chunks) >= _MAX_CHUNKS:
                    break
                chunks.append(sc)
        else:
            chunks.append(remaining)

    if not chunks:
        chunks = [text[:max_chars]]

    # page-cap: field fill, not a split; llm_extract.chunk_truncated warns.
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
    # page-cap: field fill, not a split; llm_extract.chunk_truncated warns.
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
    bust_cache: bool = False,
    document_id: str | None = None,
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
        bust_cache: When ``True``, skip the LLM cache read.  Cache writes
            still happen so subsequent calls without the flag benefit
            from the fresh result.  Used by the ``--bust-llm-cache``
            reingest flag (#2424) to force fresh LLM output after a
            prompt change, without invalidating the cache permanently.
        document_id: Optional source ``derived.documents`` UUID.  Echoed
            into the ``llm_extract.chunk_api_failure`` structured log
            event when a chunk's LLM call returns ``None``, so future log
            queries can correlate failures back to the source document
            and group by ``chunk_content_sha256`` (#4233 / #4253).  Has
            no effect on cache key.

    Returns:
        An ``LLMExtractionResult`` with extracted fields, or ``None`` if
        the API call fails (caller should fall back to regex extraction).
    """
    if not document_text or not document_text.strip():
        return None

    # LLM result cache — check before doing any work.  When bust_cache
    # is set, the cache read is skipped (but writes still happen so
    # subsequent runs benefit from the fresh extraction).
    cache = _get_llm_cache(provider, model)
    cache_key = _compute_cache_key(document_text, metadata)
    if cache is not None and not bust_cache:
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
            # Self-diagnosing chunk-failure payload (#4253).  Each
            # ``call_llm`` already represents 3 exhausted internal Google
            # retries, so a single failure here is enough to deserve the
            # full diagnostic blob (SHA-256 + preview) rather than waiting
            # for an N-failure threshold.  The 97KB / 2-chunk #4233 docs
            # never accumulated 3 failures in a single run, which made the
            # prior threshold-based event silent on the very docs it was
            # added to diagnose.
            chunk_preview = chunk[:_CHUNK_FAILURE_PREVIEW_CHARS]
            chunk_content_sha256 = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            logger.warning(
                "llm_extract.chunk_api_failure",
                chunk_index=i,
                document_id=document_id,
                provider=provider,
                model=model,
                chunk_content_sha256=chunk_content_sha256,
                chunk_preview=chunk_preview,
                chunk_len=len(chunk),
                chunk_kind="text",
            )
            return None
        if token_tracker is not None:
            token_tracker.add(llm_response.input_tokens, llm_response.output_tokens)
        # A cut-off or withheld answer is a failed chunk even when its text
        # happens to parse, so it is never cached (#5007).
        parsed = None
        if getattr(llm_response, "incomplete", False) is not True:
            parsed = _parse_response(llm_response.text, metadata)
        if parsed is None:
            logger.warning(
                "llm_extract.chunk_response_failed",
                chunk_index=i,
                document_id=document_id,
                chunk_len=len(chunk),
                **response_diagnostics(llm_response),
            )
        return parsed

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
    # A failed chunk's fields are missing.  This is a field fill, not a split,
    # so the other chunks' fields are still returned, but never cached: the
    # cache would serve the gap on every later call (#5000).
    partial = len(chunk_results) < len(chunks)
    if partial:
        logger.warning(
            "llm_extract.partial_chunk_failure",
            document_id=document_id,
            failed_chunks=len(chunks) - len(chunk_results),
            total_chunks=len(chunks),
        )

    # Single chunk: return directly.  Multiple: merge.
    result = chunk_results[0] if len(chunk_results) == 1 else _merge_results(chunk_results)

    # Write to cache
    if cache is not None and result is not None and not partial:
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
    """Parse the JSON response from the model into an ``LLMExtractionResult``.

    ``None`` (a failed chunk, never cached) when the response is not JSON or
    has no valid ``rulings`` array (#5011, ``rulings_array_shape_error``);
    an explicit ``"rulings": []`` is a valid answer.
    """
    try:
        parsed = parse_llm_json(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("llm_extract.json_parse_error", error=str(exc), raw=raw_text[:200])
        return None

    shape_error = rulings_array_shape_error(parsed)
    if shape_error is not None:
        logger.warning("llm_extract.chunk_shape_error", shape=shape_error, raw=raw_text[:200])
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

    # Parse rulings array: a list of objects (``rulings_array_shape_error``).
    rulings: list[LLMRulingResult] = []
    for r in parsed["rulings"]:
        # Normalize case number — support both old and new field names
        case_number = r.get("extracted_case_number") or r.get("case_number")
        case_number_prefix: str | None = None
        if case_number:
            raw_case_number = str(case_number)
            case_number_prefix = _extract_case_number_prefix(raw_case_number)
            case_number = _normalize_case_number(raw_case_number)

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
                case_number_prefix=case_number_prefix,
            )
        )

    return LLMExtractionResult(
        judge_name=judge_name,
        hearing_date=hearing_date,
        department=str(department) if department else None,
        case_count=len(rulings),
        rulings=rulings,
    )
