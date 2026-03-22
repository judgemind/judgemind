"""LLM-based structured extraction of court rulings.

The ``LlmExtractor`` class is the framework-level entry point for
converting raw court calendar text into structured ``ExtractedRuling``
models via the Anthropic API.

Design principles:

- **Stateless**: no DB access, no side effects.  Pure function:
  text in, structured data out.
- **Configurable**: model and API key are configurable; defaults to
  Claude Haiku 4.5 for cost efficiency.
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

# Patterns for natural split boundaries.
_PAGE_BREAK_RE = re.compile(r"\f")
_CASE_BOUNDARY_RE = re.compile(
    r"\n(?=\s*(?:Case\s+(?:Number|No\.?)|CASE\s+(?:NUMBER|NO\.?))\s*[:\s])",
    re.IGNORECASE,
)

# OC-style county prefix: "30-2024-01393434" -> "2024-01393434"
_COUNTY_PREFIX_RE = re.compile(r"^\d{2,4}-(\d{4}-\d+)$")


# ---------------------------------------------------------------------------
# LlmExtractor
# ---------------------------------------------------------------------------


class LlmExtractor:
    """Stateless extractor that converts raw court calendar text to structured data.

    Args:
        model: Anthropic model ID.  Defaults to Claude Haiku 4.5.
        api_key: Anthropic API key.  If ``None``, the ``ANTHROPIC_API_KEY``
            environment variable is used (standard ``anthropic.Anthropic()``
            behavior).
        max_retries: Number of retries on transient API failures (429, 500, 529).
        base_delay: Initial backoff delay in seconds.
        max_delay: Maximum backoff delay in seconds.
        max_output_tokens: Maximum tokens in the model response.
        max_chars_per_chunk: Per-chunk character limit for large documents.

    Example::

        extractor = LlmExtractor()
        rulings = extractor.extract("Case No. 22SMCV01940 ...")
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_HAIKU_MODEL,
        api_key: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        max_delay: float = _DEFAULT_MAX_DELAY,
        max_output_tokens: int = 4096,
        max_chars_per_chunk: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._model = model
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_output_tokens = max_output_tokens
        self._max_chars_per_chunk = max_chars_per_chunk

        # Create client — raises if no API key is available.
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
    ) -> list[ExtractedRuling]:
        """Extract structured rulings from raw calendar page text.

        This is the main entry point.  It handles chunking for large
        documents, calls the Anthropic API with the extraction prompt,
        parses the JSON response into ``ExtractedRuling`` models, and
        deduplicates rulings across chunks.

        Args:
            text: Raw text content from a court calendar page (PDF or HTML,
                already converted to plain text).
            metadata: Optional dict with authoritative scraper-provided
                context.  Supported keys: ``judge_name``, ``department``,
                ``hearing_date``.

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the text is empty or the API call fails after retries.
        """
        if not text or not text.strip():
            return []

        chunks = self._split_into_chunks(text)
        usage = TokenUsage()

        if len(chunks) == 1:
            result = self._extract_chunk(chunks[0], metadata=metadata, usage=usage)
            self._log_usage(usage)
            return result.rulings if result else []

        # Multiple chunks: extract each and merge.
        logger.info(
            "llm_extractor.chunked",
            num_chunks=len(chunks),
            chunk_sizes=[len(c) for c in chunks],
        )
        all_results: list[ExtractionResult] = []
        for i, chunk in enumerate(chunks):
            result = self._extract_chunk(chunk, metadata=metadata, usage=usage, chunk_index=i)
            if result:
                all_results.append(result)

        self._log_usage(usage)

        if not all_results:
            return []

        merged = self._merge_results(all_results)
        return merged.rulings

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
    ) -> ExtractionResult | None:
        """Call the Anthropic API for a single text chunk and parse the result.

        Retries on transient errors (429, 500, 529) with exponential backoff.
        """
        user_message = self._build_user_message(text, metadata)
        delay = self._base_delay

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_output_tokens,
                    temperature=0,
                    system=EXTRACTION_SYSTEM_PROMPT,
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
