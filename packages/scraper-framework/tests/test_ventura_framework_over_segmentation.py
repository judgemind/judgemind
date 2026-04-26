"""Regression tests for Ventura framework-prompt over-segmentation bug (#3493).

A Ventura Tentative Case Management Order (CMO) is a single-case document that
may contain many topic headings (NOTICE OF ASSIGNMENT, Arbitration, Settlement,
Phased Discovery, Mediation, Trial, etc.).  Before the prompt fix, the LLM
incorrectly split these internal headings into separate rulings, producing N
rows in ``derived.rulings`` from a single PDF that should produce 1.

This file verifies:
1. When the LLM (mocked) returns a single-ruling JSON for the CMO fixture,
   ``LlmExtractor.extract()`` produces exactly one ``ExtractedRuling``.
2. ``convert_extracted_rulings()`` converts that to exactly one
   ``ConvertedRuling`` row.
3. A documentation-style "bug shape" test shows that the old multi-ruling
   output (simulating the un-fixed prompt) WOULD produce N>1 rows via
   ``convert_extracted_rulings()``, proving the regression target.

Tests here use ``pytestmark = pytest.mark.regression`` following the
``test_san_bernardino_multi_case.py`` convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from framework.llm_extractor import LlmExtractor
from framework.llm_schema import ExtractedRuling, ExtractionOutcome, ExtractionResult
from framework.prompts.ventura import VENTURA_FRAMEWORK_PROMPT
from ingestion.ruling_guards import convert_extracted_rulings

pytestmark = pytest.mark.regression

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ventura_case_management_order.txt"


@pytest.fixture(scope="module")
def cmo_text() -> str:
    """Representative Ventura Case Management Order text with 10 topic headings."""
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _make_extractor_with_stub(
    stub_result: ExtractionResult, monkeypatch: pytest.MonkeyPatch
) -> LlmExtractor:
    """Build a LlmExtractor that bypasses the real API and returns stub_result."""

    def fake_call_api(
        self: object,  # noqa: ARG001
        chunk: str,  # noqa: ARG001
        *,
        metadata: dict | None = None,  # noqa: ARG001
        usage: object,  # noqa: ARG001
        chunk_index: int = 0,  # noqa: ARG001
        system_prompt: str | None = None,  # noqa: ARG001
    ) -> list[ExtractionResult]:
        return [stub_result]

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

    monkeypatch.setattr(LlmExtractor, "_extract_chunk_with_retry", fake_call_api)
    return extractor


# ---------------------------------------------------------------------------
# Prompt content sanity check
# ---------------------------------------------------------------------------


def test_ventura_framework_prompt_contains_single_ruling_rule() -> None:
    """VENTURA_FRAMEWORK_PROMPT must explicitly forbid topic-heading splits (#3493)."""
    assert "Each Ventura PDF is ONE ruling" in VENTURA_FRAMEWORK_PROMPT
    assert "Case Management Order" in VENTURA_FRAMEWORK_PROMPT
    # The prompt must enumerate the problematic topic headings.
    assert "Arbitration" in VENTURA_FRAMEWORK_PROMPT
    assert "Settlement" in VENTURA_FRAMEWORK_PROMPT
    assert "Mediation" in VENTURA_FRAMEWORK_PROMPT


# ---------------------------------------------------------------------------
# AC#2 — single-ruling assertion on the CMO fixture
# ---------------------------------------------------------------------------


def test_cmo_fixture_produces_single_extracted_ruling(
    cmo_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LlmExtractor must return exactly 1 ruling for a Ventura CMO (AC#2).

    The LLM (mocked) returns a single-ruling JSON representing the correct
    behaviour after the prompt fix.  We verify that the extractor's full
    post-processing pipeline (citation filter, riverside/SB sanitizers)
    does not inadvertently drop or duplicate the ruling.
    """
    single_ruling_response = ExtractionResult(
        extracted_judge_name="Patricia A. Murphy",
        hearing_date="2025-04-25",
        department="20",
        rulings=[
            ExtractedRuling(
                extracted_case_number="2025CUOE054221",
                extracted_case_title="Acme Corporation, Inc. v. Global Logistics, LLC",
                case_type=None,
                outcome=ExtractionOutcome.OTHER,
                motion_type="case_management_conference",
                ruling_text=cmo_text,
            )
        ],
    )

    extractor = _make_extractor_with_stub(single_ruling_response, monkeypatch)
    rulings = extractor.extract(cmo_text, system_prompt=VENTURA_FRAMEWORK_PROMPT)

    assert len(rulings) == 1, (
        f"Expected exactly 1 ruling from CMO fixture, got {len(rulings)}. "
        "This may indicate the post-processing pipeline incorrectly dropped the ruling."
    )
    assert rulings[0].extracted_case_number == "2025CUOE054221"


def test_cmo_fixture_single_ruling_converts_to_one_row(
    cmo_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """convert_extracted_rulings() must produce exactly 1 ConvertedRuling for the CMO (AC#2)."""
    single_ruling_response = ExtractionResult(
        extracted_judge_name="Patricia A. Murphy",
        hearing_date="2025-04-25",
        department="20",
        rulings=[
            ExtractedRuling(
                extracted_case_number="2025CUOE054221",
                extracted_case_title="Acme Corporation, Inc. v. Global Logistics, LLC",
                outcome=ExtractionOutcome.OTHER,
                motion_type="case_management_conference",
                ruling_text=cmo_text,
            )
        ],
    )

    extractor = _make_extractor_with_stub(single_ruling_response, monkeypatch)
    rulings = extractor.extract(cmo_text, system_prompt=VENTURA_FRAMEWORK_PROMPT)

    converted = convert_extracted_rulings(
        rulings,
        document_id="ventura/2025/04/25/2025CUOE054221.pdf",
        fallback_text=cmo_text,
    )

    assert len(converted) == 1, (
        f"Expected 1 ConvertedRuling from CMO, got {len(converted)}. "
        "Over-segmentation would produce N>1 here."
    )
    assert converted[0].is_multi is False
    assert converted[0].case_number == "2025CUOE054221"


# ---------------------------------------------------------------------------
# Bug-shape regression: old multi-ruling output WOULD produce N>1 rows
# ---------------------------------------------------------------------------


def test_old_multi_ruling_output_produces_n_rows() -> None:
    """Documentation test: the old (broken) multi-ruling output produces N>1 ConvertedRulings.

    This test proves the regression target: if the LLM had returned 12 entries
    (one per CMO topic heading, as it did before the prompt fix), then
    convert_extracted_rulings() would have produced 12 ConvertedRuling rows
    from a single Ventura PDF — each with outcome='other' and motion_type='other'.

    The test does NOT exercise the prompt — it directly constructs the
    broken LLM output and passes it to convert_extracted_rulings().
    """
    # Simulate the old broken LLM output: 12 entries for a single-case CMO,
    # each with outcome='other' and motion_type='other'.
    topic_headings = [
        "NOTICE OF ASSIGNMENT",
        "Arbitration",
        "Settlement",
        "Phased Discovery",
        "Class List Discovery",
        "Class Certification Motion",
        "Mediation",
        "Informal Discovery Conferences",
        "Stipulated Protective & ESI Orders",
        "Trial",
        "Final Status Conference",
        "General Orders",
    ]
    old_broken_rulings = [
        ExtractedRuling(
            extracted_case_number="2025CUOE054221" if i == 0 else None,
            extracted_case_title="Acme Corporation, Inc. v. Global Logistics, LLC",
            outcome=ExtractionOutcome.OTHER,
            motion_type="other",
            ruling_text=f"Section: {heading}\n\nSome ruling text about {heading}.",
        )
        for i, heading in enumerate(topic_headings)
    ]

    converted = convert_extracted_rulings(
        old_broken_rulings,
        document_id="ventura/2025/04/25/2025CUOE054221.pdf",
    )

    # The old (broken) output would produce 12 rows — one per topic heading.
    assert len(converted) == len(topic_headings), (
        f"Expected {len(topic_headings)} rows from broken multi-ruling output, "
        f"got {len(converted)}."
    )
    assert len(converted) > 1, "Regression target: old prompt produced N>1 rows per CMO."
    # All are marked is_multi=True (they came from a multi-ruling document).
    assert all(c.is_multi for c in converted)
