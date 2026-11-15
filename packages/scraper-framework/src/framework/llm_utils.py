"""Shared utilities for parsing LLM responses.

Consolidates common patterns (e.g. code-fence stripping) that were previously
duplicated across ``llm_extractor``, ``llm_extract``, ``llm_enrichment``,
``ruling_formatter``, ``validation/gate``, and court-specific scrapers.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def strip_llm_json_fences(text: str) -> str:
    """Strip markdown code fences and surrounding whitespace from LLM output.

    Many LLM models wrap JSON responses in markdown code fences like::

        ```json
        {"key": "value"}
        ```

    This function removes those fences so the result can be passed to
    ``json.loads`` directly.  It handles:

    * ````` ```json ````` (with language tag)
    * ````` ``` ````` (without language tag)
    * Fences with or without trailing newlines
    * Leading/trailing whitespace outside the fences

    It does **not** attempt to extract JSON from responses that contain
    explanatory text after the closing fence — callers that need that
    behaviour should implement their own fallback logic.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned


def parse_llm_json(text: str) -> Any:
    """Strip markdown code fences and parse the result as JSON (relaxed mode).

    Combines :func:`strip_llm_json_fences` with ``json.loads(..., strict=False)``
    so that unescaped control characters (U+0000 to U+001F) appearing inside
    JSON string values do not cause the whole response to be rejected.

    **Why relaxed mode?** RFC 8259 §7 requires control characters in strings
    to be escaped (e.g. ``\\u001b`` for ESC), but LLMs occasionally emit them
    unescaped — see #2518 where ~12% of Santa Clara rebuild PDFs failed with
    ``Invalid control character at: line N column M``.  Python's
    ``json.loads(..., strict=False)`` accepts these chars as-is inside string
    values, which preserves the extracted text rather than dropping the whole
    document.  Structural JSON errors (missing braces, invalid tokens, bad
    escapes) still raise :class:`json.JSONDecodeError` as expected.

    Args:
        text: Raw LLM response text, optionally wrapped in markdown code fences.

    Returns:
        The parsed JSON value (dict, list, str, int, float, bool, or ``None``).

    Raises:
        json.JSONDecodeError: If the response is structurally invalid JSON.
            Control characters inside string values are **not** considered
            invalid — those are tolerated by design.
    """
    cleaned = strip_llm_json_fences(text)
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError as exc:
        repaired, fixes = _repair_unescaped_quotes(cleaned, exc)
        if repaired is None:
            raise
        logger.info("llm_utils.json_repaired", fixes=fixes, error=str(exc))
        return repaired


# Errors the decoder raises right after a string ended at a stray quote:
# the text that follows the quote is not a delimiter.
_STRAY_QUOTE_ERRORS = (
    "Expecting ',' delimiter",
    "Expecting ':' delimiter",
    "Expecting property name enclosed in double quotes",
)

# Upper bound on repairs in one response; each one fixes one stray character.
_MAX_JSON_REPAIRS = 50


def _repair_unescaped_quotes(text: str, error: json.JSONDecodeError) -> tuple[Any, int]:
    """Parse *text* after escaping the stray characters that break it (#5007).

    Gemini copies source text into JSON strings verbatim.  When that text
    holds a straight double quote (``(“ . . . thusly:")``) or a lone
    backslash, the model sometimes leaves it unescaped, and the same input
    gives the same broken answer on every retry.  Re-asking, even in
    halves, never helps.  This repairs exactly those two faults:

    * A stray ``"`` ends the string early, so the decoder fails just after
      it, on prose where it expected a delimiter.  The quote is escaped.
      The failure must land on a non-quote character: a missing comma
      between two fields (``"a" "b"``) is not repaired.
    * An invalid escape (``\\s``) has its backslash doubled.

    Each fix must move the first decode error forward, and at most
    ``_MAX_JSON_REPAIRS`` are made.  Returns ``(value, fixes)``, or
    ``(None, 0)`` when the text cannot be repaired this way.  The callers
    still check the parsed value's shape.
    """
    fixes = 0
    current = text
    exc = error
    while fixes < _MAX_JSON_REPAIRS:
        pos = exc.pos
        if exc.msg.startswith("Invalid \\escape") and current[pos : pos + 1] == "\\":
            current = current[:pos] + "\\" + current[pos:]
        elif exc.msg in _STRAY_QUOTE_ERRORS and current[pos : pos + 1] not in ('"', ""):
            quote = current.rfind('"', 0, pos)
            # Only a quote with nothing but whitespace or a comma after it
            # can have ended the string the decoder failed after.
            between = current[quote + 1 : pos] if quote >= 0 else ""
            if quote <= 0 or current[quote - 1] == "\\" or between.strip(" \t\r\n,"):
                return None, 0
            current = current[:quote] + "\\" + current[quote:]
        else:
            return None, 0
        fixes += 1
        try:
            return json.loads(current, strict=False), fixes
        except json.JSONDecodeError as next_exc:
            # The next fault must lie beyond this fix, or the repair is
            # going in circles.
            if next_exc.pos <= pos:
                return None, 0
            exc = next_exc
    return None, 0
