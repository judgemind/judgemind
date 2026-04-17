"""Ingestion validation gate — LLM-based quality checks before DB write.

Validates extracted fields against ruling text using a cheap LLM model
(Haiku-class) to catch data quality issues that regression tests and
volume-based monitoring miss. Runs inline in the ingestion worker between
enrichment and DB write.

Gated behind ``ENABLE_INGESTION_VALIDATION`` environment variable.
Validation failures never block ingestion — on LLM errors, the document
is written normally with a logged ``error`` result.

See: architecture spec §3.5.3, issue #1801 (parent epic), #1803.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from judgemind_config import DEFAULT_HAIKU_MODEL

from framework.llm_utils import parse_llm_json
from ingestion.llm_providers import LLMResponse, call_llm

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default model — always Haiku for cost efficiency.
_VALIDATION_MODEL = DEFAULT_HAIKU_MODEL

# Max output tokens — validation responses are short JSON.
_DEFAULT_MAX_TOKENS = 512

# Minimum ruling text length worth validating. Very short texts
# produce low-quality validation and waste LLM tokens.
_MIN_TEXT_LENGTH = 30

# Validation prompt
_SYSTEM_PROMPT = """\
You are a data quality validator for a legal research platform that ingests \
court tentative rulings. Your job is to check whether the extracted metadata \
fields are consistent with the ruling text.

You will receive:
1. Extracted fields (case_number, case_title, judge_name, motion_type, outcome, \
hearing_date, department, county)
2. A ruling text excerpt

Check for these issues:
- **Case number mismatch**: The ruling text mentions a different case number \
than the extracted one.
- **Case title mismatch**: The case title does not match any parties mentioned \
in the ruling text.
- **Judge name implausible**: The judge_name field contains something that is \
clearly not a person's name (e.g., a court division header, a case number, \
boilerplate text).
- **Outcome contradiction**: The extracted outcome (granted/denied) contradicts \
what the ruling text says.
- **Wrong content assigned**: The ruling text appears to be about a completely \
different case than the metadata suggests (e.g., different parties, different \
motion type).
- **Garbled or truncated text**: The ruling text is clearly corrupted, garbled, \
or truncated mid-sentence in a way that suggests a scraping error.

Respond with a JSON object:
{
  "result": "pass" | "flag" | "fail",
  "reason": "brief explanation (null for pass)"
}

Rules:
- **pass**: Fields are consistent with the ruling text. No issues detected.
- **flag**: Minor inconsistency detected that warrants human review but the \
data is probably usable (e.g., slight case title variation, ambiguous outcome).
- **fail**: Clear data quality issue detected — the ruling text is obviously \
about a different case, or fields are clearly wrong.
- When in doubt, prefer "pass" over "flag". Only flag when there is a \
specific, articulable concern.
- Never flag missing fields — those are expected when extraction fails. Only \
flag inconsistencies between fields that ARE present and the ruling text.
"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of a single validation check."""

    result: str  # "pass", "flag", "fail", or "error"
    reason: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_document(
    *,
    ruling_text: str | None,
    case_number: str | None,
    case_title: str | None,
    judge_name: str | None,
    motion_type: str | None,
    outcome: str | None,
    hearing_date: str | None,
    department: str | None,
    county: str | None,
    client: object | None = None,
    provider: str | None = None,
    model: str | None = None,
    timeout: float | None = 30.0,
) -> ValidationResult:
    """Validate extracted fields against ruling text using an LLM.

    Parameters
    ----------
    ruling_text : str | None
        The ruling text to validate against. If None or too short,
        returns a pass result (nothing to validate).
    case_number, case_title, judge_name, motion_type, outcome,
    hearing_date, department, county : str | None
        Extracted metadata fields to validate.
    client : object | None
        Pre-created LLM client for connection reuse.
    provider : str | None
        LLM provider. Defaults to "anthropic" for validation.
    model : str | None
        Model ID. Defaults to Haiku.
    timeout : float | None
        Per-call timeout in seconds.

    Returns
    -------
    ValidationResult
        The validation outcome with reason, model, token counts, and latency.
    """
    if not ruling_text or len(ruling_text) < _MIN_TEXT_LENGTH:
        return ValidationResult(
            result="pass",
            reason=None,
            model=None,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
        )

    resolved_model = model or _VALIDATION_MODEL
    resolved_provider = provider or "anthropic"

    # Build the user message with extracted fields and ruling text excerpt.
    # Use a 500-char excerpt to keep costs low while providing enough context.
    text_excerpt = ruling_text[:500]
    if len(ruling_text) > 500:
        text_excerpt += " [...]"

    fields: dict[str, str | None] = {
        "case_number": case_number,
        "case_title": case_title,
        "judge_name": judge_name,
        "motion_type": motion_type,
        "outcome": outcome,
        "hearing_date": hearing_date,
        "department": department,
        "county": county,
    }
    # Only include non-None fields in the prompt to reduce token count.
    present_fields = {k: v for k, v in fields.items() if v is not None}

    user_message = (
        f"Extracted fields:\n{json.dumps(present_fields, indent=2)}\n\n"
        f"Ruling text excerpt:\n{text_excerpt}"
    )

    t0 = time.monotonic()
    response: LLMResponse | None = None
    try:
        response = call_llm(
            _SYSTEM_PROMPT,
            user_message,
            provider=resolved_provider,
            model=resolved_model,
            client=client,
            timeout=timeout,
            max_tokens=_DEFAULT_MAX_TOKENS,
        )
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        logger.warning(
            "Validation LLM call raised exception — treating as pass",
            extra={"error": str(exc), "latency_ms": latency_ms},
        )
        return ValidationResult(
            result="error",
            reason=f"LLM call exception: {exc}",
            model=resolved_model,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
        )

    latency_ms = round((time.monotonic() - t0) * 1000)

    if response is None:
        logger.warning(
            "Validation LLM call returned None — treating as pass",
            extra={"latency_ms": latency_ms},
        )
        return ValidationResult(
            result="error",
            reason="LLM call returned None",
            model=resolved_model,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
        )

    # Parse the LLM response
    return _parse_validation_response(
        response,
        model=resolved_model,
        latency_ms=latency_ms,
    )


def insert_validation_result(
    conn: psycopg.Connection,
    *,
    document_id: str,
    ruling_id: str | None,
    result: ValidationResult,
) -> None:
    """Insert a validation result into the validation_results table.

    Parameters
    ----------
    conn : psycopg.Connection
        Active database connection (caller manages transaction).
    document_id : str
        The document being validated.
    ruling_id : str | None
        The ruling ID, if available.
    result : ValidationResult
        The validation outcome to log.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO validation_results
                (document_id, ruling_id, result, reason, model,
                 input_tokens, output_tokens, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                document_id,
                ruling_id,
                result.result,
                result.reason,
                result.model,
                result.input_tokens,
                result.output_tokens,
                result.latency_ms,
            ),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_validation_response(
    response: LLMResponse,
    *,
    model: str,
    latency_ms: int,
) -> ValidationResult:
    """Parse the LLM JSON response into a ValidationResult.

    On parse failure, returns an ``error`` result so ingestion is not blocked.
    """
    try:
        data: dict[str, Any] = parse_llm_json(response.text)
        result_value = data.get("result", "pass")
        reason = data.get("reason")

        # Validate the result value
        if result_value not in ("pass", "flag", "fail"):
            logger.warning(
                "Validation LLM returned unknown result — treating as pass",
                extra={"raw_result": result_value},
            )
            result_value = "pass"
            reason = None

        return ValidationResult(
            result=result_value,
            reason=reason,
            model=model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
        )
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning(
            "Failed to parse validation LLM response — treating as pass",
            extra={
                "error": str(exc),
                "raw_response": response.text[:200],
            },
        )
        return ValidationResult(
            result="error",
            reason=f"Response parse error: {exc}",
            model=model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
        )
