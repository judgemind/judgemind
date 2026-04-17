"""Tests for the citation-artifact filter in LlmExtractor (#2448).

The filter removes LLM-extracted "rulings" that are actually inline citations
to other courts' orders rather than the PDF's primary case(s). Citations are
common in attorney-fee orders (Song-Beverly Act), where the ruling body
cites 10-24 precedent orders — each of which the LLM incorrectly splits
into a separate ruling.

The filter uses two signals in conjunction:
1. ``extracted_case_title`` contains a citator pattern at the end
   (federal district court, reporter, LASC BC-number, etc.)
2. ``ruling_text`` is shorter than ``_CITATION_MIN_RULING_LENGTH`` chars

Both must fire for the entry to be filtered. This keeps the filter
conservative — legitimate short rulings (e.g. ``off_calendar``) with clean
titles are never removed.

See #2448 for the motivating bug.
"""

from __future__ import annotations

from framework.llm_extractor import (
    _CITATION_MIN_RULING_LENGTH,
    _filter_citation_artifacts,
    _is_citation_artifact,
)
from framework.llm_schema import ExtractedRuling


class TestIsCitationArtifact:
    """Tests for the per-ruling citation-detection predicate."""

    # -- Federal district court citator ----------------------------------

    def test_cd_cal_suffix(self) -> None:
        """Title ending with 'C.D. Cal.' is a citation when text is short."""
        ruling = ExtractedRuling(
            extracted_case_title="Henrik Zargarian v. BMW of N. Am., LLC C.D. Cal. Mar. 3, 2020",
            ruling_text="A" * 299,
        )
        assert _is_citation_artifact(ruling) is True

    def test_nd_cal_suffix(self) -> None:
        """Title ending with 'N.D. Cal.' is a citation when text is short."""
        ruling = ExtractedRuling(
            extracted_case_title="Smith v. Ford Motor Co. (N.D. Cal. 2022)",
            ruling_text="B" * 250,
        )
        assert _is_citation_artifact(ruling) is True

    def test_ed_cal_suffix(self) -> None:
        """E.D. Cal. citator triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Jones v. BMW (E.D. Cal. 2023)",
            ruling_text="short ruling text",
        )
        assert _is_citation_artifact(ruling) is True

    def test_sd_cal_suffix(self) -> None:
        """S.D. Cal. citator triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Brown v. Kia (S.D. Cal. 2024)",
            ruling_text="short ruling text",
        )
        assert _is_citation_artifact(ruling) is True

    # -- Federal case-number embedded in title ---------------------------

    def test_federal_case_number_in_title(self) -> None:
        """'8:22-cv-01092DFM' embedded in title triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Debora Guzman V. FCA US LLC et al 8:22-cv-01092DFM",
            ruling_text="C" * 206,
        )
        assert _is_citation_artifact(ruling) is True

    def test_federal_case_number_5_21_cv(self) -> None:
        """'5:21-cv-01346SHK'-style citation in title triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Williams v. Ford Motor Company 5:21-cv-01346SHK",
            ruling_text="D" * 279,
        )
        assert _is_citation_artifact(ruling) is True

    def test_federal_case_number_with_judge_code(self) -> None:
        """'18CV475JM(BGS)' LASC-free federal cite in title triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Holcomb v. BMW of N. Am, LLC 18CV475JM(BGS) Fisher",
            ruling_text="E" * 350,
        )
        assert _is_citation_artifact(ruling) is True

    # -- LASC BC-number suffix -------------------------------------------

    def test_lasc_bc_number_suffix(self) -> None:
        """Title ending with 'BC\\d{6,}' old LASC number triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Michelle Williams v. KMA BC722351",
            ruling_text="F" * 155,
        )
        assert _is_citation_artifact(ruling) is True

    def test_lasc_bc_number_with_context(self) -> None:
        """LASC BC-number anywhere past first name/v. triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Klingenberg v. KMA BC709888",
            ruling_text="G" * 194,
        )
        assert _is_citation_artifact(ruling) is True

    # -- Reporter citations ----------------------------------------------

    def test_f_supp_reporter(self) -> None:
        """'F.Supp' reporter citation triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Acme v. Widget, 442 F.Supp.3d 1216",
            ruling_text="H" * 200,
        )
        assert _is_citation_artifact(ruling) is True

    def test_cal_app_reporter(self) -> None:
        """'Cal.App' reporter citation triggers filter."""
        ruling = ExtractedRuling(
            extracted_case_title="Doe v. Corp 12 Cal.App.5th 345",
            ruling_text="I" * 250,
        )
        assert _is_citation_artifact(ruling) is True

    # -- Negative cases: no citator suffix --------------------------------

    def test_clean_title_short_text_not_citation(self) -> None:
        """Short text with clean title (no citator) is NOT filtered."""
        ruling = ExtractedRuling(
            extracted_case_number="CIVSB2600123",
            extracted_case_title="Armistead v. Ford",
            ruling_text="Off calendar.",
        )
        assert _is_citation_artifact(ruling) is False

    def test_clean_title_long_text_not_citation(self) -> None:
        """Long substantive text with clean title is NOT filtered."""
        ruling = ExtractedRuling(
            extracted_case_number="CIVSB2223971",
            extracted_case_title="Armistead v. Ford",
            ruling_text="The motion is denied. " * 200,
        )
        assert _is_citation_artifact(ruling) is False

    def test_citator_title_but_long_text_not_filtered(self) -> None:
        """Citator-looking title with LONG text is NOT filtered.

        If the LLM produced a substantive ruling_text (>= 400 chars) for an entry
        whose title happens to contain a citator, it might be the real ruling
        with an unusually formatted title. Err on the side of keeping it.
        """
        ruling = ExtractedRuling(
            extracted_case_title="Fisher v. BMW 18CV475JM(BGS)",
            ruling_text="Z" * (_CITATION_MIN_RULING_LENGTH + 100),
        )
        assert _is_citation_artifact(ruling) is False

    def test_empty_title_not_filtered(self) -> None:
        """Empty/None title short-circuits to False (can't match citator)."""
        ruling = ExtractedRuling(
            extracted_case_number="CIVSB2600001",
            extracted_case_title=None,
            ruling_text="Short.",
        )
        assert _is_citation_artifact(ruling) is False

    def test_none_ruling_text_is_not_filtered(self) -> None:
        """None ruling_text combined with citator title is still filtered.

        A citation artifact may have no text at all — rely on the title signal
        as sufficient evidence when text is missing entirely.
        """
        ruling = ExtractedRuling(
            extracted_case_title="Diana Parros v. Kia BC710391",
            ruling_text=None,
        )
        # None is shorter than the min length, so this is filtered.
        assert _is_citation_artifact(ruling) is True

    # -- LASC/other-county case numbers in case_number field -------------

    def test_lasc_stcv_case_number(self) -> None:
        """Clean title but LASC ``21STCV`` case_number + short text is a citation."""
        ruling = ExtractedRuling(
            extracted_case_number="21STCV23811",
            extracted_case_title="Charles Steven Sedlacek IV v. General Motors, LLC",
            ruling_text="X" * 263,
        )
        assert _is_citation_artifact(ruling) is True

    def test_lasc_vecv_case_number(self) -> None:
        """LASC ``22VECV`` case_number + short text is a citation."""
        ruling = ExtractedRuling(
            extracted_case_number="22VECV01398",
            extracted_case_title="Mayra Hernandez v. General Motors LLC",
            ruling_text="Y" * 203,
        )
        assert _is_citation_artifact(ruling) is True

    def test_lasc_old_format_case_number(self) -> None:
        """LASC old-format ``BC\\d{6,}`` case_number + short text is a citation."""
        ruling = ExtractedRuling(
            extracted_case_number="BC710391",
            extracted_case_title="Diana Parros v. Kia",
            ruling_text="Z" * 262,
        )
        assert _is_citation_artifact(ruling) is True

    def test_sb_case_number_short_text_not_filtered(self) -> None:
        """SB-format case_number with clean title + short text is NOT filtered."""
        ruling = ExtractedRuling(
            extracted_case_number="CIVSB2104653",
            extracted_case_title="Horne et al v. Ford Motor Company et al",
            ruling_text="Off calendar.",
        )
        assert _is_citation_artifact(ruling) is False

    def test_lasc_case_number_but_long_text_not_filtered(self) -> None:
        """LASC case_number with LONG text is NOT filtered (likely real case)."""
        ruling = ExtractedRuling(
            extracted_case_number="21STCV23811",
            extracted_case_title="Charles Sedlacek v. General Motors, LLC",
            ruling_text="A" * (_CITATION_MIN_RULING_LENGTH + 500),
        )
        assert _is_citation_artifact(ruling) is False

    def test_riverside_cvps_case_number_not_filtered(self) -> None:
        """Riverside ``CVPS`` case_number is NOT filtered.

        ``CVPS`` is a legitimate primary-case format on the Riverside
        tentative-rulings calendar.  The citation filter intentionally
        does not flag Riverside formats because they can appear as real
        cases in the shared LLM extraction path.
        """
        ruling = ExtractedRuling(
            extracted_case_number="CVPS2400892",
            extracted_case_title="Thompson v. City of Palm Springs",
            ruling_text="The demurrer is OVERRULED.",
        )
        assert _is_citation_artifact(ruling) is False


class TestFilterCitationArtifacts:
    """Tests for the list-level filter that wraps the per-ruling predicate."""

    def test_sedlacek_scenario(self) -> None:
        """Regression test: the Sedlacek/Armistead attorney-fee PDF scenario.

        The real LLM output for PDF bda87a90...pdf (#2448) returned 22 rulings,
        of which only 1 (CIVSB2223971 Armistead v. Ford) is the primary case.
        The remaining 21 are citation artifacts — some with citator patterns
        in the title, others with clean titles but out-of-county case numbers
        (LASC ``21STCV``, ``22VECV``, LASC old-format ``BC``, etc.). The filter
        must return 1.
        """
        rulings = [
            # The real case: long substantive ruling text, clean title.
            ExtractedRuling(
                extracted_case_number="CIVSB2223971",
                extracted_case_title="Armistead v. Ford",
                ruling_text=(
                    "Before the Court is Plaintiffs' Motion for Attorneys' Fees, Costs, "
                    "and Expenses. They seek $45,776.31 in total, which consists of: "
                    "$28,343 in attorney fees, $9,920.05 from a 1.35 multiplier "
                    "enhancement, $4,013.26 in costs and expenses, and $4,000 in "
                    "anticipated fees for defending the motion and attending its "
                    "hearing. " * 10
                ),
            ),
            # Citation artifacts by title signal.
            ExtractedRuling(
                extracted_case_title=(
                    "Henrik Zargarian v. BMW of N. Am., LLC C.D. Cal. Mar. 3, 2020"
                ),
                ruling_text="A" * 299,
            ),
            ExtractedRuling(
                extracted_case_title="Michelle Williams v. KMA BC722351",
                ruling_text="B" * 155,
            ),
            ExtractedRuling(
                extracted_case_title="Rahman v. FCA US LLC C.D. Cal. 2022",
                ruling_text="C" * 185,
            ),
            ExtractedRuling(
                extracted_case_title="Klingenberg v. KMA BC709888",
                ruling_text="D" * 194,
            ),
            ExtractedRuling(
                extracted_case_title=(
                    "Sandra J. Williams et al v. Ford Motor Company 5:21-cv-01346SHK"
                ),
                ruling_text="E" * 279,
            ),
            ExtractedRuling(
                extracted_case_title="Sergio Proa v. Kia Motors America Inc. BC716646",
                ruling_text="F" * 283,
            ),
            ExtractedRuling(
                extracted_case_title="Holcomb v. BMW of N. Am, LLC 18CV475JM(BGS) Fisher",
                ruling_text="G" * 350,
            ),
            ExtractedRuling(
                extracted_case_title="Debora Guzman V. FCA US LL[C] et a1 8:22-cv-01092DFM",
                ruling_text="H" * 206,
            ),
            ExtractedRuling(
                extracted_case_title="Diana Parros v. Kia Motors America, Inc. BC710391",
                ruling_text="I" * 262,
            ),
            # Citation artifacts detected by case_number signal (LASC / other
            # counties with clean-looking titles). These mirror the real LLM
            # output from the bda87a90 cache file.
            ExtractedRuling(
                extracted_case_number="21STCV23811",
                extracted_case_title="Charles Steven Sedlacek IV v. General Motors, LLC",
                ruling_text="J" * 263,
            ),
            ExtractedRuling(
                extracted_case_number="18STCV05920",
                extracted_case_title="Espinal, et al. v. Kia Motors America, Inc.",
                ruling_text="K" * 273,
            ),
            ExtractedRuling(
                extracted_case_number="22VECV01398",
                extracted_case_title="Mayra Hernandez v. General Motors LLC",
                ruling_text="L" * 203,
            ),
            ExtractedRuling(
                extracted_case_number="21STCV39407",
                extracted_case_title="Eileen Vines v. American Honda Motor Company, Inc.",
                ruling_text="M" * 216,
            ),
        ]
        filtered = _filter_citation_artifacts(rulings)
        assert len(filtered) == 1
        assert filtered[0].extracted_case_number == "CIVSB2223971"
        assert filtered[0].extracted_case_title == "Armistead v. Ford"

    def test_all_clean_titles_passes_through(self) -> None:
        """A multi-case PDF with clean titles and long texts is untouched."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="CIVSB2600001",
                extracted_case_title="Smith v. Jones",
                ruling_text="X" * 500,
            ),
            ExtractedRuling(
                extracted_case_number="CIVSB2600002",
                extracted_case_title="Doe v. Acme",
                ruling_text="Y" * 500,
            ),
        ]
        filtered = _filter_citation_artifacts(rulings)
        assert len(filtered) == 2

    def test_empty_input(self) -> None:
        """Empty input returns empty list."""
        assert _filter_citation_artifacts([]) == []

    def test_fallback_when_all_filtered(self) -> None:
        """If every candidate is a citation artifact, keep the longest-text one.

        This prevents the filter from emptying out a document — if the signals
        are ambiguous, we would rather keep something than nothing. The
        longest-text candidate is the best heuristic: legitimate attorney-fee
        orders produce substantial ruling_text, so even if the title happens
        to match a citator pattern the longest text is most likely the real
        ruling.
        """
        rulings = [
            ExtractedRuling(
                extracted_case_title="Williams v. KMA BC722351",
                ruling_text="A" * 155,
            ),
            ExtractedRuling(
                extracted_case_title="Rahman v. FCA US LLC C.D. Cal. 2022",
                ruling_text="B" * 250,  # longest
            ),
            ExtractedRuling(
                extracted_case_title="Klingenberg v. KMA BC709888",
                ruling_text="C" * 194,
            ),
        ]
        filtered = _filter_citation_artifacts(rulings)
        assert len(filtered) == 1
        # The longest-text candidate is kept.
        assert filtered[0].ruling_text is not None
        assert filtered[0].ruling_text.startswith("B")

    def test_single_ruling_not_filtered(self) -> None:
        """A single-ruling document is never filtered to zero.

        Even if that single ruling matches the citation pattern, we keep it —
        filtering the only ruling from a document would emit zero rulings,
        which is treated as an orphan downstream.
        """
        rulings = [
            ExtractedRuling(
                extracted_case_title="Acme v. Widget BC123456",
                ruling_text="Short.",
            ),
        ]
        filtered = _filter_citation_artifacts(rulings)
        assert len(filtered) == 1

    def test_mixed_real_and_citations(self) -> None:
        """Mixed list: real cases kept, citation artifacts removed."""
        real = ExtractedRuling(
            extracted_case_number="CIVSB2600100",
            extracted_case_title="Davis v. Ford",
            ruling_text="Z" * 2000,
        )
        citation = ExtractedRuling(
            extracted_case_title="Other v. Corp BC999999",
            ruling_text="A" * 180,
        )
        rulings = [citation, real, citation]
        filtered = _filter_citation_artifacts(rulings)
        assert len(filtered) == 1
        assert filtered[0].extracted_case_number == "CIVSB2600100"

    def test_preserves_order(self) -> None:
        """Filter preserves original order of kept rulings."""
        r1 = ExtractedRuling(
            extracted_case_number="CIVSB1",
            extracted_case_title="A v. B",
            ruling_text="X" * 500,
        )
        r2 = ExtractedRuling(
            extracted_case_number="CIVSB2",
            extracted_case_title="C v. D",
            ruling_text="Y" * 500,
        )
        citation = ExtractedRuling(
            extracted_case_title="Z v. W BC111111",
            ruling_text="Q" * 100,
        )
        filtered = _filter_citation_artifacts([r1, citation, r2])
        assert [r.extracted_case_number for r in filtered] == ["CIVSB1", "CIVSB2"]


class TestExtractUsesCitationFilter:
    """Integration test: extract() applies the filter to its output."""

    def test_extract_filters_citations_from_llm_output(self, monkeypatch: object) -> None:  # noqa: ANN001
        """When the LLM returns a citation-heavy response, extract() filters it.

        Uses a stub provider response to drive a real extract() call through
        its full pipeline (including citation filtering).
        """
        from framework.llm_extractor import LlmExtractor
        from framework.llm_schema import ExtractionResult

        # Build a stub ExtractionResult that mirrors the Sedlacek bug output:
        # 1 real ruling + several citation artifacts.
        stub_result = ExtractionResult(
            rulings=[
                ExtractedRuling(
                    extracted_case_number="CIVSB2223971",
                    extracted_case_title="Armistead v. Ford",
                    ruling_text=(
                        "Before the Court is Plaintiffs' Motion for Attorneys' Fees. " * 60
                    ),
                ),
                ExtractedRuling(
                    extracted_case_number=None,
                    extracted_case_title="Williams v. KMA BC722351",
                    ruling_text="A" * 155,
                ),
                ExtractedRuling(
                    extracted_case_number=None,
                    extracted_case_title="Zargarian v. BMW C.D. Cal. Mar. 3, 2020",
                    ruling_text="B" * 200,
                ),
                ExtractedRuling(
                    extracted_case_number=None,
                    extracted_case_title="Guzman v. FCA US LLC 8:22-cv-01092DFM",
                    ruling_text="C" * 206,
                ),
            ]
        )

        def fake_call_api(
            self: object,  # noqa: ARG001
            chunk: str,  # noqa: ARG001
            *,
            metadata: dict[str, str] | None = None,  # noqa: ARG001
            usage: object,  # noqa: ARG001
            chunk_index: int = 0,  # noqa: ARG001
            system_prompt: str | None = None,  # noqa: ARG001
        ) -> list[ExtractionResult]:
            # Bypass the actual API + retry logic: return the stub directly.
            return [stub_result]

        # Use a provider stub so no API key is required.
        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._provider = "anthropic"
        extractor._model = "stub"
        extractor._client = None
        extractor._max_retries = 0
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 80_000
        extractor._cache = None
        extractor._bust_cache = False

        monkeypatch.setattr(
            LlmExtractor,
            "_extract_chunk_with_retry",
            fake_call_api,
        )

        rulings = extractor.extract("fake raw text")
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "CIVSB2223971"
