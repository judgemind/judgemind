"""Tests for markdown-to-HTML and markdown-stripping helpers in the worker."""

from __future__ import annotations

from ingestion.worker import _markdown_to_html, _strip_markdown


class TestStripMarkdown:
    def test_strips_bold(self) -> None:
        assert _strip_markdown("The motion is **GRANTED**.") == "The motion is GRANTED."

    def test_strips_italic(self) -> None:
        assert _strip_markdown("See *Smith v. Jones*.") == "See Smith v. Jones."

    def test_strips_both(self) -> None:
        result = _strip_markdown("**Bold** and *italic* text.")
        assert result == "Bold and italic text."

    def test_no_markdown_unchanged(self) -> None:
        text = "Plain text with no formatting."
        assert _strip_markdown(text) == text


class TestMarkdownToHtml:
    def test_bold(self) -> None:
        result = _markdown_to_html("**GRANTED**")
        assert "<strong>GRANTED</strong>" in result

    def test_italic(self) -> None:
        result = _markdown_to_html("*Smith v. Jones*")
        assert "<em>Smith v. Jones</em>" in result

    def test_paragraphs(self) -> None:
        result = _markdown_to_html("First paragraph.\n\nSecond paragraph.")
        assert "<p>" in result
        assert "First paragraph." in result
        assert "Second paragraph." in result

    def test_numbered_list(self) -> None:
        result = _markdown_to_html("1. First item\n2. Second item")
        assert "<ol>" in result
        assert "<li>First item</li>" in result
        assert "<li>Second item</li>" in result

    def test_html_escaping(self) -> None:
        result = _markdown_to_html("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_empty_string(self) -> None:
        assert _markdown_to_html("") == ""
