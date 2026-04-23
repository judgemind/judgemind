"""Tests for the ruling text formatter module.

Tests cover:
- LLM-powered formatting with mocked API calls
- HTML sanitization (XSS prevention)
- Zero information loss validation
- Fallback behavior (LLM failure, truncation, validation failure)
- Short text optimization
"""

from __future__ import annotations

from unittest.mock import patch

from ingestion.llm_providers import LLMResponse
from ingestion.ruling_formatter import (
    _normalize_text,
    _pre_wrap_fallback,
    _sanitize_html,
    _strip_html_tags,
    _validate_no_information_loss,
    format_ruling_text,
)

# ---------------------------------------------------------------------------
# _strip_html_tags
# ---------------------------------------------------------------------------


def test_strip_html_tags_basic() -> None:
    """Strips all HTML tags and returns plain text."""
    html = '<div class="ruling"><p>Hello <strong>world</strong></p></div>'
    result = _strip_html_tags(html)
    assert "Hello" in result
    assert "world" in result
    assert "<" not in result


def test_strip_html_tags_empty() -> None:
    """Empty string returns empty string."""
    assert _strip_html_tags("") == ""


def test_strip_html_tags_no_tags() -> None:
    """Plain text passes through unchanged."""
    assert _strip_html_tags("Hello world") == "Hello world"


# ---------------------------------------------------------------------------
# _normalize_text
# ---------------------------------------------------------------------------


def test_normalize_text_collapses_whitespace() -> None:
    """Multiple spaces and newlines collapse to single space."""
    result = _normalize_text("  hello   world\n\nfoo  ")
    assert result == "hello world foo"


def test_normalize_text_lowercases() -> None:
    """Text is lowercased for comparison."""
    result = _normalize_text("HELLO World")
    assert result == "hello world"


# ---------------------------------------------------------------------------
# _validate_no_information_loss
# ---------------------------------------------------------------------------


def test_validate_no_information_loss_pass() -> None:
    """Identical text passes validation."""
    original = "The motion for summary judgment is GRANTED."
    html_output = "<p>The motion for summary judgment is <strong>GRANTED</strong>.</p>"
    assert _validate_no_information_loss(original, html_output) is True


def test_validate_no_information_loss_with_whitespace_diff() -> None:
    """Minor whitespace differences pass validation."""
    original = "Hello   world.\nNew paragraph."
    html_output = "<p>Hello world.</p><p>New paragraph.</p>"
    assert _validate_no_information_loss(original, html_output) is True


def test_validate_no_information_loss_fail() -> None:
    """Substantially different text fails validation."""
    original = "The motion for summary judgment is GRANTED. The court finds that..."
    html_output = "<p>Motion denied.</p>"
    assert _validate_no_information_loss(original, html_output) is False


def test_validate_no_information_loss_empty_original() -> None:
    """Empty original text trivially passes."""
    assert _validate_no_information_loss("", "<p></p>") is True


# ---------------------------------------------------------------------------
# _pre_wrap_fallback
# ---------------------------------------------------------------------------


def test_pre_wrap_fallback_basic() -> None:
    """Wraps text in pre tags with ruling class."""
    result = _pre_wrap_fallback("Hello world")
    assert '<div class="ruling"><pre>' in result
    assert "Hello world" in result
    assert "</pre></div>" in result


def test_pre_wrap_fallback_escapes_html() -> None:
    """HTML entities in the original text are escaped."""
    result = _pre_wrap_fallback("<script>alert('xss')</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


# ---------------------------------------------------------------------------
# _sanitize_html
# ---------------------------------------------------------------------------


def test_sanitize_html_strips_script_tags() -> None:
    """Script tags are stripped."""
    html = '<div class="ruling"><p>Hello</p><script>alert("xss")</script></div>'
    result = _sanitize_html(html)
    assert "<script>" not in result
    assert "alert" not in result
    assert "<p>Hello</p>" in result


def test_sanitize_html_strips_iframe() -> None:
    """Iframe tags are stripped."""
    html = '<p>Text</p><iframe src="evil.com"></iframe>'
    result = _sanitize_html(html)
    assert "<iframe" not in result
    assert "<p>Text</p>" in result


def test_sanitize_html_strips_event_handlers() -> None:
    """Event handler attributes are stripped."""
    html = '<div onclick="alert(1)" class="ruling">Text</div>'
    result = _sanitize_html(html)
    assert "onclick" not in result
    assert 'class="ruling"' in result


def test_sanitize_html_preserves_allowed_tags() -> None:
    """Allowed tags with class attributes are preserved."""
    html = (
        '<div class="ruling">'
        '<header class="ruling-header">'
        "<h3>Motion</h3>"
        "<p>Text with <strong>emphasis</strong> and <em>italic</em></p>"
        "<ul><li>Item 1</li></ul>"
        "</header>"
        "</div>"
    )
    result = _sanitize_html(html)
    assert '<div class="ruling">' in result
    assert "<h3>Motion</h3>" in result
    assert "<strong>emphasis</strong>" in result
    assert "<em>italic</em>" in result
    assert "<ul><li>Item 1</li></ul>" in result


def test_sanitize_html_strips_style_tag() -> None:
    """Style tags are stripped."""
    html = "<style>body { color: red; }</style><p>Text</p>"
    result = _sanitize_html(html)
    assert "<style>" not in result
    assert "<p>Text</p>" in result


def test_sanitize_html_strips_disallowed_attributes() -> None:
    """Attributes other than class are stripped."""
    html = '<div class="ruling" style="color:red" id="foo">Text</div>'
    result = _sanitize_html(html)
    assert 'class="ruling"' in result
    assert "style=" not in result
    assert "id=" not in result


# ---------------------------------------------------------------------------
# format_ruling_text — main function
# ---------------------------------------------------------------------------


@patch("ingestion.ruling_formatter.call_llm")
def test_format_ruling_text_success(mock_call_llm: object) -> None:
    """Successful LLM formatting returns sanitized HTML."""
    original = "The motion for summary judgment is GRANTED. " * 5  # > 100 chars
    # Build formatted HTML that preserves the same text content
    formatted = (
        '<div class="ruling"><section class="ruling-body"><p>'
        "The motion for summary judgment is <strong>GRANTED</strong>. "
        "The motion for summary judgment is <strong>GRANTED</strong>. "
        "The motion for summary judgment is <strong>GRANTED</strong>. "
        "The motion for summary judgment is <strong>GRANTED</strong>. "
        "The motion for summary judgment is <strong>GRANTED</strong>. "
        "</p></section></div>"
    )
    mock_call_llm.return_value = LLMResponse(  # type: ignore[union-attr]
        text=formatted,
        input_tokens=100,
        output_tokens=200,
    )

    result = format_ruling_text(original, client=object())
    assert '<div class="ruling">' in result
    assert "<strong>GRANTED</strong>" in result
    assert "<script>" not in result


@patch("ingestion.ruling_formatter.call_llm")
def test_format_ruling_text_llm_failure_returns_pre(mock_call_llm: object) -> None:
    """LLM failure returns <pre> fallback."""
    mock_call_llm.return_value = None  # type: ignore[union-attr]
    original = "A" * 200  # > 100 chars
    result = format_ruling_text(original, client=object())
    assert "<pre>" in result
    assert "A" * 200 in result


@patch("ingestion.ruling_formatter.call_llm")
def test_format_falls_back_to_pre_on_validation_failure(mock_call_llm: object) -> None:
    """When validation fails, returns <pre> fallback."""
    original = "The motion for summary judgment is GRANTED. " * 5
    # Return completely different text — will fail validation
    mock_call_llm.return_value = LLMResponse(  # type: ignore[union-attr]
        text="<p>Something completely different that does not match the original at all.</p>",
        input_tokens=100,
        output_tokens=50,
    )

    result = format_ruling_text(original, client=object())
    assert "<pre>" in result
    assert "GRANTED" in result  # Original text preserved in fallback


@patch("ingestion.ruling_formatter.call_llm")
def test_format_falls_back_to_pre_on_truncation(mock_call_llm: object) -> None:
    """When output tokens equals max_tokens (truncation), returns <pre> fallback."""
    original = "The motion for summary judgment is GRANTED. " * 5
    mock_call_llm.return_value = LLMResponse(  # type: ignore[union-attr]
        text="<p>Truncated...",
        input_tokens=100,
        output_tokens=8192,  # equals default max_tokens
    )

    result = format_ruling_text(original, client=object())
    assert "<pre>" in result
    assert "GRANTED" in result


def test_format_ruling_text_short_text_returns_pre() -> None:
    """Text under 100 chars returns <pre> fallback without LLM call."""
    result = format_ruling_text("Short text.")
    assert "<pre>" in result
    assert "Short text." in result


def test_format_ruling_text_empty_returns_pre() -> None:
    """Empty text returns <pre> fallback."""
    result = format_ruling_text("")
    assert "<pre>" in result


def test_format_ruling_text_none_text_returns_pre() -> None:
    """None-ish text returns <pre> fallback."""
    result = format_ruling_text("   ")
    assert "<pre>" in result


@patch("ingestion.ruling_formatter.call_llm")
def test_format_ruling_text_strips_code_fences(mock_call_llm: object) -> None:
    """Markdown code fences are stripped from LLM output."""
    original = "The motion for summary judgment is GRANTED. " * 5
    formatted_with_fences = (
        "```html\n"
        '<div class="ruling"><p>'
        + "The motion for summary judgment is <strong>GRANTED</strong>. " * 5
        + "</p></div>\n```"
    )
    mock_call_llm.return_value = LLMResponse(  # type: ignore[union-attr]
        text=formatted_with_fences,
        input_tokens=100,
        output_tokens=200,
    )

    result = format_ruling_text(original, client=object())
    assert "```" not in result
    assert '<div class="ruling">' in result
