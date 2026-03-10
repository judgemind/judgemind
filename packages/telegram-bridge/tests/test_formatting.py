"""Tests for Telegram MarkdownV2 formatting helpers."""

from telegram_bridge.formatting import escape_mdv2, format_status_card, linkify_github_refs


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


class TestLinkifyGithubRefs:
    def test_issue_reference(self) -> None:
        result = linkify_github_refs("Fixed #42 today")
        assert "[\\#42](https://github.com/judgemind/judgemind/issues/42)" in result
        assert "Fixed " in result
        assert " today" in result

    def test_pr_reference(self) -> None:
        result = linkify_github_refs("Merged PR #523")
        assert "[PR \\#523](https://github.com/judgemind/judgemind/pull/523)" in result

    def test_both_issue_and_pr(self) -> None:
        result = linkify_github_refs("PR #523 closes #42")
        assert "[PR \\#523](https://github.com/judgemind/judgemind/pull/523)" in result
        assert "[\\#42](https://github.com/judgemind/judgemind/issues/42)" in result

    def test_no_references(self) -> None:
        result = linkify_github_refs("No references here.")
        assert result == escape_mdv2("No references here.")

    def test_custom_repo(self) -> None:
        result = linkify_github_refs("#99", repo="owner/other-repo")
        assert "https://github.com/owner/other-repo/issues/99" in result

    def test_escapes_surrounding_text(self) -> None:
        result = linkify_github_refs("Issue #10 (done)")
        # Parens around "done" should be escaped
        assert "\\(done\\)" in result
        # But the link parens should NOT be escaped
        assert "(https://github.com/" in result

    def test_multiple_issue_refs(self) -> None:
        result = linkify_github_refs("#1 and #2")
        assert "[\\#1](https://github.com/judgemind/judgemind/issues/1)" in result
        assert "[\\#2](https://github.com/judgemind/judgemind/issues/2)" in result


class TestFormatStatusCard:
    def test_complete_state(self) -> None:
        card = format_status_card(task="#476", state="complete", details="PR #482 merged.")
        # Should contain the issue in bold with a link.
        assert "*Issue" in card
        assert "[\\#476](https://github.com/judgemind/judgemind/issues/476)" in card
        # Should use the checkmark emoji for "complete".
        assert "\u2705" in card
        # Details should have a linked PR reference.
        assert "[PR \\#482](https://github.com/judgemind/judgemind/pull/482)" in card

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
