"""Tests for Riverside multi-case LLM contamination guards (#2564).

Two contamination patterns in Riverside tentative ruling PDFs:

1. **Title absorbs motion heading** — the LLM misreads the caption + motion
   header as one ``case_title``, e.g.
   ``"WILLARD VS HYUNDAI MOTOR AMERICA vs. MOTION FOR ATTORNEY'S FEES BY JAMES WILLARD"``
   should be sanitized to ``"WILLARD VS HYUNDAI MOTOR AMERICA"``.

2. **ruling_text bleeds across case boundary** — the LLM fails to stop at the
   next numbered-entry boundary, and the ruling for case N contains the full
   text for case N+1 as well.

All tests here FAIL on the current main branch (functions don't exist, prompt
doesn't contain guards) and PASS after the fix.  That satisfies AC#1.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from framework.llm_extractor import (
    _sanitize_riverside_rulings,
    _sanitize_title_cost_itemization_tail,
    _sanitize_title_motion_tail,
    _truncate_cross_case_ruling_text,
)
from framework.llm_schema import ExtractedRuling
from framework.prompts.riverside import RIVERSIDE_SYSTEM_PROMPT

FIXTURES = Path(__file__).parent / "fixtures" / "riv_multi_case_contaminated.json"

_RIVERSIDE_CASE_NUMBER_RE = re.compile(
    r"\b(?:CV[A-Z]{2,4}|(?:RIC|MCC|PSC|SWC|INC|CIV|MVC|TEC|UDPS)[A-Z]{0,4})\d{6,10}\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def fixture_data() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _sanitize_title_motion_tail — title contamination guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(
            {
                "input": (
                    "WILLARD VS HYUNDAI MOTOR AMERICA"
                    " vs. MOTION FOR ATTORNEY'S FEES BY JAMES WILLARD"
                ),
                "expected_title": "WILLARD VS HYUNDAI MOTOR AMERICA",
            },
            id="willard_attorney_fees",
        ),
        pytest.param(
            {
                "input": "JOHNSON vs. MOTION TO COMPEL",
                "expected_title": "JOHNSON",
            },
            id="johnson_motion_to_compel",
        ),
        pytest.param(
            {
                "input": "PEREZ VS CITY OF RIVERSIDE vs. Demurrer to First Amended Complaint",
                "expected_title": "PEREZ VS CITY OF RIVERSIDE",
            },
            id="perez_demurrer",
        ),
        pytest.param(
            {
                "input": "SMITH VS SOUTHWEST AIRLINES CO. vs. MSJ by DEFENDANT",
                "expected_title": "SMITH VS SOUTHWEST AIRLINES CO.",
            },
            id="smith_msj",
        ),
        pytest.param(
            {
                "input": (
                    "GARCIA VS COUNTY OF SAN BERNARDINO vs. Hearing on Motion to Set Aside Default"
                ),
                "expected_title": "GARCIA VS COUNTY OF SAN BERNARDINO",
            },
            id="garcia_hearing_on",
        ),
        pytest.param(
            {
                "input": "RODRIGUEZ VS TESLA INC vs. Motion to Be Relieved as Counsel",
                "expected_title": "RODRIGUEZ VS TESLA INC",
            },
            id="rodriguez_to_be_relieved",
        ),
    ],
)
def test_sanitize_title_strips_motion_tail(entry: dict) -> None:
    """Each contaminated title is cleaned to only the party-names caption."""
    result = _sanitize_title_motion_tail(entry["input"])
    assert result == entry["expected_title"]


def test_sanitize_title_preserves_clean_title() -> None:
    """A clean 'A v. B' title with no motion heading is returned unchanged."""
    clean = "Linton v. Joshua Linton"
    assert _sanitize_title_motion_tail(clean) == clean


# ---------------------------------------------------------------------------
# _truncate_cross_case_ruling_text — ruling_text bleed guard
# ---------------------------------------------------------------------------


def test_truncate_cross_case_ruling_text_drops_foreign_block(fixture_data: dict) -> None:
    """ruling_text that bleeds into a foreign case number is truncated at that boundary."""
    entry = fixture_data["cross_case_ruling_text"][0]
    result = _truncate_cross_case_ruling_text(
        entry["input_text"],
        own_case_number=entry["own_case_number"],
        case_number_re=_RIVERSIDE_CASE_NUMBER_RE,
    )
    assert result == entry["expected_truncated_text"]


def test_truncate_cross_case_ruling_text_keeps_own_case_number_mentions() -> None:
    """ruling_text that mentions the ruling's own case number inline is not truncated."""
    own = "CVRI2500796"
    text = (
        "Case CVRI2500796: The motion is denied.\n\n"
        "Plaintiff in CVRI2500796 has standing to bring this claim."
    )
    result = _truncate_cross_case_ruling_text(
        text,
        own_case_number=own,
        case_number_re=_RIVERSIDE_CASE_NUMBER_RE,
    )
    assert result == text


def test_truncate_cross_case_ruling_text_no_sibling_case_number_is_noop() -> None:
    """ruling_text with no Riverside case number at all is returned unchanged."""
    text = (
        "Tentative Ruling: GRANT Plaintiff's motion to compel.\n\n"
        "Defendant failed to respond within the statutory deadline.\n"
        "The motion is GRANTED. Defendant shall serve responses within 20 days."
    )
    result = _truncate_cross_case_ruling_text(
        text,
        own_case_number="CVRI2500796",
        case_number_re=_RIVERSIDE_CASE_NUMBER_RE,
    )
    assert result == text


# ---------------------------------------------------------------------------
# _sanitize_riverside_rulings — orchestrator covers logger + model_copy paths
# ---------------------------------------------------------------------------


def test_sanitize_riverside_rulings_cleans_contaminated_title() -> None:
    """_sanitize_riverside_rulings applies title sanitizer and calls model_copy (#2564)."""
    ruling = ExtractedRuling(
        extracted_case_number="CVRI2105192",
        extracted_case_title=(
            "WILLARD VS HYUNDAI MOTOR AMERICA vs. MOTION FOR ATTORNEY'S FEES BY JAMES WILLARD"
        ),
        ruling_text="Tentative Ruling: GRANT.",
    )
    result = _sanitize_riverside_rulings([ruling], case_number_re=_RIVERSIDE_CASE_NUMBER_RE)
    assert result[0].extracted_case_title == "WILLARD VS HYUNDAI MOTOR AMERICA"
    assert result[0].ruling_text == "Tentative Ruling: GRANT."


def test_sanitize_riverside_rulings_truncates_cross_case_ruling_text() -> None:
    """_sanitize_riverside_rulings truncates ruling_text at a foreign case number (#2564)."""
    text = (
        "Tentative Ruling: DENY.\n\n"
        "Analysis: The motion lacks merit.\n\n"
        "2.\nCVRI2500736\nSERNA VS JOHNSON\nTentative: GRANT."
    )
    ruling = ExtractedRuling(
        extracted_case_number="CVRI2500796",
        extracted_case_title="Linton v. Joshua Linton",
        ruling_text=text,
    )
    result = _sanitize_riverside_rulings([ruling], case_number_re=_RIVERSIDE_CASE_NUMBER_RE)
    assert "CVRI2500736" not in result[0].ruling_text
    assert result[0].ruling_text.startswith("Tentative Ruling: DENY.")


# ---------------------------------------------------------------------------
# RIVERSIDE_SYSTEM_PROMPT — prompt guard tests
# ---------------------------------------------------------------------------


def test_riverside_prompt_contains_motion_tail_guard() -> None:
    """RIVERSIDE_SYSTEM_PROMPT must contain the motion-tail guard in the case_title rules."""
    # The new Rule 4a must mention at least one motion keyword to guard
    # against the LLM absorbing motion headings into case_title.
    assert "Motion" in RIVERSIDE_SYSTEM_PROMPT


def test_riverside_prompt_contains_cross_case_guard() -> None:
    """RIVERSIDE_SYSTEM_PROMPT must contain wording about cross-case boundary stopping."""
    # Rule 5a must instruct the LLM to stop ruling_text at the next numbered
    # entry AND forbid foreign case numbers in the ruling text.
    prompt_lower = RIVERSIDE_SYSTEM_PROMPT.lower()
    assert (
        "foreign" in prompt_lower or "next numbered" in prompt_lower or "next entry" in prompt_lower
    )


# ---------------------------------------------------------------------------
# _sanitize_title_cost_itemization_tail — cost-itemization title guard (#3555)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(
            {
                "input": (
                    "VELASQUEZ vs MONTENEGRO "
                    "Court Reporter Fees Interpreter Fees Models, Enlargements "
                    "and Photocopies of Exhibits"
                ),
                "expected_title": "VELASQUEZ vs MONTENEGRO",
            },
            id="velasquez_full_cost_itemization",
        ),
        pytest.param(
            {
                "input": "SMITH vs JONES Interpreter Fees",
                "expected_title": "SMITH vs JONES",
            },
            id="smith_interpreter_fees",
        ),
        pytest.param(
            {
                "input": "GARCIA vs HERNANDEZ Photocopies of Exhibits",
                "expected_title": "GARCIA vs HERNANDEZ",
            },
            id="garcia_photocopies",
        ),
        # Negative cases — clean titles must be returned unchanged
        pytest.param(
            {
                "input": "VELASQUEZ vs MONTENEGRO",
                "expected_title": "VELASQUEZ vs MONTENEGRO",
            },
            id="clean_caption_unchanged",
        ),
        pytest.param(
            {
                "input": "ACME COURT REPORTING SERVICES INC vs JONES",
                "expected_title": "ACME COURT REPORTING SERVICES INC vs JONES",
            },
            id="court_reporting_in_plaintiff_name_preserved",
        ),
        pytest.param(
            {
                "input": "SMITH vs COUNTY COURT REPORTING SERVICES INC",
                "expected_title": "SMITH vs COUNTY COURT REPORTING SERVICES INC",
            },
            id="court_reporting_in_defendant_name_preserved",
        ),
        pytest.param(
            {
                "input": "JONES vs FEES INC",
                "expected_title": "JONES vs FEES INC",
            },
            id="fees_in_defendant_company_name_preserved",
        ),
        pytest.param(
            {
                "input": "JONES vs ATTORNEY FEES LLC",
                "expected_title": "JONES vs ATTORNEY FEES LLC",
            },
            id="attorney_fees_in_defendant_name_preserved",
        ),
        pytest.param(
            {
                "input": "SMITH vs COSTS RECOVERY CORP",
                "expected_title": "SMITH vs COSTS RECOVERY CORP",
            },
            id="costs_in_defendant_name_preserved",
        ),
    ],
)
def test_sanitize_title_strips_cost_itemization_tail(entry: dict) -> None:
    """Cost-itemization tails are stripped; legitimate party names are preserved."""
    result = _sanitize_title_cost_itemization_tail(entry["input"])
    assert result == entry["expected_title"]


# ---------------------------------------------------------------------------
# RIVERSIDE_SYSTEM_PROMPT — new prompt guard tests (#3555)
# ---------------------------------------------------------------------------


def test_riverside_prompt_contains_motion_type_outcome_boundary_guard() -> None:
    """RIVERSIDE_SYSTEM_PROMPT must contain per-entry motion_type/outcome boundary rule (5b)."""
    # Rule 5b must instruct the LLM never to carry forward motion_type / outcome
    # from a previous entry.
    assert "motion_type" in RIVERSIDE_SYSTEM_PROMPT
    assert "outcome" in RIVERSIDE_SYSTEM_PROMPT
    prompt_lower = RIVERSIDE_SYSTEM_PROMPT.lower()
    assert (
        "never carry forward" in prompt_lower
        or "carry forward" in prompt_lower
        or "only from that entry" in prompt_lower
        or "derived only from" in prompt_lower
    )


def test_riverside_prompt_contains_cost_itemization_stop_tokens() -> None:
    """RIVERSIDE_SYSTEM_PROMPT must reference cost-itemization stop tokens in rule 4a."""
    assert "Court Reporter" in RIVERSIDE_SYSTEM_PROMPT
    assert "Interpreter Fees" in RIVERSIDE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _sanitize_title_cost_itemization_tail — edge case: no adversarial separator
# ---------------------------------------------------------------------------


def test_sanitize_title_cost_itemization_tail_no_separator_returns_unchanged() -> None:
    """A title with no v./vs. separator is returned unchanged even if it has cost words."""
    # This exercises the early-return guard (line 939) — no adversarial
    # separator means the function must not strip anything.
    title = "Costs"
    result = _sanitize_title_cost_itemization_tail(title)
    assert result == title


# ---------------------------------------------------------------------------
# _sanitize_riverside_rulings — cost-itemization tail path via orchestrator
# ---------------------------------------------------------------------------


def test_sanitize_riverside_rulings_strips_cost_itemization_tail() -> None:
    """_sanitize_riverside_rulings strips cost-itemization tail and logs the change (#3555)."""
    ruling = ExtractedRuling(
        extracted_case_number="CVRI2500001",
        extracted_case_title=(
            "VELASQUEZ vs MONTENEGRO "
            "Court Reporter Fees Interpreter Fees Models, Enlargements "
            "and Photocopies of Exhibits"
        ),
        ruling_text="Tentative Ruling: GRANT motion to tax costs.",
    )
    result = _sanitize_riverside_rulings([ruling], case_number_re=_RIVERSIDE_CASE_NUMBER_RE)
    assert result[0].extracted_case_title == "VELASQUEZ vs MONTENEGRO"
    # ruling_text should be unchanged (no cross-case contamination)
    assert result[0].ruling_text == "Tentative Ruling: GRANT motion to tax costs."
