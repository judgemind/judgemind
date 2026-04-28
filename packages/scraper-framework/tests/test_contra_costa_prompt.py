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
