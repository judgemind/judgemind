"""Universal LLM enrichment extractor for court ruling text.

Takes ``ruling_text`` (plain text, already transcribed) and returns structured
fields: ``motion_type``, ``outcome``, ``case_title``, and ``parties``.

This module replaces per-county regex extraction patterns by using an LLM to
extract structured data from the ruling text in a single pass. It is designed
for the **enrichment** stage of the ingestion pipeline (see Architecture Spec
Section 5.2) and does NOT handle document capture or transcription.

Design principles:

- **Stateless**: no DB access, no side effects. Pure function: text in,
  structured data out.
- **Taxonomy-constrained**: motion_type and outcome values are validated
  against fixed taxonomies. Invalid values are mapped to ``"other"``.
- **Resilient**: returns a result with ``None`` fields on LLM failure rather
  than raising. Retries once on JSON parse failure.
- **Observable**: logs token usage per call for cost monitoring.

See: https://github.com/judgemind/judgemind/issues/2175
"""

from __future__ import annotations

import json
import re
from enum import StrEnum

import structlog
from pydantic import BaseModel, Field, field_validator

from ingestion.llm_providers import LLMResponse, call_llm

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------


class MotionType(StrEnum):
    """Canonical motion type taxonomy for enrichment extraction."""

    MSJ = "msj"
    MTD = "mtd"
    DEMURRER = "demurrer"
    MIL = "mil"
    MOTION_TO_COMPEL = "motion_to_compel"
    MOTION_TO_STRIKE = "motion_to_strike"
    MOTION_FOR_ATTORNEY_FEES = "motion_for_attorney_fees"
    MOTION_TO_QUASH = "motion_to_quash"
    MOTION_FOR_SUMMARY_ADJUDICATION = "motion_for_summary_adjudication"
    MOTION_TO_COMPEL_ARBITRATION = "motion_to_compel_arbitration"
    MOTION_FOR_NEW_TRIAL = "motion_for_new_trial"
    MOTION_TO_TAX_COSTS = "motion_to_tax_costs"
    MOTION_FOR_RECONSIDERATION = "motion_for_reconsideration"
    MOTION_TO_BE_RELIEVED_AS_COUNSEL = "motion_to_be_relieved_as_counsel"
    MOTION_PRO_HAC_VICE = "motion_pro_hac_vice"
    PETITION = "petition"
    OTHER = "other"


VALID_MOTION_TYPES: frozenset[str] = frozenset(m.value for m in MotionType)


class OutcomeType(StrEnum):
    """Canonical outcome taxonomy for enrichment extraction."""

    GRANTED = "granted"
    DENIED = "denied"
    GRANTED_IN_PART = "granted_in_part"
    DENIED_IN_PART = "denied_in_part"
    MOOT = "moot"
    CONTINUED = "continued"
    OFF_CALENDAR = "off_calendar"
    SUBMITTED = "submitted"
    OTHER = "other"


VALID_OUTCOMES: frozenset[str] = frozenset(o.value for o in OutcomeType)

# ---------------------------------------------------------------------------
# Pydantic output models
# ---------------------------------------------------------------------------


class EnrichmentParties(BaseModel):
    """Extracted plaintiff and defendant party names."""

    plaintiffs: list[str] = Field(default_factory=list)
    defendants: list[str] = Field(default_factory=list)


class LlmEnrichmentResult(BaseModel):
    """Result of LLM-based enrichment extraction from ruling text.

    All fields are optional — ``None`` indicates the field could not be
    extracted (either absent from the text or LLM failure).
    """

    case_title: str | None = None
    motion_type: str | None = None
    outcome: str | None = None
    parties: EnrichmentParties = Field(default_factory=EnrichmentParties)

    @field_validator("motion_type")
    @classmethod
    def validate_motion_type(cls, v: str | None) -> str | None:
        """Constrain motion_type to the taxonomy, mapping unknowns to 'other'."""
        if v is None:
            return None
        v_lower = v.strip().lower()
        if v_lower in VALID_MOTION_TYPES:
            return v_lower
        logger.debug(
            "llm_enrichment.unknown_motion_type",
            raw_value=v,
            mapped_to="other",
        )
        return "other"

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, v: str | None) -> str | None:
        """Constrain outcome to the taxonomy, mapping unknowns to 'other'."""
        if v is None:
            return None
        v_lower = v.strip().lower()
        if v_lower in VALID_OUTCOMES:
            return v_lower
        logger.debug(
            "llm_enrichment.unknown_outcome",
            raw_value=v,
            mapped_to="other",
        )
        return "other"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

ENRICHMENT_SYSTEM_PROMPT = (
    "You are a legal document parser for California court tentative rulings.\n\n"
    "Given the text of a single court ruling, extract the following structured "
    "fields into JSON:\n\n"
    '1. **case_title** - The case caption (e.g., "Smith v. Jones"). '
    'Extract the full "Plaintiff v. Defendant" format as it appears. '
    "If no clear case title is present, return null.\n\n"
    "2. **motion_type** - Classify the motion using EXACTLY one of these "
    "values:\n"
    "   - msj (motion for summary judgment)\n"
    "   - mtd (motion to dismiss)\n"
    "   - demurrer\n"
    "   - mil (motion in limine)\n"
    "   - motion_to_compel\n"
    "   - motion_to_strike\n"
    "   - motion_for_attorney_fees\n"
    "   - motion_to_quash\n"
    "   - motion_for_summary_adjudication\n"
    "   - motion_to_compel_arbitration\n"
    "   - motion_for_new_trial\n"
    "   - motion_to_tax_costs\n"
    "   - motion_for_reconsideration\n"
    "   - motion_to_be_relieved_as_counsel\n"
    "   - motion_pro_hac_vice\n"
    "   - petition\n"
    "   - other (only if none of the above fit)\n"
    "   If the ruling mentions multiple motions, use the PRIMARY motion "
    "being ruled on. If no motion type can be determined, return null.\n\n"
    "3. **outcome** - The ruling's outcome, using EXACTLY one of:\n"
    '   - granted (fully granted, including "granted with conditions")\n'
    '   - denied (fully denied, including "denied without prejudice")\n'
    "   - granted_in_part (partially granted and partially denied)\n"
    "   - denied_in_part (partially denied)\n"
    "   - moot (no longer relevant)\n"
    "   - continued (hearing postponed)\n"
    '   - off_calendar (hearing removed from calendar; look for "taken '
    'off calendar", "OCAL", "vacated")\n'
    "   - submitted (taken under submission for later decision)\n"
    "   - other (only if none of the above fit)\n"
    '   Note: "sustained" on a demurrer = granted. "overruled" on a '
    "demurrer = denied.\n\n"
    "4. **parties** - Extract plaintiff(s) and defendant(s) from the case "
    "caption. Return as:\n"
    '   {"plaintiffs": ["Name1", ...], "defendants": ["Name2", ...]}\n'
    "   Use the names exactly as they appear. Omit legal descriptors like "
    '"an individual" or "a corporation". If roles are "petitioner" '
    'and "respondent", map petitioner to plaintiffs and respondent to '
    "defendants. If parties cannot be determined, return empty lists.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "case_title": "Smith v. Jones" or null,\n'
    '  "motion_type": "msj" or null,\n'
    '  "outcome": "granted" or null,\n'
    '  "parties": {\n'
    '    "plaintiffs": ["John Smith"],\n'
    '    "defendants": ["Jane Jones"]\n'
    "  }\n"
    "}\n\n"
    "## Few-shot examples\n\n"
    "### Example 1: Motion for Summary Judgment (granted)\n\n"
    "Input:\n"
    "Case Number: 22SMCV01940\n"
    "Hernandez v. Pacific Coast Properties LLC\n"
    "Motion for Summary Judgment\n"
    "The motion for summary judgment filed by defendant Pacific Coast "
    "Properties LLC is GRANTED. Plaintiff Hernandez has failed to raise a "
    "triable issue of material fact regarding the negligence claim.\n\n"
    "Output:\n"
    "{\n"
    '  "case_title": "Hernandez v. Pacific Coast Properties LLC",\n'
    '  "motion_type": "msj",\n'
    '  "outcome": "granted",\n'
    '  "parties": {\n'
    '    "plaintiffs": ["Hernandez"],\n'
    '    "defendants": ["Pacific Coast Properties LLC"]\n'
    "  }\n"
    "}\n\n"
    "### Example 2: Demurrer (sustained = granted)\n\n"
    "Input:\n"
    "CVPS2400892\n"
    "Thompson v. City of Palm Springs\n"
    "Demurrer to Second Amended Complaint\n"
    "The demurrer is OVERRULED. The complaint adequately states a cause of "
    "action for wrongful termination.\n\n"
    "Output:\n"
    "{\n"
    '  "case_title": "Thompson v. City of Palm Springs",\n'
    '  "motion_type": "demurrer",\n'
    '  "outcome": "denied",\n'
    '  "parties": {\n'
    '    "plaintiffs": ["Thompson"],\n'
    '    "defendants": ["City of Palm Springs"]\n'
    "  }\n"
    "}\n\n"
    "### Example 3: Motion to Compel (granted in part)\n\n"
    "Input:\n"
    "Garcia v. State Farm Insurance\n"
    "Motion to Compel Further Discovery Responses\n"
    "The motion is GRANTED IN PART. Defendant shall provide further "
    "responses to interrogatories 5, 7, and 12 within 20 days. "
    "The motion is denied as to interrogatories 3 and 9.\n\n"
    "Output:\n"
    "{\n"
    '  "case_title": "Garcia v. State Farm Insurance",\n'
    '  "motion_type": "motion_to_compel",\n'
    '  "outcome": "granted_in_part",\n'
    '  "parties": {\n'
    '    "plaintiffs": ["Garcia"],\n'
    '    "defendants": ["State Farm Insurance"]\n'
    "  }\n"
    "}\n\n"
    "### Example 4: Motion to be Relieved as Counsel\n\n"
    "Input:\n"
    "Johnson v. Metro Health Systems, Inc., et al.\n"
    "Motion to Be Relieved as Counsel\n"
    "The motion of attorney Michael Chen to be relieved as counsel for "
    "plaintiff is GRANTED. The order is effective upon filing proof of "
    "service of the signed order on the client.\n\n"
    "Output:\n"
    "{\n"
    '  "case_title": "Johnson v. Metro Health Systems, Inc., et al.",\n'
    '  "motion_type": "motion_to_be_relieved_as_counsel",\n'
    '  "outcome": "granted",\n'
    '  "parties": {\n'
    '    "plaintiffs": ["Johnson"],\n'
    '    "defendants": ["Metro Health Systems, Inc."]\n'
    "  }\n"
    "}\n\n"
    "### Example 5: Off calendar\n\n"
    "Input:\n"
    "Lee v. Sunrise Medical Group\n"
    "Motion for Summary Judgment\n"
    "The motion is taken OFF CALENDAR per stipulation of the parties.\n\n"
    "Output:\n"
    "{\n"
    '  "case_title": "Lee v. Sunrise Medical Group",\n'
    '  "motion_type": "msj",\n'
    '  "outcome": "off_calendar",\n'
    '  "parties": {\n'
    '    "plaintiffs": ["Lee"],\n'
    '    "defendants": ["Sunrise Medical Group"]\n'
    "  }\n"
    "}"
)

# Prompt sent when the first LLM response contains invalid JSON.
_JSON_FIX_PROMPT = (
    "Your previous response was not valid JSON. Please respond with ONLY "
    "a valid JSON object matching this schema:\n"
    "{\n"
    '  "case_title": "..." or null,\n'
    '  "motion_type": "..." or null,\n'
    '  "outcome": "..." or null,\n'
    '  "parties": {"plaintiffs": [...], "defendants": [...]}\n'
    "}\n\n"
    "Here is the ruling text again:\n\n"
)


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------


def enrich_ruling(
    ruling_text: str,
    *,
    provider: str = "google",
    model: str | None = None,
    client: object | None = None,
) -> LlmEnrichmentResult | None:
    """Extract structured fields from ruling text via an LLM.

    Sends the ruling text to the configured LLM provider and parses the
    JSON response into an ``LlmEnrichmentResult``.  On JSON parse failure,
    retries once with a corrective prompt.  On any unrecoverable failure,
    returns a result with all fields set to ``None``.

    Parameters
    ----------
    ruling_text : str
        The plain text of a single court ruling (already transcribed).
    provider : str
        LLM provider — ``"google"`` or ``"anthropic"``.
    model : str | None
        Model ID override. If ``None``, uses the provider default.
    client : object | None
        Optional pre-created provider client for connection reuse.

    Returns
    -------
    LlmEnrichmentResult | None
        Extracted fields, or ``None`` if the LLM API call failed
        (timeout, rate limit, auth error, etc.).  A result with all
        ``None`` fields indicates the LLM responded successfully but
        could not extract data from the text (e.g. text too short,
        JSON parse failure after retry).
    """
    if not ruling_text or not ruling_text.strip():
        logger.warning("llm_enrichment.empty_ruling_text")
        return LlmEnrichmentResult()

    # First attempt
    response = call_llm(
        ENRICHMENT_SYSTEM_PROMPT,
        ruling_text,
        provider=provider,
        model=model,
        client=client,
        max_tokens=1024,
    )

    if response is None:
        logger.warning("llm_enrichment.llm_call_failed")
        return None

    _log_token_usage(response)

    result = _parse_response(response.text)
    if result is not None:
        return result

    # Retry with corrective prompt
    logger.info("llm_enrichment.json_parse_retry")
    retry_message = _JSON_FIX_PROMPT + ruling_text
    retry_response = call_llm(
        ENRICHMENT_SYSTEM_PROMPT,
        retry_message,
        provider=provider,
        model=model,
        client=client,
        max_tokens=1024,
    )

    if retry_response is None:
        logger.warning("llm_enrichment.retry_llm_call_failed")
        return None

    _log_token_usage(retry_response)

    result = _parse_response(retry_response.text)
    if result is not None:
        return result

    logger.warning("llm_enrichment.json_parse_failed_after_retry")
    return LlmEnrichmentResult()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_response(text: str) -> LlmEnrichmentResult | None:
    """Parse an LLM response string into an ``LlmEnrichmentResult``.

    Handles markdown code fences (e.g. ````` ```json ... ``` `````) that
    newer Claude models sometimes wrap around JSON output, even when
    instructed to respond with JSON only.

    Returns ``None`` if the response is not valid JSON or does not match
    the expected schema.
    """
    try:
        cleaned = text.strip()
        # Strip markdown code fences if present (e.g. ```json ... ```)
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.debug("llm_enrichment.json_decode_error", text_preview=text[:200])
        return None

    if not isinstance(data, dict):
        logger.debug("llm_enrichment.unexpected_json_type", type=type(data).__name__)
        return None

    try:
        return LlmEnrichmentResult.model_validate(data)
    except Exception:  # noqa: BLE001
        logger.debug(
            "llm_enrichment.pydantic_validation_error",
            data_keys=list(data.keys()),
        )
        return None


def _log_token_usage(response: LLMResponse) -> None:
    """Log token usage for cost monitoring."""
    logger.info(
        "llm_enrichment.token_usage",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
