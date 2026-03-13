"""LLM-powered ruling text formatter for consistent, readable HTML display.

Transforms raw ``ruling_text`` into structured semantic HTML using Claude Haiku.
The formatted output follows a standard template with consistent CSS classes
for case headers, captions, hearing info, and ruling body content.

Key guarantees:
- **Zero information loss**: validates that formatted output preserves all
  original text content (fuzzy match, 95% threshold).
- **Safe HTML**: sanitizes LLM output to prevent XSS — only whitelisted
  tags and ``class`` attribute allowed.
- **Graceful fallback**: on any failure (LLM error, truncation, validation
  failure), returns the original text wrapped in ``<pre>`` tags.

Configuration:
- ``ENABLE_RULING_FORMATTING``: environment variable to enable/disable.
  Defaults to disabled.
"""

from __future__ import annotations

import difflib
import html
import re

import structlog

from .llm_providers import LLMResponse, call_llm

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum text length for LLM formatting.  Shorter texts are wrapped in
# <pre> directly — too short to benefit from structured HTML.
_MIN_TEXT_LENGTH = 100

# Similarity threshold for zero-information-loss validation.
# 0.95 allows minor whitespace and entity encoding differences while
# catching content loss or summarization.
_VALIDATION_THRESHOLD = 0.95

# Default model for formatting — always Haiku for cost efficiency.
_FORMATTING_MODEL = "claude-haiku-4-5-20251001"

# Default max output tokens.  Haiku supports up to 8192 output tokens.
_DEFAULT_MAX_TOKENS = 8192

# ---------------------------------------------------------------------------
# HTML sanitization — whitelist approach
# ---------------------------------------------------------------------------

# Tags allowed in formatted ruling HTML.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "div",
        "header",
        "section",
        "h3",
        "h4",
        "p",
        "span",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "br",
        "pre",
        "b",
        "i",
        "a",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
)

# Attributes allowed on any tag.  Only ``class`` is needed for styling.
_ALLOWED_ATTRS: frozenset[str] = frozenset({"class"})

# Regex to match HTML tags (opening, closing, self-closing).
_TAG_RE = re.compile(r"<(/?)(\w+)([^>]*)(/?)>", re.DOTALL)

# Regex to extract individual attributes from an opening tag body.
_ATTR_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')

# Dangerous tags whose content should also be removed (not just the tag itself).
_DANGEROUS_CONTENT_RE = re.compile(
    r"<(?:script|style|iframe|object|embed|form)[^>]*>.*?</(?:script|style|iframe|object|embed|form)>",
    re.DOTALL | re.IGNORECASE,
)


def _sanitize_html(raw_html: str) -> str:
    """Sanitize HTML by allowing only whitelisted tags and attributes.

    First removes dangerous tags AND their content (script, style, iframe, etc.).
    Then strips remaining disallowed tags (keeping their text content) and
    filters attributes to only allow ``class``.

    Uses regex-based approach rather than importing additional libraries,
    since we only need a simple tag/attribute whitelist.
    """
    # Step 1: Remove dangerous tags with their content
    result = _DANGEROUS_CONTENT_RE.sub("", raw_html)

    def _replace_tag(match: re.Match[str]) -> str:
        closing_slash = match.group(1)  # "/" for closing tags
        tag_name = match.group(2).lower()
        attrs_str = match.group(3)
        self_closing = match.group(4)  # "/" for self-closing tags

        # Strip disallowed tags entirely
        if tag_name not in _ALLOWED_TAGS:
            return ""

        # For allowed tags, filter attributes
        if closing_slash:
            return f"</{tag_name}>"

        # Parse and filter attributes
        filtered_attrs: list[str] = []
        for attr_match in _ATTR_RE.finditer(attrs_str):
            attr_name = attr_match.group(1).lower()
            attr_value = attr_match.group(2) or attr_match.group(3) or ""
            if attr_name in _ALLOWED_ATTRS:
                filtered_attrs.append(f'{attr_name}="{html.escape(attr_value)}"')

        attr_str = " " + " ".join(filtered_attrs) if filtered_attrs else ""
        sc = " /" if self_closing else ""
        return f"<{tag_name}{attr_str}{sc}>"

    # Step 2: Filter remaining tags and attributes
    return _TAG_RE.sub(_replace_tag, result)


# ---------------------------------------------------------------------------
# Text extraction and validation
# ---------------------------------------------------------------------------

# Regex to strip all HTML tags for text content extraction.
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")

# Collapse runs of whitespace to a single space.
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html_tags(html_str: str) -> str:
    """Extract plain text content from HTML by stripping all tags.

    Used for validation — compares the text content of the formatted
    HTML against the original ruling text.
    """
    text = _STRIP_TAGS_RE.sub(" ", html_str)
    return text.strip()


def _normalize_text(text: str) -> str:
    """Normalize text for comparison — collapse whitespace, lowercase."""
    normalized = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return normalized


def _validate_no_information_loss(
    original: str,
    html_output: str,
    threshold: float = _VALIDATION_THRESHOLD,
) -> bool:
    """Validate that the HTML output preserves the original text content.

    Extracts plain text from the HTML, normalizes both texts, and computes
    a similarity ratio using ``difflib.SequenceMatcher``.

    Args:
        original: The original ruling text (plain text).
        html_output: The formatted HTML output from the LLM.
        threshold: Minimum similarity ratio to pass validation.

    Returns:
        ``True`` if the similarity ratio meets the threshold.
    """
    original_normalized = _normalize_text(original)
    html_text = _strip_html_tags(html_output)
    html_normalized = _normalize_text(html_text)

    if not original_normalized:
        return True  # Empty input trivially passes

    # autojunk=False is critical: the default autojunk heuristic marks
    # frequently-repeated characters as junk, which produces wildly wrong
    # similarity scores for repetitive legal text (e.g. 0.19 instead of 0.99).
    ratio = difflib.SequenceMatcher(
        None, original_normalized, html_normalized, autojunk=False
    ).ratio()

    if ratio < threshold:
        logger.warning(
            "ruling_formatter.validation_failed",
            similarity=round(ratio, 4),
            threshold=threshold,
            original_len=len(original_normalized),
            html_len=len(html_normalized),
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def _pre_wrap_fallback(ruling_text: str) -> str:
    """Wrap ruling text in a ``<pre>`` tag as a fallback.

    Escapes HTML entities in the original text to prevent XSS when
    the text is rendered in the browser.
    """
    escaped = html.escape(ruling_text)
    return f'<div class="ruling"><pre>{escaped}</pre></div>'


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_FORMATTING_SYSTEM_PROMPT = (
    "You are a legal document formatter. Your job is to take raw court "
    "ruling text and reformat it as clean, semantic HTML for web display.\n\n"
    "## Rules\n\n"
    "1. **Preserve ALL text verbatim.** Do not summarize, omit, paraphrase, "
    "or reword any content. Every word from the input must appear in the output.\n\n"
    "2. **Output HTML only.** Do not wrap in markdown code fences. "
    "Do not include ```html or ``` markers.\n\n"
    "3. **Use this template structure:**\n"
    "```\n"
    '<div class="ruling">\n'
    '  <header class="ruling-header">\n'
    '    <div class="case-number">Case No. [number]</div>\n'
    '    <div class="case-caption">\n'
    '      <span class="party plaintiff">[plaintiff name]</span>\n'
    '      <span class="vs">v.</span>\n'
    '      <span class="party defendant">[defendant name]</span>\n'
    "    </div>\n"
    '    <div class="hearing-info">\n'
    '      <span class="judge">[judge name, dept]</span>\n'
    '      <span class="date">[hearing date]</span>\n'
    "    </div>\n"
    "  </header>\n"
    '  <section class="ruling-body">\n'
    "    <h3>[Motion Type]</h3>\n"
    "    <p>[ruling text in proper paragraphs]</p>\n"
    "  </section>\n"
    "</div>\n"
    "```\n\n"
    "4. **Formatting guidelines:**\n"
    "   - Break text into logical paragraphs using <p> tags\n"
    "   - Preserve lists as <ul>/<ol> with <li> items\n"
    "   - Use <h3> for motion type headings\n"
    "   - Use <strong> for outcome words (GRANTED, DENIED, MOOT, etc.)\n"
    "   - If case info (number, parties, judge, date) is present in the text, "
    "extract it into the header. If not present, omit that header element.\n"
    "   - Use only these HTML tags: div, header, section, h3, h4, p, span, "
    "ul, ol, li, strong, em, br, pre, b, i, table, thead, tbody, tr, th, td\n"
    "   - Use only the class attribute for styling\n\n"
    "5. **If the text is too short or has no discernible structure**, "
    'wrap it in a simple <div class="ruling"><p>...</p></div>.\n'
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_ruling_text(
    ruling_text: str,
    *,
    client: object | None = None,
    timeout: float | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> str:
    """Format raw ruling text into structured semantic HTML.

    Uses Claude Haiku to transform unstructured court ruling text into
    clean HTML following a consistent template.  The result is sanitized
    and validated for zero information loss.

    On any failure (LLM error, truncation, validation failure), returns
    the original text wrapped in ``<pre>`` tags.

    Args:
        ruling_text: Raw ruling text to format.
        client: Optional pre-created ``anthropic.Anthropic`` client for
            connection reuse.
        timeout: Per-call timeout in seconds.
        max_tokens: Maximum output tokens for the LLM call.

    Returns:
        Formatted HTML string.  Always returns a non-empty string —
        either the LLM-formatted HTML or a ``<pre>``-wrapped fallback.
    """
    if not ruling_text or not ruling_text.strip():
        return _pre_wrap_fallback(ruling_text or "")

    # Short texts: skip LLM, wrap directly
    if len(ruling_text.strip()) < _MIN_TEXT_LENGTH:
        logger.info(
            "ruling_formatter.short_text_skipped",
            text_length=len(ruling_text),
        )
        return _pre_wrap_fallback(ruling_text)

    # Call LLM for formatting
    llm_response: LLMResponse | None = call_llm(
        system_prompt=_FORMATTING_SYSTEM_PROMPT,
        user_message=ruling_text,
        provider="anthropic",
        model=_FORMATTING_MODEL,
        client=client,
        max_tokens=max_tokens,
        timeout=timeout,
    )

    if llm_response is None:
        logger.warning("ruling_formatter.llm_call_failed")
        return _pre_wrap_fallback(ruling_text)

    # Log token usage for cost monitoring
    logger.info(
        "ruling_formatter.llm_response",
        input_tokens=llm_response.input_tokens,
        output_tokens=llm_response.output_tokens,
    )

    formatted_html = llm_response.text

    # Check for likely truncation — if output tokens equals max_tokens,
    # the response was probably truncated.
    if llm_response.output_tokens >= max_tokens:
        logger.warning(
            "ruling_formatter.response_truncated",
            output_tokens=llm_response.output_tokens,
            max_tokens=max_tokens,
        )
        return _pre_wrap_fallback(ruling_text)

    # Strip markdown code fences if present
    if formatted_html.startswith("```"):
        formatted_html = re.sub(r"^```\w*\n?", "", formatted_html)
        formatted_html = re.sub(r"\n?```$", "", formatted_html)

    # Sanitize HTML — strip dangerous tags/attributes
    sanitized_html = _sanitize_html(formatted_html)

    # Validate zero information loss
    if not _validate_no_information_loss(ruling_text, sanitized_html):
        logger.warning("ruling_formatter.validation_failed_fallback")
        return _pre_wrap_fallback(ruling_text)

    return sanitized_html
