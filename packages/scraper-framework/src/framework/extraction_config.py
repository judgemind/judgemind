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
        max_output_tokens=32768,
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
    # Federal CourtListener clusters ship structured case_name_full, docket_number,
    # judges, and date_filed — the per-document plain text is a federal opinion, not
    # a CA tentative-ruling calendar.  Running the default CA-tuned LLM prompt over
    # federal opinion text produces truncation and field-contamination failures
    # (issue #3967).  ExtractionMethod.NONE short-circuits LLM extraction in
    # worker.py:1579-1588 so the scraper-populated fields are used as-is.
    # Precedent: the SD calendar _SCRAPER_CONFIGS entry (#2331) uses the same shape.
    # Key uses ("FEDERAL", "FEDERAL") because get_county_extraction_config uppercases
    # both halves at extraction_config.py:225.
    ("FEDERAL", "FEDERAL"): CountyExtractionConfig(
        method=ExtractionMethod.NONE,
    ),
}


# ---------------------------------------------------------------------------
# Scraper-level extraction overrides
# ---------------------------------------------------------------------------
# Some scrapers within a county need a different extraction method than the
# county default.  For example, San Diego has two scraper paths:
#   - ca-sd-calendar:    structured HTML calendar, fully parsed by the scraper
#   - ca-sd-tentatives:  ROA ruling text, needs LLM for outcome/motion_type
#
# The county default is LLM (for tentative rulings), but the calendar scraper
# should use NONE because its structured HTML is already fully parsed — the
# scraper extracts all metadata (judge, department, hearing date, case number,
# case title, parties, motion type) directly from the HTML table structure.
# Running the LLM on calendar HTML is wasteful and produces wrong results
# (wrong dates from body text, truncated multi-case output, raw HTML stored
# as ruling_text).  See #2331.
#
# Key: scraper_id string.
#
# ``rebuild-ca-san_diego`` is a synthetic scraper_id emitted by
# ``scripts/rebuild_db.py`` when seeding the local DB from S3.  The S3
# payload for San Diego is an entire calendar HTML page (60-100KB, 30+
# cases), identical to what ``ca-sd-calendar`` captures.  Without an
# override, the (CA, SAN DIEGO) county default (LLM) fires and the
# whole HTML is shipped to the model with a prompt assuming a single
# case, producing 50000-char raw-HTML dumps and swapped identifiers
# (#2447).  Registering the synthetic scraper_id with NONE forces the
# worker to route the document through the deterministic SD splitter
# (``courts.ca.sd_calendar._split_rulings``) instead.
_SCRAPER_CONFIGS: dict[str, CountyExtractionConfig] = {
    "ca-sd-calendar": CountyExtractionConfig(
        method=ExtractionMethod.NONE,
    ),
    "rebuild-ca-san_diego": CountyExtractionConfig(
        method=ExtractionMethod.NONE,
    ),
}


def get_county_extraction_config(
    state: str,
    county: str,
    *,
    scraper_id: str | None = None,
) -> CountyExtractionConfig | None:
    """Look up the extraction config for a (state, county) pair.

    When *scraper_id* is provided, checks for a scraper-level override
    first.  This allows scrapers within the same county to use different
    extraction methods (e.g., San Diego calendar uses ``NONE`` while
    San Diego tentative rulings uses ``LLM``).

    Returns ``None`` if no custom configuration exists — the caller
    should fall back to the default framework extraction behaviour.

    Parameters
    ----------
    state : str
        Two-letter state code (e.g. ``"CA"``).
    county : str
        County name (e.g. ``"Riverside"``).
    scraper_id : str | None
        Optional scraper ID for scraper-level overrides.

    Returns
    -------
    CountyExtractionConfig | None
        The scraper-specific or county-specific config, or ``None``
        if not registered.
    """
    # Check scraper-level override first.
    if scraper_id and scraper_id in _SCRAPER_CONFIGS:
        return _SCRAPER_CONFIGS[scraper_id]
    return _COUNTY_CONFIGS.get((state.upper(), county.upper()))


# ---------------------------------------------------------------------------
# Extraction strategy — single source of truth for worker + reingest gates
# ---------------------------------------------------------------------------
# The ingestion worker (``packages/scraper-framework/src/ingestion/worker.py``)
# and the reingest path (``scripts/reingest_from_s3.py``) both decide:
#
#   - Should the framework LLM be skipped entirely? (``ExtractionMethod.NONE``)
#   - Should the multimodal extractor handle this document? (raw-PDF + per-page
#     images, ``ExtractionMethod.MULTIMODAL``)
#   - Which provider/model/system_prompt should the text LLM call use?
#   - What max_output_tokens cap applies?
#   - What max_chars_per_chunk cap applies?
#
# Historically each path consulted ``get_county_extraction_config`` inline and
# spelled the gate logic out itself.  When ``ExtractionMethod`` gained the
# ``NONE`` value the worker honored it but reingest did not — producing the
# divergence pattern in #2490, #2501, #2502, #2521, #4056.  The Option A
# parity test in ``tests/test_worker_reingest_parity.py`` (#4071) was the
# preventative; this helper is the structural fix described in #4081 (Option B).
#
# Both paths now call ``decide_extraction_strategy(...)`` once and read the
# resulting ``ExtractionStrategy`` fields.  Adding a new ``ExtractionMethod``
# value or a new ``CountyExtractionConfig`` field updates both paths in one
# place — the divergence pattern becomes structurally impossible.
#
# Default semantics for unconfigured (state, county, scraper_id) tuples
# preserve the worker's pre-refactor behavior:
#
#   - ``skip_llm = False``      — run framework extraction
#   - ``use_multimodal = True`` — allow multimodal when a raw PDF is present
#                                  (worker.py L1738 default-multimodal path)
#   - ``max_output_tokens = 4096`` — framework default (#2355)
#   - ``system_prompt = None``  — caller falls back to ``EXTRACTION_SYSTEM_PROMPT``
#   - ``provider = None``       — caller uses framework-default provider
#   - ``model = None``          — caller uses provider-default model
#   - ``max_chars_per_chunk = None`` — caller uses ``LlmExtractor`` default

_DEFAULT_MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class ExtractionStrategy:
    """Per-document extraction decision derived from county+scraper config.

    All fields are populated to concrete values where possible; ``None``
    means "use the framework/extractor default" so callers do not need to
    re-derive defaults.

    Attributes:
        skip_llm: True when ``ExtractionMethod.NONE`` is configured —
            short-circuit framework LLM extraction entirely.  The scraper
            populates all fields itself (e.g. SD calendar HTML, Federal
            CourtListener clusters).
        use_multimodal: True when ``ExtractionMethod.MULTIMODAL`` is
            configured OR no config exists for the (state, county,
            scraper_id) tuple.  Worker uses this to decide whether to
            download the raw PDF from S3 and route to the per-page
            multimodal extractor.  Reingest's ``_reparse_document`` does
            not have a multimodal sub-path today, so the flag is
            informational there — but exposing it keeps the strategy
            self-describing for future reingest enhancements.
        max_output_tokens: Per-call max output tokens for the LLM.
            Defaults to ``4096`` when no override is configured (#2355).
        system_prompt: County-specific system prompt, or ``None`` to use
            the framework default ``EXTRACTION_SYSTEM_PROMPT``.
        provider: ``"anthropic"`` or ``"google"``, or ``None`` to use
            the framework default (Anthropic).
        model: Provider-specific model id, or ``None`` to use the
            provider default.
        max_chars_per_chunk: Per-chunk character cap for chunked
            extraction.  ``None`` falls back to the ``LlmExtractor``
            default (80,000).
    """

    skip_llm: bool
    use_multimodal: bool
    max_output_tokens: int
    system_prompt: str | None
    provider: str | None
    model: str | None
    max_chars_per_chunk: int | None


def decide_extraction_strategy(
    state: str,
    county: str,
    *,
    scraper_id: str | None = None,
    has_raw_pdf: bool = False,  # noqa: ARG001 — reserved for future selectors
) -> ExtractionStrategy:
    """Resolve the extraction strategy for a (state, county, scraper_id) tuple.

    Single source of truth for both ``ingestion.worker`` and
    ``scripts/reingest_from_s3.py``.  See module docstring for the full
    divergence-history rationale.

    Parameters
    ----------
    state : str
        Two-letter state code (e.g. ``"CA"``).
    county : str
        County name (e.g. ``"Riverside"``).
    scraper_id : str | None
        Optional scraper ID for scraper-level overrides.  Same lookup
        precedence as ``get_county_extraction_config``.
    has_raw_pdf : bool
        Whether the caller currently has the document's raw PDF bytes
        in hand.  Reserved for future per-document multimodal selectors;
        not consulted today.  Accepted as a keyword argument so the
        helper signature does not need to change when a future
        ``ExtractionMethod`` adds a raw-PDF dependency.

    Returns
    -------
    ExtractionStrategy
        Populated strategy with concrete defaults — callers do not need
        to re-derive the 4096 default or the multimodal default branch.
    """
    config = get_county_extraction_config(state, county, scraper_id=scraper_id)

    if config is None:
        # No config — preserve worker.py's pre-refactor default behavior:
        # multimodal is allowed (worker.py L1738), framework default
        # 4096-token cap, no county prompt, default provider/model.
        return ExtractionStrategy(
            skip_llm=False,
            use_multimodal=True,
            max_output_tokens=_DEFAULT_MAX_OUTPUT_TOKENS,
            system_prompt=None,
            provider=None,
            model=None,
            max_chars_per_chunk=None,
        )

    skip_llm = config.method == ExtractionMethod.NONE
    use_multimodal = config.method == ExtractionMethod.MULTIMODAL
    max_output_tokens = (
        config.max_output_tokens
        if config.max_output_tokens is not None
        else _DEFAULT_MAX_OUTPUT_TOKENS
    )

    return ExtractionStrategy(
        skip_llm=skip_llm,
        use_multimodal=use_multimodal,
        max_output_tokens=max_output_tokens,
        system_prompt=config.system_prompt,
        provider=config.provider,
        model=config.model,
        max_chars_per_chunk=config.max_chars_per_chunk,
    )
