"""LLM-based field extraction using Claude Haiku.

Extracts structured fields (judge name, hearing date, case number, case title,
outcome, motion type, parties) from court ruling documents via the Anthropic API.
This is the core extraction logic called by the ingestion worker for fields that
scrapers did not populate.

On any API failure, returns ``None`` so the caller can fall back to regex-based
extraction (``extract.py``).
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date

import anthropic
import structlog

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
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LLMRulingResult:
    """Extraction result for a single ruling within a document."""

    case_number: str | None = None
    case_title: str | None = None
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
    "5. **Motion type:** Use a short descriptive label. "
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
    "6. **Hearing date:** Return as ISO format "
    '"YYYY-MM-DD".\n\n'
    "7. **Judge name:** Extract the judge's full name. "
    'Do not include titles like "Hon." or "Judge".\n\n'
    "8. If metadata is provided (judge_name, department), "
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
# Public API
# ---------------------------------------------------------------------------

# Maximum document text length to send to the model (chars).
# 100K chars ≈ ~25K tokens, well within Haiku's context window.
_MAX_DOCUMENT_LENGTH = 100_000


def extract_fields_llm(
    document_text: str,
    content_format: str,  # "html" or "pdf"
    metadata: dict[str, str] | None = None,
    client: anthropic.Anthropic | None = None,
) -> LLMExtractionResult | None:
    """Extract structured fields from a court ruling using Claude Haiku.

    Args:
        document_text: Raw document content (HTML or plain text from PDF).
        content_format: ``"html"`` or ``"pdf"``.
        metadata: Optional dict with authoritative scraper-provided context.
            May contain keys: ``link_text``, ``judge_name``, ``department``.
        client: Optional pre-created Anthropic client for connection reuse.
            If not provided, creates one from the ``ANTHROPIC_API_KEY`` env var.

    Returns:
        An ``LLMExtractionResult`` with extracted fields, or ``None`` if
        the API call fails (caller should fall back to regex extraction).
    """
    if not document_text or not document_text.strip():
        return None

    # Preprocess HTML to strip boilerplate
    if content_format == "html":
        text = preprocess_html(document_text)
    else:
        text = document_text

    # Truncate to max length
    if len(text) > _MAX_DOCUMENT_LENGTH:
        text = text[:_MAX_DOCUMENT_LENGTH]

    # Build the user message with optional metadata context
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
    user_message = "\n\n".join(user_parts)

    # Create client if not provided
    if client is None:
        try:
            client = anthropic.Anthropic()
        except anthropic.AuthenticationError:
            logger.warning("llm_extract.auth_error", error="Missing or invalid ANTHROPIC_API_KEY")
            return None

    # Call the API with retry on rate limit
    response = _call_api(client, user_message)
    if response is None:
        return None

    # Parse the response
    return _parse_response(response, metadata)


def _call_api(
    client: anthropic.Anthropic,
    user_message: str,
    *,
    max_retries: int = 1,
) -> str | None:
    """Call Claude Haiku and return the raw response text.

    Retries once on rate limit with 1s backoff. Returns ``None`` on any
    unrecoverable failure.
    """
    for attempt in range(1 + max_retries):
        try:
            response = client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=4096,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text.strip()
        except anthropic.RateLimitError:
            if attempt < max_retries:
                logger.warning("llm_extract.rate_limit", attempt=attempt + 1)
                time.sleep(1)
                continue
            logger.warning("llm_extract.rate_limit_exhausted")
            return None
        except anthropic.APIError as exc:
            logger.warning("llm_extract.api_error", error=str(exc))
            return None
    return None  # pragma: no cover — defensive unreachable


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

        # Validate outcome against enum
        outcome = r.get("outcome")
        if outcome and outcome not in OUTCOME_VALUES:
            outcome = "other"

        # Parse parties
        raw_parties = r.get("parties", [])
        parties: list[dict[str, str]] = []
        if isinstance(raw_parties, list):
            for p in raw_parties:
                if isinstance(p, dict) and p.get("name") and p.get("role"):
                    parties.append({"name": str(p["name"]), "role": str(p["role"])})

        rulings.append(
            LLMRulingResult(
                case_number=case_number,
                case_title=r.get("case_title"),
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
