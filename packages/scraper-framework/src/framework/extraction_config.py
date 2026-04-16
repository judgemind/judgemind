"""County-specific extraction configuration.

Maps (state, county) pairs to extraction methods and LLM parameters.
Counties not in the registry use the default framework-level
``EXTRACTION_SYSTEM_PROMPT`` with the Anthropic provider.

The ``ExtractionMethod`` enum determines *how* a document is extracted:

- **LLM** — Use the framework ``LlmExtractor`` with the configured
  (or default) system prompt, provider, and model.  This is the
  standard path for all counties.
- **MULTIMODAL** — Use ``LlmExtractor.extract_from_pdf()`` with
  per-page image extraction.  For tabular PDFs (e.g. OC) where
  text extraction is unreliable.
- **NONE** — No framework-level extraction.  The scraper handles
  everything (e.g. LA HTML scraper which does its own parsing).

When a county has a custom ``system_prompt``, the ``LlmExtractor``
uses that prompt instead of the generic ``EXTRACTION_SYSTEM_PROMPT``.

Per-county prompts live in ``framework.prompts`` (one module per
county).  They are re-exported here for backward compatibility::

    from framework.extraction_config import RIVERSIDE_SYSTEM_PROMPT  # still works
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Re-export per-county prompts so existing import sites keep working.
from .prompts import (  # noqa: F401
    CONTRA_COSTA_SYSTEM_PROMPT,
    FRESNO_SYSTEM_PROMPT,
    RIVERSIDE_SYSTEM_PROMPT,
    SAN_BERNARDINO_SYSTEM_PROMPT,
    SAN_DIEGO_FRAMEWORK_PROMPT,
    SAN_DIEGO_SYSTEM_PROMPT,
    SAN_FRANCISCO_SYSTEM_PROMPT,
    SANTA_CLARA_SYSTEM_PROMPT,
    VENTURA_FRAMEWORK_PROMPT,
    VENTURA_SYSTEM_PROMPT,
)


class ExtractionMethod(StrEnum):
    """How the framework extracts structured data from a document."""

    LLM = "llm"
    MULTIMODAL = "multimodal"
    NONE = "none"


@dataclass(frozen=True)
class CountyExtractionConfig:
    """Extraction configuration for a single county.

    Attributes:
        method: How to extract structured data.
        system_prompt: Custom system prompt for the LLM.  If ``None``,
            the default ``EXTRACTION_SYSTEM_PROMPT`` is used.
        provider: LLM provider (``"anthropic"`` or ``"google"``).
            If ``None``, uses the framework default (Anthropic).
        model: LLM model ID.  If ``None``, uses the provider default.
        max_output_tokens: Maximum tokens in the model response.
            If ``None``, uses the ``LlmExtractor`` default (4096).
        max_chars_per_chunk: Per-chunk character limit for large
            documents.  If ``None``, uses the ``LlmExtractor`` default
            (80,000).  Counties with large multi-ruling PDFs (e.g. SF
            family law) may need a smaller value to prevent output
            truncation.
    """

    method: ExtractionMethod = ExtractionMethod.LLM
    system_prompt: str | None = None
    provider: str | None = None
    model: str | None = None
    max_output_tokens: int | None = None
    max_chars_per_chunk: int | None = None


# ---------------------------------------------------------------------------
# County extraction registry
# ---------------------------------------------------------------------------

# Key: (state, county) tuple, both uppercase for consistent lookup.
_COUNTY_CONFIGS: dict[tuple[str, str], CountyExtractionConfig] = {
    ("CA", "RIVERSIDE"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=RIVERSIDE_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "ORANGE"): CountyExtractionConfig(
        method=ExtractionMethod.MULTIMODAL,
    ),
    ("CA", "SAN BERNARDINO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SAN_BERNARDINO_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "SAN FRANCISCO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SAN_FRANCISCO_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
        max_chars_per_chunk=40_000,
    ),
    ("CA", "FRESNO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=FRESNO_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "SANTA CLARA"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SANTA_CLARA_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "VENTURA"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=VENTURA_FRAMEWORK_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "CONTRA COSTA"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=CONTRA_COSTA_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "SAN DIEGO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SAN_DIEGO_FRAMEWORK_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
}


def get_county_extraction_config(
    state: str,
    county: str,
) -> CountyExtractionConfig | None:
    """Look up the extraction config for a (state, county) pair.

    Returns ``None`` if no custom configuration exists — the caller
    should fall back to the default framework extraction behaviour.

    Parameters
    ----------
    state : str
        Two-letter state code (e.g. ``"CA"``).
    county : str
        County name (e.g. ``"Riverside"``).

    Returns
    -------
    CountyExtractionConfig | None
        The county-specific config, or ``None`` if not registered.
    """
    return _COUNTY_CONFIGS.get((state.upper(), county.upper()))
