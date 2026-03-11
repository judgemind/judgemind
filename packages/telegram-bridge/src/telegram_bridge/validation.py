"""Telegram payload validation helpers.

Shared between the Tier 1 CI integration tests and the Tier 2 e2e smoke test
script (``scripts/tg-smoke-test.py``).

These functions validate that a ``sendMessage`` payload is well-formed and will
not be rejected by the Telegram Bot API (HTTP 400).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Telegram HTML tags that are valid in parse_mode=HTML.
# See https://core.telegram.org/bots/api#html-style
_ALLOWED_TAGS_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler"
    r"|a|tg-emoji|code|pre|blockquote)\b[^>]*>",
    re.IGNORECASE,
)

# Matches raw `<` or `>` that are NOT part of an allowed HTML tag.
# Strategy: strip all allowed tags, then check for remaining `<` or `>`.
_RAW_ANGLE_RE = re.compile(r"[<>]")

# Matches `&` that is NOT part of a valid HTML entity.
_RAW_AMP_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;|#\d+;|#x[\da-fA-F]+;)")

# GitHub issue/PR link pattern in HTML `<a>` tags.
_GITHUB_LINK_RE = re.compile(
    r'<a href="https://github\.com/[^"]+/(?:issues|pull)/(\d+)">'
    r"(?:PR\s+)?#\1</a>"
)


@dataclass
class ValidationResult:
    """Result of validating a Telegram sendMessage payload."""

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        """Record a validation error (payload would likely be rejected)."""
        self.errors.append(msg)
        self.valid = False

    def add_warning(self, msg: str) -> None:
        """Record a non-fatal warning."""
        self.warnings.append(msg)


def validate_telegram_payload(payload: dict[str, Any]) -> ValidationResult:
    """Validate a Telegram ``sendMessage`` payload for correctness.

    Checks:
    - Required fields are present (``chat_id``, ``text``).
    - ``text`` is not empty.
    - If ``parse_mode`` is ``HTML``, the text contains only valid HTML tags
      and all special characters are properly escaped.
    - GitHub references (``#N``, ``PR #N``) are linkified as ``<a>`` tags.
    - ``disable_web_page_preview`` is set to avoid unwanted link previews.

    Args:
        payload: The JSON body that would be sent to Telegram's
            ``sendMessage`` endpoint.

    Returns:
        A :class:`ValidationResult` with ``valid=True`` if the payload is
        well-formed, or ``valid=False`` with specific error messages.
    """
    result = ValidationResult()

    # --- Required fields ---
    if "chat_id" not in payload:
        result.add_error("Missing required field: chat_id")

    text = payload.get("text")
    if not text:
        result.add_error("Missing or empty field: text")
        return result  # Cannot validate further without text.

    # --- HTML parse mode validation ---
    parse_mode = payload.get("parse_mode", "")
    if parse_mode == "HTML":
        _validate_html_text(text, result)

    # --- Link preview ---
    if not payload.get("disable_web_page_preview"):
        result.add_warning("disable_web_page_preview is not set; link previews may appear.")

    return result


def _validate_html_text(text: str, result: ValidationResult) -> None:
    """Validate that *text* is safe for Telegram HTML parse mode."""
    # Strip allowed tags to find any raw angle brackets.
    stripped = _ALLOWED_TAGS_RE.sub("", text)
    raw_angles = _RAW_ANGLE_RE.findall(stripped)
    if raw_angles:
        result.add_error(
            f"Text contains unescaped angle bracket(s) outside allowed HTML tags: "
            f"{raw_angles!r} in stripped text: {stripped[:200]!r}"
        )

    # Check for unescaped ampersands.
    raw_amps = _RAW_AMP_RE.findall(stripped)
    if raw_amps:
        result.add_error(
            "Text contains unescaped '&' character(s) that are not valid HTML entities."
        )


def validate_github_links(text: str) -> list[int]:
    """Extract issue/PR numbers from GitHub ``<a>`` links in *text*.

    Returns a list of integer issue/PR numbers found as clickable links.
    This can be used to verify that expected references were linkified.
    """
    return [int(m.group(1)) for m in _GITHUB_LINK_RE.finditer(text)]


def validate_html_round_trip(raw_text: str, formatted_text: str) -> ValidationResult:
    """Validate that *formatted_text* is a safe HTML-formatted version of *raw_text*.

    Checks that:
    - All plain-text segments are properly HTML-escaped.
    - GitHub references in *raw_text* appear as ``<a>`` links in *formatted_text*.

    Args:
        raw_text: The original unformatted text.
        formatted_text: The HTML-formatted version (output of ``linkify_github_refs``).

    Returns:
        A :class:`ValidationResult`.
    """
    result = ValidationResult()

    # Verify that the formatted text is valid HTML for Telegram.
    _validate_html_text(formatted_text, result)

    # Check that issue references in raw text are linkified.
    issue_refs = re.findall(r"(?<!\w)#(\d+)", raw_text)
    linked_nums = validate_github_links(formatted_text)

    for ref in issue_refs:
        num = int(ref)
        if num not in linked_nums:
            result.add_warning(f"Issue reference #{num} in raw text is not linkified in output.")

    return result
