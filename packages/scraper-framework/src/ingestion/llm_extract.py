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

import html
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import structlog

from .llm_providers import call_llm

logger = structlog.get_logger(__name__)

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
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
        if pages_text:
            return "\n\f\n".join(pages_text)
        return None
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# System prompt (v2 — from evaluation)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (  # noqa: E501 — prompt text sent to LLM, line length irrelevant
    "You are a legal document parser for California court "
    "tentative rulings.\n\n"
    "Given a court ruling document, extract ALL structured "
    "fields into JSON.\n\n"
    "## Rules\n\n"
    "1. **Multi-ruling documents:** A single document may "
    "contain rulings for multiple cases. Return an array "
    "of ALL cases found.\n\n"
    "2. **Outcome taxonomy** — use EXACTLY one of these "
    "values:\n"
    "   - granted\n"
    "   - denied\n"
    "   - granted_in_part\n"
    "   - denied_in_part\n"
    "   - moot\n"
    "   - continued\n"
    "   - off_calendar\n"
    "   - submitted\n"
    "   - other (only if none of the above fit)\n\n"
    "3. **Case number normalization:** Strip any county "
    "prefix digits before the year. For example, "
    '"30-2024-01393434" becomes "2024-01393434". '
    "Keep the full number for formats like "
    '"24NNCV02551".\n\n'
    "4. **Parties:** Extract plaintiff(s) and defendant(s) "
    "from case captions. Each party is "
    '{"name": "...", "role": "plaintiff"} or '
    '{"name": "...", "role": "defendant"}.\n\n'
    "5. **Case type:** Classify the case using EXACTLY one "
    "of these values:\n"
    "   - civil (general civil litigation, torts, contracts, "
    "employment, PI — includes 'unlimited civil' and "
    "'limited civil')\n"
    "   - criminal\n"
    "   - family (divorce, custody, domestic violence)\n"
    "   - probate (wills, estates, conservatorships, "
    "guardianships)\n"
    "   - small_claims\n"
    "   - juvenile\n"
    "   - traffic\n"
    "   - other (only if none of the above fit)\n\n"
    "   Inference hints: case numbers starting with SC or "
    "containing 'SC' often indicate small claims; "
    "'CV', 'CIV', 'STCV' indicate civil; "
    "'CR', 'F' indicate criminal; "
    "'FL', 'DV' indicate family; "
    "'PR', 'BP' indicate probate. "
    "Motion types like 'demurrer', 'msj', 'anti_slapp' "
    "are civil. "
    "If the case type cannot be determined, use null.\n\n"
    "6. **Motion type:** Use a short descriptive label. "
    "Common values: "
    '"msj" (summary judgment), '
    '"msj_partial" (summary adjudication), '
    '"mtd" (motion to dismiss), '
    '"mil" (motion in limine), '
    '"demurrer", "motion_to_compel", '
    '"motion_to_strike", "anti_slapp", '
    '"preliminary_injunction", '
    '"ex_parte_application", "petition", '
    '"default_judgment", '
    '"motion_for_attorney_fees", '
    '"motion_to_be_relieved_as_counsel", '
    '"motion_for_leave_to_amend", '
    '"motion_for_sanctions", '
    '"osc" (order to show cause), "other".\n\n'
    "7. **Hearing date:** Return as ISO format "
    '"YYYY-MM-DD".\n\n'
    "8. **Judge name:** Extract the judge's full name. "
    'Do not include titles like "Hon." or "Judge".\n\n'
    "9. If metadata is provided (judge_name, department), "
    "treat it as authoritative — use it directly rather "
    "than extracting from the document.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "3" or "H" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "case_number": "24NNCV02551" or null,\n'
    '      "case_title": "Smith v. Jones" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "granted" or null,\n'
    '      "motion_type": "msj" or null,\n'
    '      "parties": [\n'
    '        {"name": "John Smith", "role": "plaintiff"},\n'
    '        {"name": "Jane Jones", "role": "defendant"}\n'
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}"
)


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

    Returns:
        An ``LLMExtractionResult`` with extracted fields, or ``None`` if
        the API call fails (caller should fall back to regex extraction).
    """
    if not document_text or not document_text.strip():
        return None

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
        )
        if llm_response is None:
            logger.warning("llm_extract.chunk_api_failure", chunk_index=i)
            return None
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
    if len(chunk_results) == 1:
        return chunk_results[0]
    return _merge_results(chunk_results)


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

    # Extract top-level fields
    judge_name = parsed.get("judge_name")
    hearing_date = _parse_date(parsed.get("hearing_date"))
    department = parsed.get("department")

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

        # Normalize case number
        case_number = r.get("case_number")
        if case_number:
            case_number = _normalize_case_number(str(case_number))

        # Validate case_type against enum
        case_type = r.get("case_type")
        if case_type and case_type not in CASE_TYPE_VALUES:
            case_type = "other"

        # Validate outcome against enum
        outcome = r.get("outcome")
        if outcome and outcome not in OUTCOME_VALUES:
            outcome = "other"

        # Parse parties — validate that names are plausible (not
        # garbage text blocks).  Real party names are well under 200
        # chars and never contain newlines.
        raw_parties = r.get("parties", [])
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
                    parties.append({"name": name, "role": str(p["role"])})

        rulings.append(
            LLMRulingResult(
                case_number=case_number,
                case_title=r.get("case_title"),
                case_type=case_type,
                outcome=outcome,
                motion_type=r.get("motion_type"),
                parties=parties,
            )
        )

    return LLMExtractionResult(
        judge_name=judge_name,
        hearing_date=hearing_date,
        department=str(department) if department else None,
        case_count=len(rulings),
        rulings=rulings,
    )
