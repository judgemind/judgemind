"""Ruling outcome and motion type classifier using Claude Haiku.

Classifies tentative rulings into structured outcome and motion type
categories for the Judgemind NLP pipeline (architecture spec Section 5.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

# Allowed values for ruling outcomes
OUTCOME_VALUES = frozenset(
    {
        "granted",
        "denied",
        "granted_in_part",
        "denied_in_part",
        "moot",
        "continued",
        "off_calendar",
        "submitted",
        "other",
    }
)

# Allowed values for motion types
MOTION_TYPE_VALUES = frozenset(
    {
        "msj",
        "msj_partial",
        "mtd",
        "mil",
        "demurrer",
        "motion_to_compel",
        "motion_to_strike",
        "anti_slapp",
        "preliminary_injunction",
        "other",
    }
)

_SYSTEM_PROMPT = """You are a legal document classifier for California court tentative rulings.

Given a tentative ruling text, classify it into:
1. **outcome** - the ruling's outcome
2. **motion_type** - the type of motion being ruled on
3. **confidence** - your confidence in the classification (0.0 to 1.0)

Allowed outcome values:
- granted: Motion fully granted
- denied: Motion fully denied
- granted_in_part: Motion partially granted
- denied_in_part: Motion partially denied
- moot: Motion is moot
- continued: Hearing continued to a later date
- off_calendar: Matter taken off calendar
- submitted: Matter submitted for decision
- other: Does not fit any category above

Allowed motion_type values:
- msj: Motion for summary judgment
- msj_partial: Motion for summary adjudication / partial summary judgment
- mtd: Motion to dismiss
- mil: Motion in limine
- demurrer: Demurrer
- motion_to_compel: Motion to compel (discovery, arbitration, etc.)
- motion_to_strike: Motion to strike
- anti_slapp: Anti-SLAPP motion (CCP 425.16)
- preliminary_injunction: Preliminary injunction
- other: Does not fit any category above

Respond with ONLY a JSON object, no other text:
{"outcome": "...", "motion_type": "...", "confidence": 0.XX}"""


@dataclass(frozen=True)
class Classification:
    """Result of classifying a ruling's outcome and motion type."""

    outcome: str
    motion_type: str
    confidence: float


class RulingClassifier:
    """Classifies ruling outcomes and motion types using Claude Haiku.

    Uses the Anthropic API to classify tentative rulings into structured
    outcome and motion type categories. Low-confidence results (< 0.7)
    should be flagged for human review in a future review queue.
    """

    def __init__(self, api_key: str) -> None:
        """Initialize the classifier with an Anthropic API key.

        Args:
            api_key: Anthropic API key for Claude Haiku access.
        """
        self._client = anthropic.Anthropic(api_key=api_key)

    def classify(self, ruling_text: str) -> Classification:
        """Classify a ruling's outcome and motion type.

        Args:
            ruling_text: The full text of a tentative ruling.

        Returns:
            A Classification with outcome, motion_type, and confidence.

        Raises:
            ValueError: If the ruling text is empty or the API returns
                invalid values outside the allowed sets.
        """
        if not ruling_text.strip():
            raise ValueError("ruling_text must not be empty")

        response = self._client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Classify this tentative ruling:\n\n{ruling_text}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        parsed = json.loads(raw_text)

        outcome = parsed["outcome"]
        motion_type = parsed["motion_type"]
        confidence = float(parsed["confidence"])

        if outcome not in OUTCOME_VALUES:
            raise ValueError(
                f"Invalid outcome '{outcome}'. Must be one of: {sorted(OUTCOME_VALUES)}"
            )
        if motion_type not in MOTION_TYPE_VALUES:
            raise ValueError(
                f"Invalid motion_type '{motion_type}'. Must be one of: {sorted(MOTION_TYPE_VALUES)}"
            )
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {confidence}")

        return Classification(
            outcome=outcome,
            motion_type=motion_type,
            confidence=confidence,
        )
