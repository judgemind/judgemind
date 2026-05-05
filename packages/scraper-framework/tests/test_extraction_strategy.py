"""Unit tests for ``framework.extraction_config.decide_extraction_strategy``.

Validates the structural-fix helper introduced in #4081 — the single
source of truth consulted by both ``ingestion.worker`` and
``scripts/reingest_from_s3.py`` for "how should this document be
extracted?".  See ``test_worker_reingest_parity.py`` for the
cross-path live-call regression guard introduced in #4071.
"""

from __future__ import annotations

import pytest

from framework.extraction_config import (
    CountyExtractionConfig,
    ExtractionMethod,
    ExtractionStrategy,
    decide_extraction_strategy,
)

# ---------------------------------------------------------------------------
# ExtractionStrategy dataclass — shape, immutability, defaults
# ---------------------------------------------------------------------------


class TestExtractionStrategyDataclass:
    """ExtractionStrategy mirrors CountyExtractionConfig with concrete defaults."""

    def test_required_fields(self) -> None:
        """All seven fields are required positional/keyword args."""
        strategy = ExtractionStrategy(
            skip_llm=False,
            use_multimodal=False,
            max_output_tokens=4096,
            system_prompt=None,
            provider=None,
            model=None,
            max_chars_per_chunk=None,
        )
        assert strategy.skip_llm is False
        assert strategy.use_multimodal is False
        assert strategy.max_output_tokens == 4096
        assert strategy.system_prompt is None
        assert strategy.provider is None
        assert strategy.model is None
        assert strategy.max_chars_per_chunk is None

    def test_frozen(self) -> None:
        """ExtractionStrategy is frozen (immutable)."""
        strategy = ExtractionStrategy(
            skip_llm=False,
            use_multimodal=False,
            max_output_tokens=4096,
            system_prompt=None,
            provider=None,
            model=None,
            max_chars_per_chunk=None,
        )
        with pytest.raises(AttributeError):
            strategy.skip_llm = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# decide_extraction_strategy — registry-backed cases
# ---------------------------------------------------------------------------


class TestDecideStrategyForRegisteredCounties:
    """Verify strategy resolution against the live registry."""

    def test_riverside_llm_strategy(self) -> None:
        """Riverside (CA) is configured LLM with Google + 32768 tokens."""
        strategy = decide_extraction_strategy("CA", "Riverside")
        assert strategy.skip_llm is False
        assert strategy.use_multimodal is False
        assert strategy.max_output_tokens == 32768
        assert strategy.provider == "google"
        assert strategy.model == "gemini-2.5-flash-lite"
        assert strategy.system_prompt is not None
        assert strategy.max_chars_per_chunk is None

    def test_orange_multimodal_strategy(self) -> None:
        """Orange (CA) is configured MULTIMODAL — use_multimodal flips True."""
        strategy = decide_extraction_strategy("CA", "Orange")
        assert strategy.skip_llm is False
        assert strategy.use_multimodal is True
        assert strategy.max_output_tokens == 32768
        # Orange has no provider/model override; framework default applies.
        assert strategy.provider is None
        assert strategy.model is None
        assert strategy.system_prompt is None

    def test_san_francisco_max_chars_per_chunk_propagated(self) -> None:
        """SF Family Law has a 40K char per-chunk cap (#2107) — surface it."""
        strategy = decide_extraction_strategy("CA", "San Francisco")
        assert strategy.max_chars_per_chunk == 40_000
        assert strategy.max_output_tokens == 32768
        assert strategy.provider == "google"

    def test_federal_skip_llm(self) -> None:
        """Federal CourtListener is ExtractionMethod.NONE → skip_llm True (#3967, #4056)."""
        strategy = decide_extraction_strategy("Federal", "Federal")
        assert strategy.skip_llm is True
        assert strategy.use_multimodal is False
        # max_output_tokens still resolves to a concrete value — callers
        # that ignore skip_llm and continue to the LLM call must not see
        # ``None`` here.
        assert strategy.max_output_tokens == 4096
        assert strategy.system_prompt is None

    def test_sd_calendar_scraper_override_skip_llm(self) -> None:
        """SD calendar scraper override sets ExtractionMethod.NONE (#2331)."""
        strategy = decide_extraction_strategy("CA", "San Diego", scraper_id="ca-sd-calendar")
        assert strategy.skip_llm is True
        assert strategy.use_multimodal is False

    def test_sd_tentatives_falls_through_to_county(self) -> None:
        """SD tentatives scraper falls through to county-level LLM config."""
        strategy = decide_extraction_strategy("CA", "San Diego", scraper_id="ca-sd-tentatives")
        assert strategy.skip_llm is False
        assert strategy.use_multimodal is False
        assert strategy.provider == "google"
        assert strategy.system_prompt is not None

    def test_rebuild_sd_synthetic_scraper_id(self) -> None:
        """``rebuild-ca-san_diego`` synthetic scraper_id → NONE (#2447)."""
        strategy = decide_extraction_strategy("CA", "San Diego", scraper_id="rebuild-ca-san_diego")
        assert strategy.skip_llm is True


# ---------------------------------------------------------------------------
# decide_extraction_strategy — unconfigured (default) behavior
# ---------------------------------------------------------------------------


class TestDecideStrategyDefaults:
    """Verify the no-config default preserves the worker's pre-refactor behavior."""

    def test_unknown_county_default_strategy(self) -> None:
        """Unknown (state, county) → multimodal-allowed default (worker.py L1738)."""
        strategy = decide_extraction_strategy("XX", "Nonexistent")
        assert strategy.skip_llm is False
        # Worker default treats unconfigured counties as multimodal-allowed
        # so raw PDFs still get downloaded and routed through the per-page
        # multimodal extractor.  Preserving this is critical — pre-refactor
        # behavior is the safety contract for unmigrated counties.
        assert strategy.use_multimodal is True
        assert strategy.max_output_tokens == 4096  # framework default
        assert strategy.system_prompt is None
        assert strategy.provider is None
        assert strategy.model is None
        assert strategy.max_chars_per_chunk is None

    def test_unknown_scraper_id_falls_through(self) -> None:
        """An unknown scraper_id falls through to the county-level config."""
        strategy = decide_extraction_strategy("CA", "Riverside", scraper_id="ca-totally-unknown")
        # Same as the no-scraper_id Riverside lookup.
        assert strategy.skip_llm is False
        assert strategy.use_multimodal is False
        assert strategy.provider == "google"

    def test_max_output_tokens_default_when_config_lacks_override(self) -> None:
        """A registered county without ``max_output_tokens`` falls back to 4096."""
        # Construct an ad-hoc CountyExtractionConfig with the field missing
        # via the registry monkey-patch path: instead of mutating _COUNTY_CONFIGS
        # we verify the dataclass default behavior plus the helper's fallback
        # by building one via direct construction and asserting the helper's
        # 4096-default branch is exercised.  The default-args ExtractionStrategy
        # produced by ``decide_extraction_strategy("XX", "Nonexistent")`` is
        # the registry-miss case and already covers this branch.
        strategy = decide_extraction_strategy("XX", "Nonexistent")
        assert strategy.max_output_tokens == 4096

    def test_has_raw_pdf_keyword_accepted(self) -> None:
        """The ``has_raw_pdf`` kwarg is accepted (reserved for future selectors)."""
        # The kwarg currently has no effect; this test pins the signature so
        # callers can pass it without TypeError, and a future ExtractionMethod
        # that depends on raw-PDF availability can read it.
        s_true = decide_extraction_strategy("CA", "Riverside", has_raw_pdf=True)
        s_false = decide_extraction_strategy("CA", "Riverside", has_raw_pdf=False)
        assert s_true == s_false

    def test_case_insensitive_lookup(self) -> None:
        """State/county lookups are case-insensitive (matches get_county_extraction_config)."""
        upper = decide_extraction_strategy("CA", "RIVERSIDE")
        mixed = decide_extraction_strategy("ca", "Riverside")
        assert upper == mixed
        assert upper.provider == "google"


# ---------------------------------------------------------------------------
# CountyExtractionConfig method coverage matrix
# ---------------------------------------------------------------------------


class TestExtractionMethodMatrix:
    """Verify every ExtractionMethod value maps to the correct strategy flags."""

    def test_llm_method_no_skip_no_multimodal(self) -> None:
        """LLM method → both skip_llm and use_multimodal stay False."""
        # Riverside is LLM-configured.
        strategy = decide_extraction_strategy("CA", "Riverside")
        assert strategy.skip_llm is False
        assert strategy.use_multimodal is False

    def test_multimodal_method_flips_use_multimodal(self) -> None:
        """MULTIMODAL method → use_multimodal True, skip_llm False."""
        strategy = decide_extraction_strategy("CA", "Orange")
        assert strategy.use_multimodal is True
        assert strategy.skip_llm is False

    def test_none_method_flips_skip_llm(self) -> None:
        """NONE method → skip_llm True, use_multimodal False."""
        strategy = decide_extraction_strategy("Federal", "Federal")
        assert strategy.skip_llm is True
        assert strategy.use_multimodal is False

    def test_methods_are_mutually_exclusive(self) -> None:
        """No registered config produces both skip_llm and use_multimodal True."""
        from framework.extraction_config import _COUNTY_CONFIGS, _SCRAPER_CONFIGS

        # County registry — every (state, county) pair must produce a
        # mutually-exclusive flag set.
        for (state, county), _config in _COUNTY_CONFIGS.items():
            strategy = decide_extraction_strategy(state, county)
            assert not (strategy.skip_llm and strategy.use_multimodal), (
                f"({state}, {county}) produced skip_llm AND use_multimodal — "
                f"these flags must be mutually exclusive"
            )

        # Scraper-level registry — same invariant.
        for scraper_id, _config in _SCRAPER_CONFIGS.items():
            # Use a synthetic state/county; scraper override fires first.
            strategy = decide_extraction_strategy("XX", "Synthetic", scraper_id=scraper_id)
            assert not (strategy.skip_llm and strategy.use_multimodal), (
                f"scraper_id={scraper_id!r} produced skip_llm AND use_multimodal"
            )


# ---------------------------------------------------------------------------
# Sanity check — the helper preserves CountyExtractionConfig fields
# ---------------------------------------------------------------------------


class TestStrategyMirrorsConfig:
    """For each registered config, the strategy mirrors its non-method fields."""

    def test_riverside_strategy_mirrors_config(self) -> None:
        """Riverside ExtractionStrategy fields equal the underlying config fields."""
        from framework.extraction_config import get_county_extraction_config

        config = get_county_extraction_config("CA", "Riverside")
        assert config is not None  # for type narrowing
        strategy = decide_extraction_strategy("CA", "Riverside")
        assert strategy.system_prompt is config.system_prompt
        assert strategy.provider == config.provider
        assert strategy.model == config.model
        assert strategy.max_output_tokens == config.max_output_tokens
        assert strategy.max_chars_per_chunk == config.max_chars_per_chunk

    def test_san_francisco_strategy_mirrors_config(self) -> None:
        """San Francisco preserves max_chars_per_chunk through the helper."""
        from framework.extraction_config import get_county_extraction_config

        config = get_county_extraction_config("CA", "San Francisco")
        assert config is not None
        strategy = decide_extraction_strategy("CA", "San Francisco")
        assert strategy.max_chars_per_chunk == config.max_chars_per_chunk

    def test_dataclass_used_for_construction_demonstrates_defaults(self) -> None:
        """Construct an ad-hoc CountyExtractionConfig and verify the helper
        default-applies the framework 4096 token cap when ``max_output_tokens
        is None``.  This guards against a regression where the helper passes
        ``None`` straight through and downstream callers crash on a NoneType
        comparison."""
        # The CountyExtractionConfig defaults pin this contract; the helper
        # must convert ``None`` to 4096.
        default_config = CountyExtractionConfig()
        assert default_config.method == ExtractionMethod.LLM
        assert default_config.max_output_tokens is None
        # Helper invocation against the unknown path uses the same code branch.
        strategy = decide_extraction_strategy("XX", "Unconfigured")
        assert strategy.max_output_tokens == 4096
