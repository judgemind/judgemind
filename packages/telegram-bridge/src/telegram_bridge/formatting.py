"""Telegram HTML formatting helpers.

Uses ``parse_mode=HTML`` instead of ``MarkdownV2`` because HTML only
requires escaping ``<``, ``>``, and ``&``, making it far more robust
for free-text replies from the Claude interpreter.  MarkdownV2 requires
escaping 18+ special characters and silently breaks with 400 errors when
any are missed.
"""

from __future__ import annotations

import html
import re

# Default GitHub repository for link generation.
DEFAULT_GITHUB_REPO = "judgemind/judgemind"

# Matches "PR #N" or standalone "#N" (not preceded by a word char to avoid
# matching inside URLs or other tokens).  The PR variant is checked first so
# that "PR #123" is captured as a single match rather than matching "#123"
# alone.
_GITHUB_REF_RE = re.compile(r"(?<!\w)(PR\s+#(\d+))|(?<!\w)#(\d+)")


def escape_html(text: str) -> str:
    """Escape special characters for Telegram HTML parse mode.

    Only ``<``, ``>``, and ``&`` need escaping.

    See https://core.telegram.org/bots/api#html-style
    """
    return html.escape(text, quote=False)


# Keep the old name as an alias for backwards compatibility during the
# transition.  Callers that imported ``escape_mdv2`` will get the HTML
# escaper instead, which is safe — HTML-escaping is a superset of "do
# nothing harmful" for plain text.
escape_mdv2 = escape_html


def linkify_github_refs(text: str, *, repo: str = DEFAULT_GITHUB_REPO) -> str:
    """Replace ``#N`` and ``PR #N`` references with clickable HTML links.

    Non-link text is escaped for HTML; link syntax uses ``<a href>`` tags.

    Args:
        text: Raw (unescaped) text that may contain GitHub references.
        repo: GitHub repository in ``owner/repo`` format.

    Returns:
        HTML-safe string with clickable links.
    """
    base_url = f"https://github.com/{repo}"
    parts: list[str] = []
    last_end = 0

    for m in _GITHUB_REF_RE.finditer(text):
        # Escape any text before this match.
        parts.append(escape_html(text[last_end : m.start()]))

        if m.group(1):
            # "PR #N" variant
            pr_num = m.group(2)
            parts.append(f'<a href="{base_url}/pull/{pr_num}">PR #{pr_num}</a>')
        else:
            # Standalone "#N"
            issue_num = m.group(3)
            parts.append(f'<a href="{base_url}/issues/{issue_num}">#{issue_num}</a>')

        last_end = m.end()

    # Escape any remaining text after the last match.
    parts.append(escape_html(text[last_end:]))
    return "".join(parts)


def format_status_card(
    *,
    task: str,
    state: str,
    details: str,
    repo: str = DEFAULT_GITHUB_REPO,
) -> str:
    """Build a compact HTML status card.

    Returns a string ready to pass as ``text`` with ``parse_mode=HTML``.
    """
    state_emoji = _STATE_EMOJIS.get(state.lower(), "\u2139\ufe0f")
    # Issue label is bold; task and details get GitHub-linked references.
    return (
        f"\U0001f4cb <b>Issue {linkify_github_refs(task, repo=repo)}</b>\n"
        f"Status: {state_emoji} {escape_html(state.capitalize())}\n"
        f"{linkify_github_refs(details, repo=repo)}"
    )


_STATE_EMOJIS: dict[str, str] = {
    "complete": "\u2705",
    "in_progress": "\u23f3",
    "failed": "\u274c",
    "blocked": "\U0001f6d1",
    "waiting": "\u23f3",
}
