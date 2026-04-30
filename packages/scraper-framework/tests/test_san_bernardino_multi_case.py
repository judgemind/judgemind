"""Tests for San Bernardino multi-case LLM contamination guards (#2565).

Three contamination patterns in San Bernardino tentative ruling PDFs:

1. **Concatenated captions** — the LLM fuses adjacent case captions from a
   multi-case PDF into one ``case_title``, e.g.
   ``"Smith v. Jones Doe v. Roe"`` should be sanitized to ``"Smith v. Jones"``
   by the generic ``_truncate_concatenated_case_titles`` helper.

2. **ruling_text bleeds across case boundary** — the LLM fails to stop at
   the horizontal-rule / repeated-header boundary between two CIVSB/CIVRS
   cases, and the ruling for case N contains the ruling for case N+1 as well.

3. **Role-literal titles** — the LLM emits ``"Plaintiff v. Defendant"`` (or
   ``"Petitioner v. Respondent"``) as the ``case_title`` instead of the real
   party names from the ruling body.  Fixed by ``_rebuild_title_from_parties``
   using ``extracted_parties``.

All tests FAIL on the current main branch (functions don't exist, prompt
doesn't contain guards) and PASS after the fix.  That satisfies AC#1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.llm_extractor import (
    _SB_CASE_NUMBER_RE,
    _rebuild_title_from_parties,
    _sanitize_san_bernardino_rulings,
    _truncate_concatenated_case_titles,
    _truncate_cross_case_ruling_text,
)
from framework.llm_schema import ExtractedParty, ExtractedRuling
from framework.prompts.san_bernardino import SAN_BERNARDINO_SYSTEM_PROMPT

FIXTURES = Path(__file__).parent / "fixtures" / "sb_multi_case_contaminated.json"


@pytest.fixture(scope="module")
def fixture_data() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _truncate_concatenated_case_titles — concatenated SB caption guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(
            {
                "input": "Hernandez v. General Motors LLC Martinez v. Ford Motor Company",
                "expected_title": "Hernandez v. General Motors LLC",
            },
            id="hernandez_fused",
        ),
        pytest.param(
            {
                "input": "JOHNSON v. CITY OF SAN BERNARDINO et al. WILLIAMS v. RIVERSIDE COUNTY",
                "expected_title": "JOHNSON v. CITY OF SAN BERNARDINO et al.",
            },
            id="johnson_et_al",
        ),
        pytest.param(
            {
                "input": (
                    "Wong v. State Farm Mutual Automobile Insurance Company"
                    " Chen v. Allstate Insurance"
                ),
                "expected_title": "Wong v. State Farm Mutual Automobile Insurance Company",
            },
            id="wong_long_defendant",
        ),
        pytest.param(
            {
                "input": "Lopez v. Walmart Inc. Kim v. Target Corporation",
                "expected_title": "Lopez v. Walmart Inc.",
            },
            id="lopez_period_split",
        ),
    ],
)
def test_truncate_concatenated_title_strips_second_sb_caption(entry: dict) -> None:
    """Concatenated SB captions are truncated to the first caption only."""
    ruling = ExtractedRuling(
        extracted_case_number="CIVSB2505526",
        extracted_case_title=entry["input"],
        ruling_text="Tentative Ruling: GRANT.",
    )
    result = _truncate_concatenated_case_titles([ruling])
    assert result[0].extracted_case_title == entry["expected_title"]


def test_truncate_concatenated_title_preserves_clean_sb_title() -> None:
    """A clean 'A v. B' SB title with no second caption is returned unchanged."""
    clean = "Lorenzo Solis v. General Motors LLC"
    ruling = ExtractedRuling(
        extracted_case_number="CIVSB2419120",
        extracted_case_title=clean,
        ruling_text="Tentative Ruling: DENY.",
    )
    result = _truncate_concatenated_case_titles([ruling])
    assert result[0].extracted_case_title == clean


# ---------------------------------------------------------------------------
# _truncate_cross_case_ruling_text — SB ruling_text bleed guard
# ---------------------------------------------------------------------------


def test_truncate_cross_case_ruling_text_on_sb_numbers(fixture_data: dict) -> None:
    """ruling_text that bleeds into a foreign SB case number is truncated."""
    entry = fixture_data["cross_case_ruling_text"][0]
    result = _truncate_cross_case_ruling_text(
        entry["input_text"],
        own_case_number=entry["own_case_number"],
        case_number_re=_SB_CASE_NUMBER_RE,
    )
    assert result == entry["expected_truncated_text"]
    # Confirm the foreign case number is gone from the truncated text
    for sibling in entry["sibling_case_numbers"]:
        assert sibling not in result


def test_truncate_cross_case_ruling_text_keeps_own_sb_case_number() -> None:
    """ruling_text mentioning the ruling's own SB case number inline is not truncated."""
    own = "CIVSB2505526"
    text = (
        "Case CIVSB2505526: The motion is denied.\n\n"
        "Plaintiff in CIVSB2505526 has standing to bring this claim."
    )
    result = _truncate_cross_case_ruling_text(
        text,
        own_case_number=own,
        case_number_re=_SB_CASE_NUMBER_RE,
    )
    assert result == text


def test_truncate_cross_case_ruling_text_no_sibling_sb_case_is_noop() -> None:
    """ruling_text with no SB case number at all is returned unchanged."""
    text = (
        "Tentative Ruling: GRANT Plaintiff's motion to compel.\n\n"
        "Defendant failed to respond within the statutory deadline.\n"
        "The motion is GRANTED. Defendant shall serve responses within 20 days."
    )
    result = _truncate_cross_case_ruling_text(
        text,
        own_case_number="CIVSB2505526",
        case_number_re=_SB_CASE_NUMBER_RE,
    )
    assert result == text


# ---------------------------------------------------------------------------
# _rebuild_title_from_parties — role-literal title fix
# ---------------------------------------------------------------------------


def test_rebuild_role_literal_title_from_parties_plaintiff(fixture_data: dict) -> None:
    """'Plaintiff v. Defendant' is rebuilt from extracted_parties (CIVSB2322876)."""
    entry = fixture_data["role_literal_title"][0]
    parties = [ExtractedParty(name=p["name"], role=p["role"]) for p in entry["input_parties"]]
    result = _rebuild_title_from_parties(entry["input_title"], parties)
    assert result == entry["expected_title"]


def test_rebuild_role_literal_title_petitioner_respondent(fixture_data: dict) -> None:
    """'Petitioner v. Respondent' is rebuilt using petitioner/respondent parties."""
    entry = fixture_data["role_literal_title"][1]
    parties = [ExtractedParty(name=p["name"], role=p["role"]) for p in entry["input_parties"]]
    result = _rebuild_title_from_parties(entry["input_title"], parties)
    assert result == entry["expected_title"]


def test_rebuild_role_literal_title_plural_forms(fixture_data: dict) -> None:
    """Plural forms 'Plaintiffs v. Defendants' are also matched and rebuilt."""
    entry = fixture_data["role_literal_title"][2]
    parties = [ExtractedParty(name=p["name"], role=p["role"]) for p in entry["input_parties"]]
    result = _rebuild_title_from_parties(entry["input_title"], parties)
    assert result == entry["expected_title"]


def test_rebuild_role_literal_title_no_parties_returns_none(fixture_data: dict) -> None:
    """Role-literal title with no extracted_parties returns None (cannot rebuild)."""
    entry = fixture_data["role_literal_title"][3]
    result = _rebuild_title_from_parties(entry["input_title"], [])
    assert result is None


def test_rebuild_role_literal_title_clean_title_is_noop() -> None:
    """A real party-name title like 'Smith v. Jones' is not touched."""
    result = _rebuild_title_from_parties(
        "Smith v. Jones",
        [ExtractedParty(name="Smith", role="plaintiff")],
    )
    assert result is None  # pattern does not match — function returns None


def test_rebuild_role_literal_title_none_input() -> None:
    """None title input returns None."""
    result = _rebuild_title_from_parties(None, [])
    assert result is None


def test_rebuild_title_from_parties_accepts_dict_shaped_parties() -> None:
    """_rebuild_title_from_parties accepts dict-shaped parties (LA path) — #3749.

    LA uses list[dict[str, str]] rather than pydantic ExtractedParty instances.
    The dict-adapter branch must return the same rebuilt title as the getattr path.
    """
    parties = [
        {"name": "Sumayya Aasi", "role": "plaintiff"},
        {"name": "General Motors, LLC", "role": "defendant"},
    ]
    result = _rebuild_title_from_parties("Plaintiff v. General Motors, LLC", parties)
    assert result == "Sumayya Aasi v. General Motors, LLC"


# ---------------------------------------------------------------------------
# _sanitize_san_bernardino_rulings — orchestrator covers all paths
# ---------------------------------------------------------------------------


def test_sanitize_sb_rulings_fixes_cross_case_ruling_text() -> None:
    """_sanitize_san_bernardino_rulings truncates ruling_text at a foreign SB case number."""
    text = (
        "Tentative Ruling: DENY.\n\n"
        "Analysis: The motion lacks merit.\n\n"
        "____________________________\n"
        "CIVSB2421856\nDoe v. Roe\nTentative: GRANT."
    )
    ruling = ExtractedRuling(
        extracted_case_number="CIVSB2505526",
        extracted_case_title="Smith v. Jones",
        ruling_text=text,
    )
    result = _sanitize_san_bernardino_rulings([ruling], case_number_re=_SB_CASE_NUMBER_RE)
    assert "CIVSB2421856" not in result[0].ruling_text
    assert result[0].ruling_text.startswith("Tentative Ruling: DENY.")


def test_sanitize_sb_rulings_fixes_role_literal_title() -> None:
    """_sanitize_san_bernardino_rulings replaces role-literal titles with real names."""
    ruling = ExtractedRuling(
        extracted_case_number="CIVSB2322876",
        extracted_case_title="Plaintiff v. Defendant",
        ruling_text="Tentative Ruling: GRANT.",
        extracted_parties=[
            ExtractedParty(name="Lorenzo Solis", role="plaintiff"),
            ExtractedParty(name="General Motors LLC", role="defendant"),
        ],
    )
    result = _sanitize_san_bernardino_rulings([ruling], case_number_re=_SB_CASE_NUMBER_RE)
    assert result[0].extracted_case_title == "Lorenzo Solis v. General Motors LLC"


def test_sanitize_sb_rulings_fixes_merged_title_and_cross_case_ruling_text() -> None:
    """End-to-end: _sanitize_san_bernardino_rulings + _truncate_concatenated_case_titles.

    This test exercises both contamination patterns together and is the
    primary AC#1 verification: it FAILS on main (functions don't exist)
    and PASSES after the fix.
    """
    contaminated_text = (
        "Tentative Ruling: DENY.\n\n"
        "The motion is DENIED because Plaintiff has demonstrated a triable "
        "issue of material fact.\n\n"
        "____________________________\n"
        "CIVSB2421856\nDoe v. Roe\nTentative: GRANT Plaintiff's Motion."
    )
    ruling = ExtractedRuling(
        extracted_case_number="CIVSB2505526",
        # Concatenated caption — generic truncator should strip second caption.
        extracted_case_title="Smith v. Jones Doe v. Roe",
        ruling_text=contaminated_text,
        extracted_parties=[
            ExtractedParty(name="Smith", role="plaintiff"),
            ExtractedParty(name="Jones", role="defendant"),
        ],
    )
    # Apply the generic title truncator first (as the production pipeline does).
    rulings = _truncate_concatenated_case_titles([ruling])
    # Then apply the SB-specific sanitizer.
    rulings = _sanitize_san_bernardino_rulings(rulings, case_number_re=_SB_CASE_NUMBER_RE)

    assert rulings[0].extracted_case_title == "Smith v. Jones"
    assert "CIVSB2421856" not in rulings[0].ruling_text
    assert rulings[0].ruling_text.startswith("Tentative Ruling: DENY.")


def test_sanitize_sb_rulings_is_noop_for_non_sb_case_numbers() -> None:
    """_sanitize_san_bernardino_rulings is a no-op for non-SB case numbers."""
    text = "Tentative Ruling: GRANT.\n\nCVRI2500736\nThis would be a Riverside ruling."
    ruling = ExtractedRuling(
        extracted_case_number="CVRI2105192",  # Riverside, not SB
        extracted_case_title="Plaintiff v. Defendant",
        ruling_text=text,
    )
    result = _sanitize_san_bernardino_rulings([ruling], case_number_re=_SB_CASE_NUMBER_RE)
    # Title unchanged (CVRI case number → skipped by SB sanitizer)
    assert result[0].extracted_case_title == "Plaintiff v. Defendant"
    # ruling_text unchanged
    assert result[0].ruling_text == text


def test_sanitize_sb_rulings_role_literal_no_parties_unchanged() -> None:
    """Role-literal title with no extracted_parties is left unchanged (no crash)."""
    ruling = ExtractedRuling(
        extracted_case_number="CIVSB2525250",
        extracted_case_title="Plaintiff vs. Defendant",
        ruling_text="Tentative Ruling: GRANT.",
        extracted_parties=[],
    )
    result = _sanitize_san_bernardino_rulings([ruling], case_number_re=_SB_CASE_NUMBER_RE)
    # Cannot rebuild — title is left as-is
    assert result[0].extracted_case_title == "Plaintiff vs. Defendant"


# ---------------------------------------------------------------------------
# SAN_BERNARDINO_SYSTEM_PROMPT — prompt guard tests
# ---------------------------------------------------------------------------


def test_san_bernardino_prompt_contains_multi_case_guard() -> None:
    """SAN_BERNARDINO_SYSTEM_PROMPT must mention cross-case boundary stopping."""
    prompt_lower = SAN_BERNARDINO_SYSTEM_PROMPT.lower()
    assert "foreign" in prompt_lower or "next case" in prompt_lower or "boundary" in prompt_lower


def test_san_bernardino_prompt_forbids_role_literal_title() -> None:
    """SAN_BERNARDINO_SYSTEM_PROMPT must forbid role-literal party names."""
    # The new Rule 3b must mention role words to guard against the LLM
    # emitting 'Plaintiff' / 'Defendant' as the party name.
    assert "Plaintiff" in SAN_BERNARDINO_SYSTEM_PROMPT or "role" in SAN_BERNARDINO_SYSTEM_PROMPT
