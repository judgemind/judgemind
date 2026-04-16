"""Shared utilities for parsing LLM responses.

Consolidates common patterns (e.g. code-fence stripping) that were previously
duplicated across ``llm_extractor``, ``llm_extract``, ``llm_enrichment``,
``ruling_formatter``, ``validation/gate``, and court-specific scrapers.
"""

from __future__ import annotations

import re


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
