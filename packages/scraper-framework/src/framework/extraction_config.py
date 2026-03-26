"""County-specific extraction configuration.

Maps (state, county) pairs to extraction methods and LLM parameters.
Counties not in the registry use the default framework-level
``EXTRACTION_SYSTEM_PROMPT`` with the Anthropic provider.

The ``ExtractionMethod`` enum determines *how* a document is extracted:

- **LLM** — Use the framework ``LlmExtractor`` with the configured
  (or default) system prompt, provider, and model.  This is the
  standard path for all counties.
- **MULTIMODAL** — Use ``LlmExtractor.extract_from_pdf()`` with
  per-page image extraction.  For tabular PDFs (e.g. OC) where
  text extraction is unreliable.
- **NONE** — No framework-level extraction.  The scraper handles
  everything (e.g. LA HTML scraper which does its own parsing).

When a county has a custom ``system_prompt``, the ``LlmExtractor``
uses that prompt instead of the generic ``EXTRACTION_SYSTEM_PROMPT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExtractionMethod(StrEnum):
    """How the framework extracts structured data from a document."""

    LLM = "llm"
    MULTIMODAL = "multimodal"
    NONE = "none"


@dataclass(frozen=True)
class CountyExtractionConfig:
    """Extraction configuration for a single county.

    Attributes:
        method: How to extract structured data.
        system_prompt: Custom system prompt for the LLM.  If ``None``,
            the default ``EXTRACTION_SYSTEM_PROMPT`` is used.
        provider: LLM provider (``"anthropic"`` or ``"google"``).
            If ``None``, uses the framework default (Anthropic).
        model: LLM model ID.  If ``None``, uses the provider default.
        max_output_tokens: Maximum tokens in the model response.
            If ``None``, uses the ``LlmExtractor`` default (4096).
    """

    method: ExtractionMethod = ExtractionMethod.LLM
    system_prompt: str | None = None
    provider: str | None = None
    model: str | None = None
    max_output_tokens: int | None = None


# ---------------------------------------------------------------------------
# Riverside-specific prompt (validated in eval #1718, 100% accuracy)
# ---------------------------------------------------------------------------

RIVERSIDE_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Riverside County Superior Court.\n\n"
    "You will receive the full text extracted from a PDF containing "
    "tentative rulings.  Your job is to identify EVERY individual "
    "case ruling in the document and extract structured data for each.\n\n"
    "## Riverside Document Format\n\n"
    "Riverside PDFs have this structure:\n"
    "1. **Header**: 'Tentative Rulings for [date]' followed by "
    "department and judge information, plus standard boilerplate "
    "about oral arguments and telephonic appearances.\n"
    "2. **Numbered entries**: Each case starts with a number on its "
    "own line (e.g., '1.', '2.', '3.'), followed by:\n"
    "   - Case number (e.g., CVPS2306157, CVMV2507098, RIC1904113)\n"
    "   - Party names (e.g., 'YELDELL vs HENSS')\n"
    "   - Motion description (e.g., 'Hearing re: Demurrer on 1st "
    "Amended Complaint')\n"
    "   - 'Tentative Ruling:' followed by the ruling text\n"
    "3. **IMPORTANT — Two-layer structure**: Riverside PDFs have a "
    "TWO-LAYER structure for substantive motions (MSJ, demurrers, "
    "motions to strike, etc.):\n"
    "   - **Layer 1 (calendar table)**: A brief disposition summary "
    "line after 'Tentative Ruling:', e.g., 'DENY Defendant's "
    "Motion for Summary Judgment' — typically one sentence.\n"
    "   - **Layer 2 (detailed analysis)**: The judge's FULL legal "
    "analysis follows below the summary, often spanning MULTIPLE "
    "PAGES. This includes: legal standards (e.g., CCP section "
    "437c), analysis of burden of proof, discussion of evidence, "
    "case law citations (e.g., Aguilar v. Atlantic Richfield, "
    "Byrne v. Laura), and detailed reasoning.\n"
    "   The ruling_text MUST include BOTH layers — the brief "
    "disposition AND the full analysis. The detailed analysis is "
    "the most valuable content. Capturing only the disposition "
    "summary line is WRONG. A substantive ruling (MSJ, demurrer) "
    "that is only one or two sentences long is almost certainly "
    "truncated — look for the full analysis that follows.\n"
    "4. **Cross-references**: Some entries may reference another entry "
    "with phrases like 'See #1 Above', 'See No. 3 above', or 'Same "
    "as #2'. These are SEPARATE entries that must be counted "
    "individually — they are distinct cases even though they share "
    "ruling text.\n"
    "5. **Page breaks**: Rulings may span multiple pages. 'Page N of M' "
    "footers appear at the bottom of each page.\n"
    "6. **No tentative rulings**: Some PDFs contain only 'No Tentative "
    "Rulings for [date]' or 'No Tentative Rulings [date]' with "
    "boilerplate text. These have zero cases.\n\n"
    "## Case Number Formats\n\n"
    "Riverside case numbers use these patterns:\n"
    "- CV + location code + year + sequence: CVPS2306157, CVMV2507098, "
    "CVRI2403055\n"
    "- Location prefix + sequence: RIC1904113, MCC2012345, PSC2112345\n"
    "- Location codes: PS=Palm Springs, MV=Moreno Valley, M=Murrieta, "
    "RI=Riverside, C=Corona\n\n"
    "## Rules\n\n"
    "1. Count and return EVERY numbered entry as a separate ruling. "
    "If the document has entries 1 through 4, return 4 rulings.\n"
    "2. Cross-reference entries ('See #N Above') are their OWN rulings "
    "with their OWN case number — do NOT skip them or merge them.\n"
    "3. Extract the case number EXACTLY as it appears.\n"
    "4. For case_title, use 'Plaintiff v. Defendant' format.\n"
    "5. For ruling_text, include the COMPLETE ruling text — the "
    "disposition summary AND the full legal analysis that follows "
    "it. Include ALL pages of analysis, legal standards, case "
    "citations, evidence discussion, and reasoning. Do NOT truncate "
    "or summarize. Preserve the text VERBATIM. A ruling_text under "
    "200 characters for a substantive motion (MSJ, demurrer, motion "
    "to strike) is almost certainly incomplete.\n"
    "6. Skip the header boilerplate (oral argument instructions, "
    "phone numbers, URLs, etc.) — only extract from the numbered "
    "entries.\n"
    "7. 'No Tentative Rulings' documents have zero cases — return an "
    "empty rulings array.\n"
    "8. Strip 'Page N of M' footers from ruling text.\n\n"
    "## Parties\n\n"
    "Extract plaintiff(s) and defendant(s) from the case caption line. "
    "Each party is "
    '{"name": "...", "role": "plaintiff", "confidence": "high"} or '
    '{"name": "...", "role": "defendant", "confidence": "high"}.\n\n'
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted — motion was fully granted\n"
    "- denied — motion was fully denied\n"
    "- granted_in_part — partially granted and partially denied\n"
    "- denied_in_part — partially denied\n"
    "- moot — motion is moot\n"
    "- continued — hearing was postponed\n"
    "- off_calendar — hearing removed from calendar\n"
    "- submitted — taken under submission\n"
    "- other — none of the above fit\n\n"
    "For 'overruled' (demurrers), map to 'denied'.\n"
    "For 'sustained' (demurrers), map to 'granted'.\n"
    "For 'No tentative ruling, a hearing will be conducted', use 'other'.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "PS1" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "CVPS2306157" or null,\n'
    '      "extracted_case_title": "Yeldell v. Henss" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "denied" or null,\n'
    '      "motion_type": "demurrer" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Yeldell", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Henss", "role": "defendant", "confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "judge": "high",\n'
    '        "ruling_text": "high",\n'
    '        "outcome": "high"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}"
)


# ---------------------------------------------------------------------------
# County extraction registry
# ---------------------------------------------------------------------------

# Key: (state, county) tuple, both uppercase for consistent lookup.
_COUNTY_CONFIGS: dict[tuple[str, str], CountyExtractionConfig] = {
    ("CA", "RIVERSIDE"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=RIVERSIDE_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "ORANGE"): CountyExtractionConfig(
        method=ExtractionMethod.MULTIMODAL,
    ),
}


def get_county_extraction_config(
    state: str,
    county: str,
) -> CountyExtractionConfig | None:
    """Look up the extraction config for a (state, county) pair.

    Returns ``None`` if no custom configuration exists — the caller
    should fall back to the default framework extraction behaviour.

    Parameters
    ----------
    state : str
        Two-letter state code (e.g. ``"CA"``).
    county : str
        County name (e.g. ``"Riverside"``).

    Returns
    -------
    CountyExtractionConfig | None
        The county-specific config, or ``None`` if not registered.
    """
    return _COUNTY_CONFIGS.get((state.upper(), county.upper()))
