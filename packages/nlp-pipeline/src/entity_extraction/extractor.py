"""Entity extraction for California tentative rulings using Claude Haiku.

Extracts structured entities (judges, parties, attorneys, case numbers,
dates, monetary amounts, statute references) from ruling text. Part of
the Judgemind NLP pipeline (architecture spec Section 5.2).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import anthropic

_SYSTEM_PROMPT = (
    "You are a legal document entity extractor for "
    "California court tentative rulings.\n\n"
    "Given a tentative ruling text, extract these entities:\n\n"
    "1. **judge_name** - Canonical form: Last, First M. "
    "(e.g. Johnson, Robert M.). Null if not found.\n"
    "2. **case_number** - e.g. 23STCV12345. Null if not found.\n"
    "3. **department** - e.g. Dept. 68, Department 12. "
    "Null if not found.\n"
    "4. **hearing_date** - ISO 8601 (YYYY-MM-DD). "
    "Null if not found.\n"
    "5. **parties** - List of party names (plaintiffs, "
    "defendants, cross-complainants, etc.).\n"
    "6. **attorneys** - List of attorney names mentioned.\n"
    "7. **monetary_amounts** - List of amounts "
    '(e.g. "$5,000", "$1,250,000.00").\n'
    "8. **statute_refs** - List of statute references "
    '(e.g. "CCP § 437c", "Evidence Code § 1152").\n\n'
    "Rules:\n"
    "- For judge_name, use Last, First M. format. "
    "If only a last name is given, use just the last name.\n"
    "- For parties, extract actual names, not generic labels "
    'like "Plaintiff" or "Defendant".\n'
    "- For statute_refs, normalize to short form "
    '(e.g. "CCP § 437c" not '
    '"Code of Civil Procedure section 437c").\n'
    "- Return empty lists (not null) for parties, attorneys, "
    "monetary_amounts, and statute_refs if none found.\n"
    "- Only extract entities clearly present in the text. "
    "Do not guess or infer.\n\n"
    "Respond with ONLY a JSON object, no other text:\n"
    '{"judge_name": "...", "case_number": "...", '
    '"department": "...", "hearing_date": "...", '
    '"parties": [...], "attorneys": [...], '
    '"monetary_amounts": [...], "statute_refs": [...]}'
)


@dataclass(frozen=True)
class ExtractedEntities:
    """Entities extracted from a tentative ruling."""

    judge_name: str | None = None
    case_number: str | None = None
    department: str | None = None
    hearing_date: str | None = None  # ISO 8601 or None
    parties: list[str] = field(default_factory=list)
    attorneys: list[str] = field(default_factory=list)
    monetary_amounts: list[str] = field(default_factory=list)
    statute_refs: list[str] = field(default_factory=list)


class EntityExtractor:
    """Extracts structured entities from tentative ruling text using Claude Haiku.

    Uses the Anthropic API to identify and normalize judges, attorneys,
    parties, case numbers, dates, monetary amounts, and statute references
    from ruling text. Results are cached downstream in the entity graph.
    """

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize the extractor with an Anthropic API key.

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

    def extract(self, ruling_text: str) -> ExtractedEntities:
        """Extract entities from a tentative ruling.

        Args:
            ruling_text: The full text of a tentative ruling.

        Returns:
            An ExtractedEntities dataclass with all identified entities.

        Raises:
            ValueError: If the ruling text is empty.
        """
        if not ruling_text.strip():
            raise ValueError("ruling_text must not be empty")

        response = self._client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Extract entities from this tentative ruling:\n\n{ruling_text}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        parsed = json.loads(raw_text)

        return ExtractedEntities(
            judge_name=parsed.get("judge_name"),
            case_number=parsed.get("case_number"),
            department=parsed.get("department"),
            hearing_date=parsed.get("hearing_date"),
            parties=parsed.get("parties", []),
            attorneys=parsed.get("attorneys", []),
            monetary_amounts=parsed.get("monetary_amounts", []),
            statute_refs=parsed.get("statute_refs", []),
        )
