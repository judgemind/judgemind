"""Tests for Telegram HTML formatting helpers."""

from telegram_bridge.formatting import (
    escape_html,
    escape_mdv2,
    format_status_card,
    linkify_github_refs,
)


class TestEscapeHtml:
    def test_plain_text_unchanged(self) -> None:
        assert escape_html("hello world") == "hello world"

    def test_angle_brackets_escaped(self) -> None:
        assert escape_html("a < b > c") == "a &lt; b &gt; c"

    def test_ampersand_escaped(self) -> None:
        assert escape_html("A & B") == "A &amp; B"

    def test_special_chars_not_escaped(self) -> None:
        """Characters that were problematic in MarkdownV2 should pass through in HTML."""
        assert escape_html("foo_bar") == "foo_bar"
        assert escape_html("*bold*") == "*bold*"
        assert escape_html("#heading") == "#heading"
        assert escape_html("a.b") == "a.b"
        assert escape_html("(parens)") == "(parens)"
        assert escape_html("a-b") == "a-b"

    def test_exclamation_mark_not_escaped(self) -> None:
        assert escape_html("Links working!") == "Links working!"
        assert escape_html("Done! Next step.") == "Done! Next step."

    def test_multiple_html_specials(self) -> None:
        result = escape_html("<script>alert('xss')</script>")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<" not in result.replace("&lt;", "").replace("&gt;", "")


class TestEscapeMdv2Alias:
    """The escape_mdv2 alias should behave identically to escape_html."""

    def test_alias_is_escape_html(self) -> None:
        assert escape_mdv2 is escape_html

    def test_alias_escapes_html_chars(self) -> None:
        assert escape_mdv2("a < b") == "a &lt; b"


class TestLinkifyGithubRefs:
    def test_issue_reference(self) -> None:
        result = linkify_github_refs("Fixed #42 today")
        assert '<a href="https://github.com/judgemind/judgemind/issues/42">#42</a>' in result
        assert "Fixed " in result
        assert " today" in result

    def test_pr_reference(self) -> None:
        result = linkify_github_refs("Merged PR #523")
        assert '<a href="https://github.com/judgemind/judgemind/pull/523">PR #523</a>' in result

    def test_both_issue_and_pr(self) -> None:
        result = linkify_github_refs("PR #523 closes #42")
        assert '<a href="https://github.com/judgemind/judgemind/pull/523">PR #523</a>' in result
        assert '<a href="https://github.com/judgemind/judgemind/issues/42">#42</a>' in result

    def test_no_references(self) -> None:
        result = linkify_github_refs("No references here.")
        assert result == escape_html("No references here.")

    def test_custom_repo(self) -> None:
        result = linkify_github_refs("#99", repo="owner/other-repo")
        assert "https://github.com/owner/other-repo/issues/99" in result

    def test_escapes_surrounding_text(self) -> None:
        result = linkify_github_refs("Issue #10 (done)")
        # Parens should NOT need escaping in HTML (unlike MarkdownV2)
        assert "(done)" in result
        # The link should be an <a> tag
        assert '<a href="https://github.com/' in result

    def test_html_special_chars_escaped_in_surrounding_text(self) -> None:
        result = linkify_github_refs("A < B & #10 > C")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result
        assert '<a href="https://github.com/judgemind/judgemind/issues/10">#10</a>' in result

    def test_multiple_issue_refs(self) -> None:
        result = linkify_github_refs("#1 and #2")
        assert '<a href="https://github.com/judgemind/judgemind/issues/1">#1</a>' in result
        assert '<a href="https://github.com/judgemind/judgemind/issues/2">#2</a>' in result


class TestFormatStatusCard:
    def test_complete_state(self) -> None:
        card = format_status_card(task="#476", state="complete", details="PR #482 merged.")
        # Should contain the issue in bold with a link.
        assert "<b>Issue" in card
        assert '<a href="https://github.com/judgemind/judgemind/issues/476">#476</a>' in card
        # Should use the checkmark emoji for "complete".
        assert "\u2705" in card
        # Details should have a linked PR reference.
        assert '<a href="https://github.com/judgemind/judgemind/pull/482">PR #482</a>' in card

    def test_failed_state(self) -> None:
        card = format_status_card(task="#99", state="failed", details="CI red.")
        assert "\u274c" in card

    def test_unknown_state_gets_default_emoji(self) -> None:
        card = format_status_card(task="#1", state="custom", details="stuff")
        # Default info emoji.
        assert "\u2139" in card

    def test_says_issue_not_task(self) -> None:
        card = format_status_card(task="#1", state="complete", details="Done.")
        assert "Issue" in card
        assert "Task" not in card
