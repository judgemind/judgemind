"""Tests for Telegram HTML formatting helpers."""

from telegram_bridge.formatting import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    escape_html,
    escape_mdv2,
    format_status_card,
    linkify_github_refs,
    split_message,
)


class TestEscapeHtml:
    def test_plain_text_unchanged(self) -> None:
        assert escape_html("hello world") == "hello world"

    def test_ampersand_escaped(self) -> None:
        assert escape_html("foo & bar") == "foo &amp; bar"

    def test_angle_brackets_escaped(self) -> None:
        assert escape_html("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"

    def test_special_chars_not_escaped(self) -> None:
        """Characters that are special in MarkdownV2 but NOT in HTML should pass through."""
        assert escape_html("foo_bar") == "foo_bar"
        assert escape_html("*bold*") == "*bold*"
        assert escape_html("#heading") == "#heading"
        assert escape_html("a.b") == "a.b"
        assert escape_html("(parens)") == "(parens)"
        assert escape_html("Links working!") == "Links working!"

    def test_multiple_specials(self) -> None:
        result = escape_html("a < b & c > d")
        assert result == "a &lt; b &amp; c &gt; d"


class TestEscapeMdv2:
    """Backward-compatibility tests for the deprecated escape_mdv2 function."""

    def test_plain_text_unchanged(self) -> None:
        assert escape_mdv2("hello world") == "hello world"

    def test_special_chars_escaped(self) -> None:
        assert escape_mdv2("foo_bar") == r"foo\_bar"
        assert escape_mdv2("*bold*") == r"\*bold\*"
        assert escape_mdv2("#heading") == r"\#heading"
        assert escape_mdv2("a.b") == r"a\.b"


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
        result = linkify_github_refs("Issue #10 <done>")
        # Angle brackets around "done" should be escaped for HTML
        assert "&lt;done&gt;" in result
        # But the link HTML should NOT be escaped
        assert '<a href="https://github.com/' in result

    def test_ampersand_in_surrounding_text(self) -> None:
        result = linkify_github_refs("Fix #10 & ship")
        assert "&amp;" in result
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

    def test_uses_html_bold(self) -> None:
        card = format_status_card(task="#1", state="complete", details="Done.")
        assert "<b>" in card
        assert "</b>" in card
        # Should NOT contain MarkdownV2 bold markers
        assert "*Issue" not in card


class TestSplitMessage:
    def test_short_message_returns_single_chunk(self) -> None:
        text = "Hello world"
        result = split_message(text)
        assert result == [text]

    def test_empty_string(self) -> None:
        result = split_message("")
        assert result == [""]

    def test_exact_limit_not_split(self) -> None:
        text = "a" * 4096
        result = split_message(text)
        assert result == [text]

    def test_splits_at_paragraph_boundary(self) -> None:
        para1 = "First paragraph. " * 100  # ~1800 chars
        para2 = "Second paragraph. " * 100
        para3 = "Third paragraph. " * 100
        text = f"{para1}\n\n{para2}\n\n{para3}"
        result = split_message(text, max_length=4096)
        assert len(result) >= 2
        # Each chunk should be within the limit.
        for chunk in result:
            assert len(chunk) <= 4096

    def test_splits_at_newline_when_no_paragraph_break(self) -> None:
        # Build a text with only single newlines.
        lines = ["Line number " + str(i) for i in range(500)]
        text = "\n".join(lines)
        result = split_message(text, max_length=4096)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 4096

    def test_splits_at_space_when_no_newlines(self) -> None:
        text = "word " * 1000  # ~5000 chars
        result = split_message(text, max_length=4096)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 4096

    def test_hard_cut_when_no_good_split_point(self) -> None:
        # One long word with no spaces or newlines.
        text = "a" * 5000
        result = split_message(text, max_length=4096)
        assert len(result) == 2
        assert len(result[0]) == 4096
        assert len(result[1]) == 904

    def test_custom_max_length(self) -> None:
        text = "Hello World! " * 10  # ~130 chars
        result = split_message(text, max_length=50)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 50

    def test_preserves_content(self) -> None:
        """All original text should be recoverable from the chunks."""
        para1 = "First paragraph."
        para2 = "Second paragraph."
        text = f"{para1}\n\n{para2}"
        result = split_message(text, max_length=25)
        # The full text content should be preserved across chunks.
        joined = "\n\n".join(result) if len(result) > 1 else result[0]
        assert para1 in joined
        assert para2 in joined

    def test_default_limit_is_telegram_max(self) -> None:
        assert TELEGRAM_MAX_MESSAGE_LENGTH == 4096
