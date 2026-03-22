"""Per-county extraction method configuration.

Controls whether the ingestion worker uses regex-based splitters or the
LLM-based extractor for each county. Feature-flagged for incremental rollout
per decision #1467.

The extraction method is determined at ingestion time (not scrape time) so
historical documents can be re-processed through the new pipeline without
re-scraping.

Environment variable override:
    ``EXTRACTION_METHOD_<STATE>_<COUNTY>`` — override the configured method
    for a specific county (e.g. ``EXTRACTION_METHOD_CA_ORANGE=llm``).
    This allows operators to switch counties without code changes.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum

logger = logging.getLogger(__name__)


class ExtractionMethod(StrEnum):
    """Extraction method for a county's documents."""

    REGEX = "regex"
    LLM = "llm"


# ---------------------------------------------------------------------------
# Per-county default extraction method.
#
# Counties not listed here default to REGEX.  Rollout order (per #1467):
#   1. Orange County — most parsing bugs
#   2. Riverside — cross-contamination, motion text in titles
#   3. San Bernardino — unsplit calendars
#   4. Los Angeles — null titles, metadata in parties
#   5. San Francisco, Santa Clara, Ventura — lower volume
#
# Start with OC on LLM; others remain on regex until validated.
# ---------------------------------------------------------------------------

_COUNTY_EXTRACTION_METHODS: dict[tuple[str, str], ExtractionMethod] = {
    ("CA", "Orange"): ExtractionMethod.LLM,
}


def get_extraction_method(state: str, county: str) -> ExtractionMethod:
    """Return the configured extraction method for a (state, county) pair.

    Resolution order:
      1. Environment variable ``EXTRACTION_METHOD_<STATE>_<COUNTY>``
         (county name uppercased, spaces replaced with underscores).
      2. Hardcoded ``_COUNTY_EXTRACTION_METHODS`` table.
      3. Default: ``ExtractionMethod.REGEX``.

    Parameters
    ----------
    state : str
        Two-letter state code (e.g. ``"CA"``).
    county : str
        County name as it appears in event payloads (e.g. ``"Orange"``).

    Returns
    -------
    ExtractionMethod
        The extraction method to use for this county.
    """
    # Check environment variable override first.
    env_key = f"EXTRACTION_METHOD_{state}_{county}".upper().replace(" ", "_")
    env_val = os.environ.get(env_key, "").strip().lower()
    if env_val:
        try:
            method = ExtractionMethod(env_val)
            logger.debug(
                "Extraction method override from env %s=%s",
                env_key,
                method.value,
            )
            return method
        except ValueError:
            logger.warning(
                "Invalid extraction method in env %s=%s — ignoring",
                env_key,
                env_val,
            )

    # Look up in the hardcoded table.
    return _COUNTY_EXTRACTION_METHODS.get(
        (state, county),
        ExtractionMethod.REGEX,
    )


def set_extraction_method(state: str, county: str, method: ExtractionMethod) -> None:
    """Programmatically set the extraction method for a county.

    Primarily useful in tests. In production, prefer the environment variable
    override or updating ``_COUNTY_EXTRACTION_METHODS``.

    Parameters
    ----------
    state : str
        Two-letter state code.
    county : str
        County name.
    method : ExtractionMethod
        The extraction method to set.
    """
    _COUNTY_EXTRACTION_METHODS[(state, county)] = method


def reset_extraction_methods() -> None:
    """Reset the extraction method table to defaults.

    Only used in tests to avoid cross-test contamination.
    """
    _COUNTY_EXTRACTION_METHODS.clear()
    _COUNTY_EXTRACTION_METHODS[("CA", "Orange")] = ExtractionMethod.LLM
