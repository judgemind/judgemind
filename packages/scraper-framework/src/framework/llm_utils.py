"""Shared utilities for parsing LLM responses.

Consolidates common patterns (e.g. code-fence stripping) that were previously
duplicated across ``llm_extractor``, ``llm_extract``, ``llm_enrichment``,
``ruling_formatter``, ``validation/gate``, and court-specific scrapers.
"""

from __future__ import annotations

import json
import re
from typing import Any


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
    return json.loads(cleaned, strict=False)
