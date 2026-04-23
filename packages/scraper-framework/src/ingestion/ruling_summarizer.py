"""Ruling summarization at ingestion time using Claude Haiku.

Generates plain-English summaries of tentative rulings during ingestion.
Summaries are pre-computed and cached in the ``rulings`` table — user-facing
pages serve cached summaries without live AI calls.

Gated behind ``ENABLE_RULING_SUMMARIZATION`` environment variable.
Summary generation failures never block ruling ingestion (graceful degradation).

Uses the same prompt and model as ``scripts/backfill_summaries.py`` and
``packages/nlp-pipeline/src/summarization/summarizer.py`` for consistency.
"""

from __future__ import annotations

import structlog
from judgemind_config import DEFAULT_HAIKU_MODEL

from .llm_providers import LLMResponse, call_llm

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default model — always Haiku for cost efficiency.
_SUMMARY_MODEL = DEFAULT_HAIKU_MODEL

# Max output tokens — summaries are 2-4 sentences, 512 is generous.
_DEFAULT_MAX_TOKENS = 512

# Minimum ruling text length worth summarizing.  Very short texts
# produce low-quality summaries and waste LLM tokens.
_MIN_TEXT_LENGTH = 50

# Summarization prompt — matches backfill_summaries.py and nlp-pipeline summarizer.py
_SYSTEM_PROMPT = (
    "You are summarizing a court tentative ruling for a legal research platform.\n"
    "Write 2-4 sentences summarizing: what motion was before the court, "
    "what the judge's tentative decision is, and the key reason(s). "
    "Use plain language that a non-lawyer can understand. "
    "Do not include case numbers or procedural boilerplate.\n\n"
    "If optional case context is provided (e.g. case title, motion type), "
    "use it to make the summary more specific, but do not repeat it verbatim."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize_ruling(
    ruling_text: str,
    *,
    case_context: dict[str, str] | None = None,
    client: object | None = None,
    timeout: float | None = 60.0,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> tuple[str | None, str]:
    """Generate a plain-English summary of a tentative ruling.

    Uses Claude Haiku via the provider-agnostic ``call_llm`` adapter.
    On any failure, returns ``(None, model)`` — the caller should
    continue without a summary rather than blocking ingestion.

    Args:
        ruling_text: The full text of the tentative ruling.
        case_context: Optional dict with case metadata (e.g. ``case_title``,
            ``motion_type``) to improve summary specificity.
        client: Optional pre-created Anthropic client for connection reuse.
        timeout: Per-call timeout in seconds.
        max_tokens: Maximum output tokens for the LLM call.

    Returns:
        Tuple of ``(summary_text_or_none, model_id)``.  The model ID is
        always returned so the caller can persist it even when the summary
        is ``None`` (for debugging/metrics).
    """
    model = _SUMMARY_MODEL

    if not ruling_text or not ruling_text.strip():
        logger.warning("ruling_summarizer.empty_text")
        return None, model

    if len(ruling_text.strip()) < _MIN_TEXT_LENGTH:
        logger.info(
            "ruling_summarizer.short_text_skipped",
            text_length=len(ruling_text),
        )
        return None, model

    # Build user message with optional case context
    user_content = "Summarize this tentative ruling:\n\n"
    if case_context:
        context_lines = [f"- {k}: {v}" for k, v in case_context.items()]
        user_content += "Case context:\n" + "\n".join(context_lines) + "\n\n"
    user_content += ruling_text

    try:
        llm_response: LLMResponse | None = call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_content,
            provider="anthropic",
            model=model,
            client=client,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except Exception:
        logger.exception("ruling_summarizer.llm_call_exception")
        return None, model

    if llm_response is None:
        logger.warning("ruling_summarizer.llm_call_failed")
        return None, model

    summary = llm_response.text.strip()

    logger.info(
        "ruling_summarizer.success",
        input_tokens=llm_response.input_tokens,
        output_tokens=llm_response.output_tokens,
        summary_length=len(summary),
    )

    return summary, model
