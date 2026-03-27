"""Tests for ingestion.ruling_guards — shared multi-ruling conversion logic.

Verifies the ruling_text cross-contamination guard, document ID splitting,
parties conversion, outcome mapping, and motion_type normalization.  Also
includes a parameterized parity test confirming that worker.py and
reingest_from_s3.py produce identical ruling_text values for the same inputs
when both use the shared function.

See #2084 for background.
"""

from __future__ import annotations

import pytest

from framework.llm_schema import (
    ExtractedParty,
    ExtractedRuling,
    ExtractionCaseType,
    ExtractionOutcome,
)
from ingestion.ruling_guards import convert_extracted_rulings
from ingestion.split_ids import make_split_document_id

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DOC_ID = "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"

_RULING_WITH_TEXT = ExtractedRuling(
    extracted_case_number="24STCV01234",
    extracted_case_title="Smith v. Jones",
    extracted_judge_name="Hon. Jane Doe",
    department="Dept 11",
    motion_type="Motion for Summary Judgment",
    outcome=ExtractionOutcome.GRANTED,
    ruling_text="The motion is GRANTED.",
    hearing_date="2026-03-15",
    extracted_parties=[
        ExtractedParty(name="Smith", role="plaintiff"),
        ExtractedParty(name="Jones", role="defendant"),
    ],
    case_type=ExtractionCaseType.CIVIL,
)

_RULING_WITHOUT_TEXT = ExtractedRuling(
    extracted_case_number="24STCV05678",
    extracted_case_title="Doe v. Roe",
    outcome=ExtractionOutcome.DENIED,
    ruling_text=None,
)

_RULING_EMPTY_TEXT = ExtractedRuling(
    extracted_case_number="24STCV09999",
    extracted_case_title="Alpha v. Beta",
    outcome=ExtractionOutcome.MOOT,
    ruling_text="",
)


# ---------------------------------------------------------------------------
# Single-ruling tests
# ---------------------------------------------------------------------------


class TestSingleRuling:
    """Tests for single-ruling documents (is_multi=False)."""

    def test_ruling_text_preserved_when_present(self) -> None:
        """When ruling.ruling_text has content, it should be used directly."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT],
            _DOC_ID,
            fallback_text="full document text",
        )
        assert len(results) == 1
        assert results[0].ruling_text == "The motion is GRANTED."

    def test_ruling_text_falls_back_to_fallback_when_none(self) -> None:
        """For single-ruling docs, None ruling_text falls back to fallback_text."""
        results = convert_extracted_rulings(
            [_RULING_WITHOUT_TEXT],
            _DOC_ID,
            fallback_text="full document text",
        )
        assert len(results) == 1
        assert results[0].ruling_text == "full document text"

    def test_ruling_text_falls_back_to_fallback_when_empty(self) -> None:
        """For single-ruling docs, empty ruling_text falls back to fallback_text."""
        results = convert_extracted_rulings(
            [_RULING_EMPTY_TEXT],
            _DOC_ID,
            fallback_text="full document text",
        )
        assert len(results) == 1
        assert results[0].ruling_text == "full document text"

    def test_ruling_text_none_when_no_fallback(self) -> None:
        """If no fallback_text and ruling_text is None, result is None."""
        results = convert_extracted_rulings(
            [_RULING_WITHOUT_TEXT],
            _DOC_ID,
        )
        assert len(results) == 1
        assert results[0].ruling_text is None

    def test_document_id_is_original(self) -> None:
        """Single-ruling docs keep the original document ID."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].document_id == _DOC_ID

    def test_is_multi_false(self) -> None:
        """Single-ruling docs have is_multi=False."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].is_multi is False

    def test_split_count_one(self) -> None:
        """Single-ruling docs have split_count=1."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].split_count == 1
        assert results[0].split_index == 0


# ---------------------------------------------------------------------------
# Multi-ruling tests (cross-contamination guard)
# ---------------------------------------------------------------------------


class TestMultiRuling:
    """Tests for multi-ruling documents (is_multi=True)."""

    def test_ruling_text_none_when_missing_prevents_contamination(self) -> None:
        """Multi-ruling: None ruling_text must NOT fall back to document text."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT],
            _DOC_ID,
            fallback_text="FULL DOCUMENT TEXT - should never appear",
        )
        assert len(results) == 2
        # First ruling has its own text
        assert results[0].ruling_text == "The motion is GRANTED."
        # Second ruling has None (NOT the fallback text)
        assert results[1].ruling_text is None

    def test_ruling_text_none_when_empty_prevents_contamination(self) -> None:
        """Multi-ruling: empty ruling_text must NOT fall back to document text."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_EMPTY_TEXT],
            _DOC_ID,
            fallback_text="FULL DOCUMENT TEXT - should never appear",
        )
        assert results[1].ruling_text is None

    def test_ruling_text_preserved_when_present(self) -> None:
        """Multi-ruling: ruling_text is preserved when it has content."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_WITH_TEXT],
            _DOC_ID,
        )
        assert results[0].ruling_text == "The motion is GRANTED."
        assert results[1].ruling_text == "The motion is GRANTED."

    def test_split_document_ids(self) -> None:
        """Multi-ruling: each ruling gets a deterministic split document ID."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT],
            _DOC_ID,
        )
        assert results[0].document_id == make_split_document_id(_DOC_ID, 0)
        assert results[1].document_id == make_split_document_id(_DOC_ID, 1)

    def test_original_document_id_preserved(self) -> None:
        """Multi-ruling: original_document_id is the parent document ID."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT],
            _DOC_ID,
        )
        for r in results:
            assert r.original_document_id == _DOC_ID

    def test_is_multi_true(self) -> None:
        """Multi-ruling docs have is_multi=True."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT],
            _DOC_ID,
        )
        for r in results:
            assert r.is_multi is True

    def test_split_count_and_indices(self) -> None:
        """Multi-ruling: split_count and split_index are correct."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT, _RULING_EMPTY_TEXT],
            _DOC_ID,
        )
        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.split_count == 3
            assert r.split_index == i


# ---------------------------------------------------------------------------
# Parties conversion
# ---------------------------------------------------------------------------


class TestPartiesConversion:
    """Tests for ExtractedParty -> dict conversion."""

    def test_parties_converted_to_dicts(self) -> None:
        """ExtractedParty objects are converted to name/role dicts."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].parties == [
            {"name": "Smith", "role": "plaintiff"},
            {"name": "Jones", "role": "defendant"},
        ]

    def test_empty_parties_becomes_empty_list(self) -> None:
        """Rulings with no parties produce an empty list."""
        results = convert_extracted_rulings([_RULING_WITHOUT_TEXT], _DOC_ID)
        assert results[0].parties == []


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------


class TestOutcomeMapping:
    """Tests for ExtractionOutcome enum -> string mapping."""

    def test_outcome_mapped_to_string(self) -> None:
        """Outcome enum is converted to its string value."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].outcome == "granted"

    def test_none_outcome_stays_none(self) -> None:
        """None outcome stays None."""
        ruling = ExtractedRuling(ruling_text="some text")
        results = convert_extracted_rulings([ruling], _DOC_ID)
        assert results[0].outcome is None


# ---------------------------------------------------------------------------
# Motion type normalization
# ---------------------------------------------------------------------------


class TestMotionTypeNormalization:
    """Tests for motion_type normalization."""

    def test_normalization_disabled_by_default(self) -> None:
        """Without normalize_motion=True, motion_type is passed through."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].motion_type == "Motion for Summary Judgment"

    def test_normalization_enabled(self) -> None:
        """With normalize_motion=True, motion_type is normalized."""
        results = convert_extracted_rulings(
            [_RULING_WITH_TEXT],
            _DOC_ID,
            normalize_motion=True,
        )
        # "Motion for Summary Judgment" should normalize to "msj"
        assert results[0].motion_type == "msj"

    def test_normalization_returns_none_for_unknown(self) -> None:
        """Unrecognized motion_type normalizes to None."""
        ruling = ExtractedRuling(
            ruling_text="some text",
            motion_type="Completely Unknown Type XYZ123",
        )
        results = convert_extracted_rulings(
            [ruling],
            _DOC_ID,
            normalize_motion=True,
        )
        assert results[0].motion_type is None

    def test_none_motion_type_stays_none(self) -> None:
        """None motion_type stays None regardless of normalize_motion."""
        ruling = ExtractedRuling(ruling_text="some text")
        results = convert_extracted_rulings(
            [ruling],
            _DOC_ID,
            normalize_motion=True,
        )
        assert results[0].motion_type is None


# ---------------------------------------------------------------------------
# Field passthrough
# ---------------------------------------------------------------------------


class TestFieldPassthrough:
    """Tests for case_number, case_title, judge_name, etc."""

    def test_fields_passed_through(self) -> None:
        """All extraction fields are passed through to the result."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        r = results[0]
        assert r.case_number == "24STCV01234"
        assert r.case_title == "Smith v. Jones"
        assert r.judge_name == "Hon. Jane Doe"
        assert r.department == "Dept 11"
        assert r.hearing_date == "2026-03-15"
        assert r.case_type == "civil"

    def test_none_fields_stay_none(self) -> None:
        """None fields are passed through as None."""
        results = convert_extracted_rulings([_RULING_WITHOUT_TEXT], _DOC_ID)
        r = results[0]
        assert r.judge_name is None
        assert r.department is None
        assert r.hearing_date is None
        assert r.case_type is None

    def test_case_type_enum_converted_to_string(self) -> None:
        """case_type ExtractionCaseType enum is converted to its value string."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        assert results[0].case_type == "civil"
        assert isinstance(results[0].case_type, str)


# ---------------------------------------------------------------------------
# ConvertedRuling is a frozen dataclass
# ---------------------------------------------------------------------------


class TestConvertedRulingImmutability:
    """Verify ConvertedRuling is immutable."""

    def test_frozen(self) -> None:
        """ConvertedRuling should be frozen (immutable)."""
        results = convert_extracted_rulings([_RULING_WITH_TEXT], _DOC_ID)
        r = results[0]
        with pytest.raises(AttributeError):
            r.ruling_text = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Parity: worker and reingest produce identical ruling_text
# ---------------------------------------------------------------------------


class TestWorkerReingestParity:
    """Parameterized parity test: worker and reingest use the shared function
    and produce identical ruling_text values for the same inputs.

    This test exercises convert_extracted_rulings with the exact parameters
    each caller would pass, confirming the cross-contamination guard
    produces identical ruling_text results.
    """

    @pytest.mark.parametrize(
        "description,extracted_rulings,worker_fallback,reingest_fallback",
        [
            (
                "single ruling with text",
                [_RULING_WITH_TEXT],
                "full doc text for worker",
                "",
            ),
            (
                "single ruling without text",
                [_RULING_WITHOUT_TEXT],
                "full doc text for worker",
                "",
            ),
            (
                "multi ruling, first has text, second missing",
                [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT],
                "full doc text for worker",
                "",
            ),
            (
                "multi ruling, both have text",
                [_RULING_WITH_TEXT, _RULING_WITH_TEXT],
                "full doc text for worker",
                "",
            ),
            (
                "multi ruling, none have text",
                [_RULING_WITHOUT_TEXT, _RULING_EMPTY_TEXT],
                "full doc text for worker",
                "",
            ),
            (
                "three rulings, mixed text",
                [_RULING_WITH_TEXT, _RULING_WITHOUT_TEXT, _RULING_EMPTY_TEXT],
                "full doc text for worker",
                "",
            ),
        ],
        ids=[
            "single-with-text",
            "single-without-text",
            "multi-first-has-text",
            "multi-both-have-text",
            "multi-none-have-text",
            "three-mixed",
        ],
    )
    def test_ruling_text_guard_parity(
        self,
        description: str,
        extracted_rulings: list[ExtractedRuling],
        worker_fallback: str,
        reingest_fallback: str,
    ) -> None:
        """Both paths produce the same ruling_text for multi-ruling docs.

        For multi-ruling documents, the ruling_text guard must behave
        identically: None ruling_text must never fall back to the full
        document text, regardless of what fallback_text the caller passes.
        """
        worker_results = convert_extracted_rulings(
            extracted_rulings,
            _DOC_ID,
            fallback_text=worker_fallback,
        )
        reingest_results = convert_extracted_rulings(
            extracted_rulings,
            _DOC_ID,
            fallback_text=reingest_fallback,
        )

        is_multi = len(extracted_rulings) > 1

        for i, (w, r) in enumerate(zip(worker_results, reingest_results)):
            if is_multi:
                # For multi-ruling docs, ruling_text must be identical
                # (either both have the ruling's own text or both are None)
                assert w.ruling_text == r.ruling_text, (
                    f"Parity mismatch in '{description}', ruling {i}: "
                    f"worker={w.ruling_text!r}, reingest={r.ruling_text!r}"
                )
            # For single-ruling docs, fallback differs by design
            # (worker passes full text, reingest passes "")

            # All other fields must match regardless
            assert w.document_id == r.document_id
            assert w.original_document_id == r.original_document_id
            assert w.split_index == r.split_index
            assert w.split_count == r.split_count
            assert w.is_multi == r.is_multi
            assert w.case_number == r.case_number
            assert w.case_title == r.case_title
            assert w.judge_name == r.judge_name
            assert w.department == r.department
            assert w.outcome == r.outcome
            assert w.hearing_date == r.hearing_date
            assert w.parties == r.parties


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_rulings_list(self) -> None:
        """Empty input returns empty output."""
        results = convert_extracted_rulings([], _DOC_ID)
        assert results == []

    def test_single_ruling_with_empty_string_fallback(self) -> None:
        """Single ruling with empty string fallback returns the empty string."""
        results = convert_extracted_rulings(
            [_RULING_WITHOUT_TEXT],
            _DOC_ID,
            fallback_text="",
        )
        # Empty string is falsy, so it should still be used as fallback
        # because we explicitly passed it. But since ruling_text is None
        # and "" is falsy, the guard should return fallback_text which is "".
        assert results[0].ruling_text == ""
