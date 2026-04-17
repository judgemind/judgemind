"""Tests for framework.la_parser_utils — shared LA parser regex patterns.

Verifies that the centralized regex patterns and constants match the
behavior expected by the LA tentative rulings scraper and backfill scripts.
See #1465 for deduplication rationale.
"""

from __future__ import annotations

from framework.la_parser_utils import (
    BARE_ROLE_LABELS,
    CASE_NAME_FIELD_RE,
    CASE_PARTIES_RE,
    D_ROLE_INLINE_RE,
    D_ROLE_RE,
    ENTITY_DESCRIPTOR_RE,
    MAX_PARTY_NAME_LENGTH,
    MOVING_PARTY_RE,
    P_ROLE_INLINE_RE,
    P_ROLE_RE,
    RESPONDING_PARTY_RE,
    ROLE_MAP,
    ROLE_PREFIX_RE,
    SKIP_RESPONDING_PHRASES,
    VS_RE,
)

# ---------------------------------------------------------------------------
# ROLE_PREFIX_RE tests
# ---------------------------------------------------------------------------


class TestRolePrefixRe:
    """Tests for the ROLE_PREFIX_RE pattern."""

    def test_strips_defendant_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Defendant Acme Corp")
        assert result == "Acme Corp"

    def test_strips_defendants_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Defendants Acme Corp")
        assert result == "Acme Corp"

    def test_strips_plaintiff_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Plaintiff John Smith")
        assert result == "John Smith"

    def test_strips_plaintiffs_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Plaintiffs David K and Claudia L")
        assert result == "David K and Claudia L"

    def test_strips_petitioner_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Petitioner Jane Doe")
        assert result == "Jane Doe"

    def test_strips_respondent_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Respondent State of CA")
        assert result == "State of CA"

    def test_strips_cross_complainant_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Cross-Complainant Widget Corp")
        assert result == "Widget Corp"

    def test_strips_cross_defendant_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Cross-Defendant Alpha LLC")
        assert result == "Alpha LLC"

    def test_comma_separator(self) -> None:
        """Role prefix followed by comma+space should also be stripped (#1425)."""
        result = ROLE_PREFIX_RE.sub("", "Defendant, Acme Corp")
        assert result == "Acme Corp"

    def test_no_match_without_prefix(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "Acme Corp")
        assert result == "Acme Corp"

    def test_case_insensitive(self) -> None:
        result = ROLE_PREFIX_RE.sub("", "DEFENDANT Acme Corp")
        assert result == "Acme Corp"


# ---------------------------------------------------------------------------
# MOVING_PARTY_RE / RESPONDING_PARTY_RE tests
# ---------------------------------------------------------------------------


class TestMovingRespondingRe:
    """Tests for MOVING_PARTY_RE and RESPONDING_PARTY_RE patterns."""

    def test_moving_party_basic(self) -> None:
        text = "MOVING PARTY: Defendant Acme Corporation."
        m = MOVING_PARTY_RE.search(text)
        assert m is not None
        assert m.group("name") == "Defendant Acme Corporation"

    def test_moving_parties_plural(self) -> None:
        text = "MOVING PARTIES: Defendants Alpha and Beta."
        m = MOVING_PARTY_RE.search(text)
        assert m is not None
        assert "Alpha and Beta" in m.group("name")

    def test_responding_party_basic(self) -> None:
        text = "RESPONDING PARTY: Plaintiff John Smith."
        m = RESPONDING_PARTY_RE.search(text)
        assert m is not None
        assert m.group("name") == "Plaintiff John Smith"

    def test_opposing_party(self) -> None:
        text = "OPPOSING PARTY: Plaintiff Small LLC."
        m = RESPONDING_PARTY_RE.search(text)
        assert m is not None
        assert "Small LLC" in m.group("name")

    def test_no_match_on_unrelated_text(self) -> None:
        text = "The court grants the motion."
        assert MOVING_PARTY_RE.search(text) is None
        assert RESPONDING_PARTY_RE.search(text) is None


# ---------------------------------------------------------------------------
# CASE_PARTIES_RE tests
# ---------------------------------------------------------------------------


class TestCasePartiesRe:
    """Tests for the CASE_PARTIES_RE caption block pattern."""

    def test_standard_caption_block(self) -> None:
        text = (
            "SUMAYYA AASI, et al.,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "AMERICAN HONDA MOTOR CO., INC., et al.,\n"
            "  Defendant(s)."
        )
        m = CASE_PARTIES_RE.search(text)
        assert m is not None
        assert "AASI" in m.group("plaintiff")
        assert "HONDA" in m.group("defendant")
        assert m.group("p_role") == "Plaintiff"
        assert m.group("d_role") == "Defendant"

    def test_petitioner_respondent(self) -> None:
        text = (
            "MIC-BRY8, LLC,\n"
            "  Petitioner(s),\n"
            "  vs.\n"
            "CERTAIN STATUTORY INTERESTED PARTIES,\n"
            "  Respondent(s)."
        )
        m = CASE_PARTIES_RE.search(text)
        assert m is not None
        assert m.group("p_role") == "Petitioner"
        assert m.group("d_role") == "Respondent"


# ---------------------------------------------------------------------------
# Caption role marker tests
# ---------------------------------------------------------------------------


class TestCaptionRoleMarkers:
    """Tests for P_ROLE_RE, D_ROLE_RE, VS_RE, and inline variants."""

    def test_p_role_re_matches_start_of_line(self) -> None:
        text = "NAME,\nPlaintiff(s),\nvs."
        assert P_ROLE_RE.search(text) is not None

    def test_d_role_re_matches_start_of_line(self) -> None:
        text = "NAME,\nDefendant(s)."
        assert D_ROLE_RE.search(text) is not None

    def test_p_role_inline_re(self) -> None:
        text = "SMITH, Plaintiff(s), vs."
        assert P_ROLE_INLINE_RE.search(text) is not None

    def test_d_role_inline_re(self) -> None:
        text = "JONES, Defendant(s)."
        assert D_ROLE_INLINE_RE.search(text) is not None

    def test_vs_re_matches_vs_dot(self) -> None:
        assert VS_RE.search("vs.") is not None

    def test_vs_re_matches_v_dot(self) -> None:
        assert VS_RE.search("v.") is not None


# ---------------------------------------------------------------------------
# CASE_NAME_FIELD_RE tests
# ---------------------------------------------------------------------------


class TestCaseNameFieldRe:
    """Tests for the CASE_NAME_FIELD_RE pattern."""

    def test_case_name_field(self) -> None:
        text = "CASE NAME: Porsche v. Mikia CASE NUMBER: 25SMCV01132"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        assert "Porsche v. Mikia" in m.group("title")

    def test_case_title_field(self) -> None:
        text = "CASE TITLE: Smith v. Jones CASE NUMBER: 22SMCV01940"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        assert "Smith v. Jones" in m.group("title")

    # Regression tests for #2578 — FILED metadata boundary.  Some LA dept pages
    # (Dept 25 Mkrtchyan pattern) render CASE NAME on the same visual line as
    # a COMP/PET/FAC FILED metadata field — the old regex only stopped at
    # "CASE NUMBER" so it leaked the filing date into the title.

    def test_case_name_stops_at_comp_filed(self) -> None:
        text = "CASE NAME: Landaverde v. Meller    COMP. FILED: 07-03-25"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        title = m.group("title").strip()
        assert title == "Landaverde v. Meller"
        assert "FILED" not in title.upper()
        assert "07-03-25" not in title

    def test_case_name_stops_at_pet_filed(self) -> None:
        text = "CASE NAME: Smith v. Jones    PET. FILED: 11-26-25"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        title = m.group("title").strip()
        assert title == "Smith v. Jones"
        assert "FILED" not in title.upper()

    def test_case_name_stops_at_fac_filed(self) -> None:
        text = "CASE NAME: Acme Corp v. Beta Industries   FAC FILED: 02-15-25"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        title = m.group("title").strip()
        assert title == "Acme Corp v. Beta Industries"
        assert "FILED" not in title.upper()

    def test_case_name_stops_at_complaint_filed_longform(self) -> None:
        text = "CASE NAME: Doe v. Roe    COMPLAINT FILED: 01-01-25"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        title = m.group("title").strip()
        assert title == "Doe v. Roe"

    def test_case_name_stops_at_petition_filed_longform(self) -> None:
        text = "CASE NAME: In re Smith    PETITION FILED: 03-12-25"
        m = CASE_NAME_FIELD_RE.search(text)
        assert m is not None
        title = m.group("title").strip()
        assert title == "In re Smith"


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for shared constants."""

    def test_bare_role_labels_contains_all_roles(self) -> None:
        expected = {
            "defendant",
            "defendants",
            "plaintiff",
            "plaintiffs",
            "petitioner",
            "petitioners",
            "respondent",
            "respondents",
            "cross-complainant",
            "cross-complainants",
            "cross-defendant",
            "cross-defendants",
        }
        assert BARE_ROLE_LABELS == expected

    def test_role_map_keys(self) -> None:
        expected_keys = {
            "plaintiff",
            "petitioner",
            "cross-complainant",
            "defendant",
            "respondent",
            "cross-defendant",
        }
        assert set(ROLE_MAP.keys()) == expected_keys

    def test_max_party_name_length(self) -> None:
        assert MAX_PARTY_NAME_LENGTH == 500

    def test_skip_responding_phrases(self) -> None:
        assert "no opposition" in SKIP_RESPONDING_PHRASES
        assert "none" in SKIP_RESPONDING_PHRASES
        assert "no response" in SKIP_RESPONDING_PHRASES
        assert "unopposed" in SKIP_RESPONDING_PHRASES


# ---------------------------------------------------------------------------
# ENTITY_DESCRIPTOR_RE tests
# ---------------------------------------------------------------------------


class TestEntityDescriptorRe:
    """Tests for the ENTITY_DESCRIPTOR_RE pattern."""

    def test_strips_an_individual(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "John Smith, An Individual")
        assert result == "John Smith"

    def test_strips_california_corporation(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "Acme Corp, A California Corporation")
        assert result == "Acme Corp"

    def test_strips_delaware_llc(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "Widget Inc, A Delaware Limited Liability Company")
        assert result == "Widget Inc"

    def test_strips_individually_and_as(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "Jane Doe, Individually And As Guardian")
        assert result == "Jane Doe"

    def test_strips_as_trustee_of(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "Robert Chen, As Trustee Of The Chen Family Trust")
        assert result == "Robert Chen"

    def test_strips_form_unknown(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "ABC Corp, Form Unknown")
        assert result == "ABC Corp"

    def test_strips_does(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "Defendants, Does 1 To 100, Inclusive")
        assert result == "Defendants"

    def test_no_match_plain_name(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "John Smith")
        assert result == "John Smith"

    def test_case_insensitive(self) -> None:
        result = ENTITY_DESCRIPTOR_RE.sub("", "Acme Corp, a california corporation")
        assert result == "Acme Corp"
