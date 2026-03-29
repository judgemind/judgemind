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

import json
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
# Per-page PDF extraction prompt and join patterns (#1590)
# ---------------------------------------------------------------------------

# System prompt for per-page extraction from OC-style 3-column PDF tables.
# Each page is sent individually.  The LLM returns a JSON array of table rows.
# This is the visual-structure prompt validated in PR #1692 (eval achieved 100%
# lenient accuracy across all OC fixtures).
PDF_PER_PAGE_PROMPT = (
    "You are a court ruling transcriber. You will receive a single page "
    "image from a California court tentative ruling PDF.\n\n"
    "The page contains a TABLE with THREE COLUMNS separated by TWO "
    "VERTICAL RULED LINES that run the full height of the page. ROWS "
    "are separated by HORIZONTAL RULED LINES.\n\n"
    "Use the VISUAL POSITION of text relative to the ruled lines to "
    "determine which column it belongs to:\n\n"
    "- Column 1 (entry_number): VERY NARROW column at the far left, "
    "LEFT of the first vertical line. Usually blank.\n"
    "- Column 2 (case_info): MEDIUM column BETWEEN the two vertical "
    "lines. Contains case name and case number.\n"
    "- Column 3 (ruling_text): the WIDEST column, taking up most "
    "of the page width, RIGHT of the second vertical line. Contains "
    "the full ruling text including all paragraphs, headings, and "
    "numbered sections.\n\n"
    "Most text on the page is in column 3. When in doubt about "
    "column membership, the text is in column 3.\n\n"
    "## Rules\n\n"
    "- Return ONE JSON object per ROW (between horizontal lines).\n"
    "- Read each column by its POSITION relative to the vertical "
    "lines.\n"
    "- Transcribe ruling_text as **Markdown** preserving formatting:\n"
    "  - Use **bold** for bold text and headings\n"
    "  - Use *italic* for italic/underlined text\n"
    "  - Use numbered lists (1. 2. 3.) for numbered paragraphs\n"
    "  - Use blank lines between paragraphs\n"
    "  - Preserve ALL content — do not summarize or omit\n"
    "- If a column is blank in a row, set its value to empty string.\n"
    "- SKIP page headers, footers, and page numbers.\n\n"
    "{\n"
    '  "rulings": [\n'
    '    {"entry_number": "101", "case_info": "Smith vs Jones\\n'
    '25-01455183",\n'
    '     "ruling_text": "**MOTION FOR SUMMARY JUDGMENT**\\n\\n'
    "Defendant's motion for summary judgment is **GRANTED**.\\n\\n"
    "1. The moving party has met its initial burden...\\n"
    '2. Plaintiff fails to raise a triable issue..."},\n'
    '    {"entry_number": "", "case_info": "",\n'
    '     "ruling_text": "continuation from previous page..."}\n'
    "  ]\n"
    "}"
)

# Pattern to detect case numbers in OC format.
_CASE_NUMBER_RE = re.compile(r"\d{2,4}-\d{5,8}|\b\d{7,8}\b")

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
    """
    import hashlib

    seen_hashes: dict[str, int] = {}  # hash -> index of first occurrence
    for i, ruling in enumerate(rulings):
        text = ruling.ruling_text
        if not text or len(text) < _DEDUP_MIN_LENGTH:
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
    ) -> None:
        self._provider = provider
        self._model = model or self._PROVIDER_DEFAULT_MODELS.get(provider, DEFAULT_HAIKU_MODEL)
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_output_tokens = max_output_tokens
        self._max_chars_per_chunk = max_chars_per_chunk

        # Create provider-specific client.
        if provider == "google":
            self._client = _create_google_client(api_key=api_key)
        else:
            # Default to Anthropic.
            client_kwargs: dict[str, str] = {}
            if api_key is not None:
                client_kwargs["api_key"] = api_key
            self._client = anthropic.Anthropic(**client_kwargs)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        system_prompt: str | None = None,
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

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the text is empty or the API call fails after retries.
        """
        if not text or not text.strip():
            return []

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
            return self._propagate_document_fields(merged)

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
        return self._propagate_document_fields(merged)

    def extract_from_pdf(
        self,
        pdf_bytes: bytes,
        *,
        metadata: dict[str, str] | None = None,
        max_pages: int = 20,
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

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the PDF is empty, rendering fails, or the API call fails
            after retries.
        """
        if not pdf_bytes:
            return []

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
        return _join_page_rows(all_rows, metadata=metadata)

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
            "Transcribe all rows of the ruling table on this page. "
            "One entry per row. Skip page headers and footers."
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
            cleaned = raw_text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```\w*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
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
    and ``ruling_text`` (str).  Invalid entries are filtered out.
    """
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON array in the response.
        start_idx = cleaned.find("[")
        end_idx = cleaned.rfind("]") + 1
        if start_idx >= 0 and end_idx > start_idx:
            try:
                parsed = json.loads(cleaned[start_idx:end_idx])
            except json.JSONDecodeError:
                logger.warning(
                    "llm_extractor.page_parse_error",
                    page_index=page_index,
                    raw_preview=raw_text[:200],
                )
                return []
        else:
            logger.warning(
                "llm_extractor.page_parse_error",
                page_index=page_index,
                raw_preview=raw_text[:200],
            )
            return []

    # Handle both list and dict responses.
    if isinstance(parsed, dict):
        # Some models wrap the array in a dict (keys: "rulings", "rows", "entries").
        rows_raw = parsed.get("rulings", parsed.get("rows", parsed.get("entries", [parsed])))
    elif isinstance(parsed, list):
        rows_raw = parsed
    else:
        return []

    rows: list[dict] = []
    for item in rows_raw:
        if not isinstance(item, dict):
            continue
        entry_number = item.get("entry_number")
        if entry_number is not None:
            # Normalize: strip trailing period, convert to int.
            try:
                entry_number = int(str(entry_number).rstrip(".").strip())
            except (ValueError, TypeError):
                entry_number = None
        rows.append(
            {
                "entry_number": entry_number,
                "case_info": str(item.get("case_info", "")).strip(),
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
    # 5. Collapse multiple whitespace to single space.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # 6. Remove leading/trailing punctuation and whitespace.
    cleaned = cleaned.strip(" -;,\t")
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

    # Extract judge/department from header rows before skipping them.
    # Header rows typically have entry_number=null, empty ruling_text,
    # and case_info containing "JUDGE {Name}" or "Department {Code}".
    header_judge: str | None = None
    header_dept: str | None = None
    for row in rows:
        if row["entry_number"] is None and not row["ruling_text"] and row.get("case_info"):
            info = row["case_info"]
            # Extract judge from "JUDGE WILLIAM D. CLASTER" pattern
            judge_match = re.search(r"JUDGE\s+(.+?)(?:\n|$)", info, re.IGNORECASE)
            if judge_match and not header_judge:
                header_judge = judge_match.group(1).strip()
            # Extract department from "Department CX101" or "Dept. C25" pattern
            dept_match = re.search(
                r"(?:Department|Dept\.?)\s+([A-Z0-9]+)", info, re.IGNORECASE
            )
            if dept_match and not header_dept:
                header_dept = dept_match.group(1).strip()

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

    # Post-processing: deduplicate identical ruling texts (#2096).
    # The LLM sometimes produces the same ruling text for multiple cases
    # in the same PDF.  Keep only the first occurrence; null out duplicates.
    rulings = _deduplicate_ruling_texts(rulings)

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
