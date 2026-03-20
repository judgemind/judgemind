"""Ruling summarization using Claude Haiku.

Generates plain-English summaries of California tentative rulings at
ingestion time. Summaries are pre-computed and cached — user-facing
pages serve cached summaries without live AI calls. Part of the
Judgemind NLP pipeline (architecture spec Section 5.2).
"""

from __future__ import annotations

import os

import anthropic

_SYSTEM_PROMPT = (
    "You are summarizing a court tentative ruling for a legal research platform.\n"
    "Write 2-4 sentences summarizing: what motion was before the court, "
    "what the judge's tentative decision is, and the key reason(s). "
    "Use plain language that a non-lawyer can understand. "
    "Do not include case numbers or procedural boilerplate.\n\n"
    "If optional case context is provided (e.g. case title, motion type), "
    "use it to make the summary more specific, but do not repeat it verbatim."
)


class RulingSummarizer:
    """Generates plain-English summaries of tentative rulings using Claude Haiku.

    Uses the Anthropic API to produce 2-4 sentence summaries suitable for
    display on the legal research platform. Summaries cover the motion,
    the judge's decision, and the key reasoning.
    """

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize the summarizer with an Anthropic API key.

        Args:
            api_key: Anthropic API key. If None, reads from ANTHROPIC_API_KEY
                environment variable.

        Raises:
            ValueError: If no API key is provided and ANTHROPIC_API_KEY is not set.
        """
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "api_key must be provided or ANTHROPIC_API_KEY environment variable must be set"
            )
        self._client = anthropic.Anthropic(api_key=resolved_key)

    def summarize(self, ruling_text: str, case_context: dict[str, str] | None = None) -> str:
        """Summarize a tentative ruling in plain English.

        Args:
            ruling_text: The full text of a tentative ruling.
            case_context: Optional dict with case metadata (e.g. case_title,
                motion_type) to improve summary specificity.

        Returns:
            A 2-4 sentence plain-English summary of the ruling.

        Raises:
            ValueError: If the ruling text is empty.
        """
        if not ruling_text.strip():
            raise ValueError("ruling_text must not be empty")

        user_content = "Summarize this tentative ruling:\n\n"
        if case_context:
            context_lines = [f"- {k}: {v}" for k, v in case_context.items()]
            user_content += "Case context:\n" + "\n".join(context_lines) + "\n\n"
        user_content += ruling_text

        response = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
        )

        return response.content[0].text.strip()
