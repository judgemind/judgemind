"""Tests for Contra Costa County LLM extraction prompt (#3609)."""

from __future__ import annotations

from framework.prompts.contra_costa import CONTRA_COSTA_SYSTEM_PROMPT


class TestContraCostaPromptFormatCCalendarListings:
    """Prompt structural assertions for Format C calendar-listing section (#3609)."""

    def test_contra_costa_prompt_includes_format_c_calendar_listings(self) -> None:
        """CONTRA_COSTA_SYSTEM_PROMPT must contain the Format C section substrings.

        Verifies that the prompt instructs the LLM to skip CC probate
        calendar-pointer documents (dept 30 sheets where every numbered entry
        is a pointer, not a ruling body).
        """
        assert "Format C" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "Calendar listings" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "see also alternate" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "rulings=[]" in CONTRA_COSTA_SYSTEM_PROMPT


class TestContraCostaPromptNPrefixCaseTypeNuance:
    """Prompt must instruct the LLM that N-prefix case_type is context-dependent (#4291).

    Pre-fix the prompt simply mapped ``N##-#### -- name change / probate``,
    which biased the LLM to return ``case_type='probate'`` for N-prefix
    rulings even when they appeared on civil-department master calendar
    PDFs (Dept 14, 32, 34) with plaintiff-v-defendant case titles.  These
    are legitimately civil rulings — see #4291's investigation:
    N25-2244 (Dept 34, "Leyla Ramirez Mejia v. (Unknown)"), N25-2433
    (Dept 14, "Ufcw-Northern California ... v. Jug Shop, Inc"), and
    N26-0247 (Dept 32, "Claim Of:Camila Garcia Nabor") all sat in
    civil dept calendar headers and were misclassified as probate.

    The prompt now instructs the LLM to infer case_type from the parent
    department + title format, not just the N-prefix.
    """

    def test_n_prefix_case_type_is_context_dependent(self) -> None:
        """Prompt must tell LLM to infer N-prefix case_type from context."""
        # The legacy "name change / probate" qualifier is preserved with
        # "typically" so the prompt still expresses the prior of probate-
        # likely on N-prefix cases — but pinned to "typically" rather than
        # asserted unconditionally.
        assert "typically name change / probate" in CONTRA_COSTA_SYSTEM_PROMPT
        # Civil-department override clause must be present so the LLM
        # knows N-prefix on civil dept PDFs is civil.
        assert "civil-department master" in CONTRA_COSTA_SYSTEM_PROMPT
        # Title-format override clause must be present so the LLM knows
        # plaintiff-v-defendant titles are civil regardless of prefix.
        assert "plaintiff-v-defendant case titles are CIVIL" in CONTRA_COSTA_SYSTEM_PROMPT
        # Issue-tracking marker so future readers can find the source.
        assert "#4291" in CONTRA_COSTA_SYSTEM_PROMPT
