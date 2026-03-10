"""Tests for Telegram MarkdownV2 formatting helpers."""

from telegram_bridge.formatting import escape_mdv2, format_status_card


class TestEscapeMdv2:
    def test_plain_text_unchanged(self) -> None:
        assert escape_mdv2("hello world") == "hello world"

    def test_special_chars_escaped(self) -> None:
        assert escape_mdv2("foo_bar") == r"foo\_bar"
        assert escape_mdv2("*bold*") == r"\*bold\*"
        assert escape_mdv2("#heading") == r"\#heading"
        assert escape_mdv2("a.b") == r"a\.b"

    def test_multiple_specials(self) -> None:
        result = escape_mdv2("PR #482 (merged)")
        assert r"\#" in result
        assert r"\(" in result
        assert r"\)" in result


class TestFormatStatusCard:
    def test_complete_state(self) -> None:
        card = format_status_card(task="#476", state="complete", details="PR #482 merged.")
        # Should contain the task in bold.
        assert r"*Task \#476*" in card
        # Should use the checkmark emoji for "complete".
        assert "\u2705" in card

    def test_failed_state(self) -> None:
        card = format_status_card(task="#99", state="failed", details="CI red.")
        assert "\u274c" in card

    def test_unknown_state_gets_default_emoji(self) -> None:
        card = format_status_card(task="#1", state="custom", details="stuff")
        # Default info emoji.
        assert "\u2139" in card
