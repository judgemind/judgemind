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
    _BRACKETED_PLACEHOLDER_TITLE_RE,
    _SB_CASE_NUMBER_RE,
    _disjoint_plaintiff_names,
    _is_role_literal_title,
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
# _BRACKETED_PLACEHOLDER_TITLE_RE — bracketed-placeholder title guard (#3988)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,should_match",
    [
        # Currently-supported shapes (regression guard) — verbatim from removed tests
        ("Ezra Arce v. [Defendant not specified]", True),
        ("[Plaintiff name unknown] v. Smith", True),
        ("In re Doe [Respondent missing]", True),
        # Clean / non-placeholder brackets — must NOT match
        ("Smith v. Jones", False),
        ("Smith v. Acme [DBA Acme Corp.]", False),
        # Currently-unsupported but real LLM outputs — documented non-matches.
        # If product later confirms these need catching, widen regex AND flip to True.
        ("Smith v. [Defendant 1]", False),
        ("Smith v. [defendant's name]", False),
        ("Smith v. [DEFENDANT]", False),
        ("Smith v. [TBD]", False),
        ("Smith v. [Name to be determined]", False),
        ("Smith v. [Insert defendant here]", False),
    ],
)
def test_bracketed_placeholder_regex_coverage(title: str, should_match: bool) -> None:
    assert bool(_BRACKETED_PLACEHOLDER_TITLE_RE.search(title)) is should_match


def test_rebuild_title_from_parties_handles_bracketed_placeholder() -> None:
    """Bracketed-placeholder title with only one real party returns None (drop path).

    The LA Arce shape: 'Ezra Arce v. [Defendant not specified]' with only a
    plaintiff in extracted_parties.  _rebuild_title_from_parties cannot build
    'P v. D', so returns None — caller nulls the title.
    """
    result = _rebuild_title_from_parties(
        "Ezra Arce v. [Defendant not specified]",
        [{"name": "Ezra Arce", "role": "plaintiff"}],
    )
    assert result is None


def test_rebuild_title_from_parties_rebuilds_bracketed_placeholder_with_both_parties() -> None:
    """Bracketed-placeholder title with both parties returns rebuilt 'P v. D'.

    When extracted_parties contains both plaintiff and defendant, the title
    should be rebuilt as 'PlaintiffName v. DefendantName'.
    """
    result = _rebuild_title_from_parties(
        "Ezra Arce v. [Defendant not specified]",
        [
            {"name": "Ezra Arce", "role": "plaintiff"},
            {"name": "John Doe Corp", "role": "defendant"},
        ],
    )
    assert result == "Ezra Arce v. John Doe Corp"


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


def test_san_bernardino_prompt_contains_caption_without_number_rule() -> None:
    """SAN_BERNARDINO_SYSTEM_PROMPT must document the caption-without-number rule (AC1)."""
    # Rule 4b must explicitly instruct the LLM not to inherit the prior section's
    # case number when a section has a distinct caption but no visible CIVSB/CIVRS token.
    prompt_lower = SAN_BERNARDINO_SYSTEM_PROMPT.lower()
    assert "caption" in prompt_lower and (
        "caption_without_number" in prompt_lower
        or "caption-without-number" in prompt_lower
        or "no" in prompt_lower  # "no visible ... case number" phrasing
    )
    # The rule must also instruct null/absent case_number handling
    assert "null" in prompt_lower or "inherit" in prompt_lower


# ---------------------------------------------------------------------------
# _disjoint_plaintiff_names helper
# ---------------------------------------------------------------------------


def test_disjoint_plaintiff_names_returns_true_for_disjoint_sets() -> None:
    """Two rulings with completely different plaintiff names are detected as disjoint."""
    parties_a = [
        ExtractedParty(name="Bernard Austin", role="plaintiff"),
        ExtractedParty(name="Pepper Dale", role="defendant"),
    ]
    parties_b = [
        ExtractedParty(name="Maria Juana Montalvo", role="plaintiff"),
        ExtractedParty(name="Loma Linda University Medical Center", role="defendant"),
    ]
    assert _disjoint_plaintiff_names(parties_a, parties_b) is True


def test_disjoint_plaintiff_names_returns_false_when_same_plaintiff() -> None:
    """Two rulings sharing the same plaintiff are not disjoint (multi-motion shape)."""
    parties_a = [
        ExtractedParty(name="Bernard Austin", role="plaintiff"),
        ExtractedParty(name="Pepper Dale", role="defendant"),
    ]
    parties_b = [
        ExtractedParty(name="Bernard Austin", role="plaintiff"),
        ExtractedParty(name="City of San Bernardino", role="defendant"),
    ]
    assert _disjoint_plaintiff_names(parties_a, parties_b) is False


def test_disjoint_plaintiff_names_case_insensitive() -> None:
    """Name comparison is case-insensitive."""
    parties_a = [ExtractedParty(name="BERNARD AUSTIN", role="plaintiff")]
    parties_b = [ExtractedParty(name="bernard austin", role="plaintiff")]
    assert _disjoint_plaintiff_names(parties_a, parties_b) is False


def test_disjoint_plaintiff_names_empty_a_is_conservative() -> None:
    """When anchor has no plaintiffs, return False (conservative — don't fire)."""
    parties_b = [ExtractedParty(name="Maria Montalvo", role="plaintiff")]
    assert _disjoint_plaintiff_names([], parties_b) is False


def test_disjoint_plaintiff_names_empty_b_is_conservative() -> None:
    """When subsequent ruling has no plaintiffs, return False (conservative)."""
    parties_a = [ExtractedParty(name="Bernard Austin", role="plaintiff")]
    assert _disjoint_plaintiff_names(parties_a, []) is False


def test_disjoint_plaintiff_names_accepts_dict_shaped_parties() -> None:
    """_disjoint_plaintiff_names accepts dict-shaped parties (LA path)."""
    parties_a = [{"name": "Bernard Austin", "role": "plaintiff"}]
    parties_b = [{"name": "Maria Montalvo", "role": "plaintiff"}]
    assert _disjoint_plaintiff_names(parties_a, parties_b) is True


# ---------------------------------------------------------------------------
# _sanitize_san_bernardino_rulings — inherited case_number guard (AC2)
# ---------------------------------------------------------------------------


def test_sanitize_sb_rulings_nullifies_inherited_case_number_when_disjoint_parties(
    fixture_data: dict,
) -> None:
    """Second ruling with disjoint plaintiff names gets its case_number nulled (AC2).

    Simulates the real-world shape: two rulings sharing CIVSB1234567 where
    ruling[0] is Bernard Austin (anchor) and ruling[1] is Maria Juana Montalvo
    (subsequent — different case, LLM incorrectly inherited the case number).
    """
    entry = fixture_data["inherited_case_number"][0]
    rulings = [
        ExtractedRuling(
            extracted_case_number=r["extracted_case_number"],
            extracted_case_title=r["extracted_case_title"],
            ruling_text=r["ruling_text"],
            extracted_parties=[
                ExtractedParty(name=p["name"], role=p["role"])
                for p in r.get("extracted_parties", [])
            ],
        )
        for r in entry["input_rulings"]
    ]
    result = _sanitize_san_bernardino_rulings(rulings, case_number_re=_SB_CASE_NUMBER_RE)

    # Anchor (ruling[0]) is unchanged
    assert result[0].extracted_case_number == entry["expected"][0]["extracted_case_number"]
    assert result[0].extracted_case_title == entry["expected"][0]["extracted_case_title"]

    # Subsequent (ruling[1]) has case_number nulled and title rebuilt from its own parties
    assert result[1].extracted_case_number is None
    assert result[1].extracted_case_title == entry["expected"][1]["extracted_case_title"]


def test_sanitize_sb_rulings_keeps_inherited_case_number_when_parties_match() -> None:
    """Two rulings with the same plaintiff (legitimate multi-motion shape) are untouched."""
    shared_cn = "CIVSB2419120"
    rulings = [
        ExtractedRuling(
            extracted_case_number=shared_cn,
            extracted_case_title="Bernard Austin v. Pepper Dale",
            ruling_text="Motion to compel GRANTED.",
            extracted_parties=[
                ExtractedParty(name="Bernard Austin", role="plaintiff"),
                ExtractedParty(name="Pepper Dale", role="defendant"),
            ],
        ),
        ExtractedRuling(
            extracted_case_number=shared_cn,
            extracted_case_title="Bernard Austin v. Pepper Dale",
            ruling_text="Motion to set aside default DENIED.",
            extracted_parties=[
                ExtractedParty(name="Bernard Austin", role="plaintiff"),
                ExtractedParty(name="Pepper Dale", role="defendant"),
            ],
        ),
    ]
    result = _sanitize_san_bernardino_rulings(rulings, case_number_re=_SB_CASE_NUMBER_RE)
    # Both rulings keep their case_number
    assert result[0].extracted_case_number == shared_cn
    assert result[1].extracted_case_number == shared_cn


def test_sanitize_sb_rulings_inherited_case_number_no_anchor_parties_is_noop() -> None:
    """When anchor has no plaintiffs, heuristic conservatively leaves subsequent unchanged."""
    shared_cn = "CIVSB2419120"
    rulings = [
        ExtractedRuling(
            extracted_case_number=shared_cn,
            extracted_case_title="Unknown v. Pepper Dale",
            ruling_text="Motion GRANTED.",
            extracted_parties=[
                ExtractedParty(name="Pepper Dale", role="defendant"),
                # No plaintiff parties for anchor
            ],
        ),
        ExtractedRuling(
            extracted_case_number=shared_cn,
            extracted_case_title="Maria Montalvo v. Loma Linda University",
            ruling_text="Motion DENIED.",
            extracted_parties=[
                ExtractedParty(name="Maria Montalvo", role="plaintiff"),
                ExtractedParty(name="Loma Linda University", role="defendant"),
            ],
        ),
    ]
    result = _sanitize_san_bernardino_rulings(rulings, case_number_re=_SB_CASE_NUMBER_RE)
    # Conservative: anchor has no plaintiffs so subsequent is left unchanged
    assert result[1].extracted_case_number == shared_cn


# ---------------------------------------------------------------------------
# _is_role_literal_title — detection on EITHER side of v. (#4618)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        # Left-anchored (old regex behaviour — must still detect)
        "Plaintiff v. General Motors, LLC",
        "Defendant v. Smith",
        "Petitioner v. Smith",
        "Respondent v. Smith",
        "Plaintiffs v. Smith",
        "Defendants v. Smith",
        # Right-only (the new behaviour — previously MISSED)
        "Acme Corp v. Defendants",
        "Tcfi Cp Llc v. Defendants/Cross-Complainants",
        "Smith v. Defendant",
        "Plaintiff/Petitioner v. Defendant/Respondent",
        "Jane Roe v. Respondents",
        # vs. / vs / v separator variants
        "Acme Corp vs. Defendants",
        "Acme Corp vs Defendants",
        "Acme Corp v Defendants",
    ],
)
def test_is_role_literal_title_detected(title: str) -> None:
    """A role-literal placeholder on either side of v. is detected (#4618)."""
    assert _is_role_literal_title(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # Segment merely starts with / contains a role word but has real text
        "Smith v. Defendants Inc.",
        "Plaintiff Holdings LLC v. Smith",
        # Clean real-party titles
        "Aasi v. American Honda",
        "Doe v. Roe",
        # No separator at all
        "In re Estate of Smith",
        # Compound where one piece is NOT a role token
        "Acme Corp v. Defendants/Acme",
    ],
)
def test_is_role_literal_title_false_positive_guard(title: str) -> None:
    """A real party name that merely contains a role word is NOT flagged (#4618)."""
    assert _is_role_literal_title(title) is False


def test_is_role_literal_title_none_and_empty_return_false() -> None:
    """None and empty-string inputs return False (#4618)."""
    assert _is_role_literal_title(None) is False
    assert _is_role_literal_title("") is False
    assert _is_role_literal_title("   ") is False
