"""Telegram MarkdownV2 formatting helpers."""

from __future__ import annotations

import re

# Characters that must be escaped in Telegram MarkdownV2 outside of code blocks.
_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_mdv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    See https://core.telegram.org/bots/api#markdownv2-style
    """
    return _ESCAPE_RE.sub(r"\\\1", text)


def format_status_card(*, task: str, state: str, details: str) -> str:
    """Build a compact MarkdownV2 status card.

    Returns a string ready to pass as ``text`` with ``parse_mode=MarkdownV2``.
    """
    state_emoji = _STATE_EMOJIS.get(state.lower(), "\u2139\ufe0f")
    # Task label is bold, details are plain text (escaped).
    return (
        f"\U0001f4cb *Task {escape_mdv2(task)}*\n"
        f"Status: {state_emoji} {escape_mdv2(state.capitalize())}\n"
        f"{escape_mdv2(details)}"
    )


_STATE_EMOJIS: dict[str, str] = {
    "complete": "\u2705",
    "in_progress": "\u23f3",
    "failed": "\u274c",
    "blocked": "\U0001f6d1",
    "waiting": "\u23f3",
}
