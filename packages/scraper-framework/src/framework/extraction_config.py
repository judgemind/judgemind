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
# San Bernardino-specific prompt (validated in eval #1961)
# ---------------------------------------------------------------------------

SAN_BERNARDINO_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from San Bernardino County Superior Court.\n\n"
    "You will receive the full text extracted from a PDF containing "
    "tentative rulings.  Your job is to identify EVERY individual "
    "case ruling in the document and extract structured data for each.\n\n"
    "## San Bernardino Document Format\n\n"
    "San Bernardino PDFs have this structure:\n"
    "1. **Header**: One of two formats:\n"
    "   - 'TENTATIVE RULING[S] FOR [date/case]' followed by "
    "'Department {CODE} - Judge {Name}' and standard boilerplate.\n"
    "   - 'TENTATIVE RULINGS FOR DEPT. {CODE} [DATE]' followed by "
    "'BEFORE THE HONORABLE {NAME}' (all-caps format used by some "
    "departments like S36).\n"
    "2. **Case entries**: Unlike Riverside (numbered entries), "
    "San Bernardino cases are separated by horizontal rules "
    "(underscores ____________) or by repeated headers. Each case "
    "contains:\n"
    "   - Case number (e.g., CIVSB2419120, CIVRS2502080, "
    "CIVSB 2600093)\n"
    "   - Case title / party names (e.g., 'LORENZO SOLIS v. GENERAL "
    "MOTORS LLC')\n"
    "   - Motion description\n"
    "   - Ruling text with legal analysis\n"
    "3. **Multi-case PDFs**: Some PDFs contain multiple cases for "
    "the same department and hearing date.  Each case starts with "
    "its own case number and title, often preceded by a horizontal "
    "rule or a repeated header block.  Count each distinct case "
    "number as a separate ruling.\n"
    "4. **Single-case PDFs with multiple motions**: Some cases "
    "have multiple related motions (e.g., 7 motions to compel, "
    "2 motions to set aside default).  These are ONE ruling because "
    "they share the same case number.  The ruling_text should "
    "include the full text covering ALL motions for that case.\n"
    "5. **Two-layer structure for substantive motions**: Like "
    "Riverside, San Bernardino PDFs may have:\n"
    "   - A brief disposition summary (e.g., 'Motion for Summary "
    "Adjudication is denied without prejudice')\n"
    "   - A FULL legal analysis that follows, often spanning MULTIPLE "
    "PAGES with legal standards, case citations, and detailed "
    "reasoning.\n"
    "   The ruling_text MUST include BOTH the disposition AND the "
    "full analysis.  Do NOT truncate.\n"
    "6. **Page footers**: Strip 'Page | N' and 'Page N of M' footers "
    "from ruling text.\n\n"
    "## Case Number Formats\n\n"
    "San Bernardino case numbers use these patterns:\n"
    "- CIV + location code + digits: CIVRS2502080, CIVSB2416631\n"
    "- Location codes: RS=Rancho Cucamonga, SB=San Bernardino\n"
    "- Some departments insert a space: 'CIVSB 2600093' — normalize "
    "to 'CIVSB2600093' (remove internal spaces).\n\n"
    "## Rules\n\n"
    "1. Count and return one ruling per distinct case number.  "
    "Multiple motions under the same case number are ONE ruling.\n"
    "2. Extract the case number EXACTLY as it appears (but remove "
    "any internal spaces — 'CIVSB 2600093' becomes 'CIVSB2600093').\n"
    "3. For case_title, use 'Plaintiff v. Defendant' format.  "
    "Convert ALL-CAPS titles to title case.\n"
    "4. For ruling_text, include the COMPLETE ruling text — the "
    "disposition AND the full legal analysis.  Include ALL pages "
    "of analysis.  Do NOT truncate or summarize.  Preserve the text "
    "VERBATIM (but strip page footers).\n"
    "5. Skip the header boilerplate (appearance instructions, phone "
    "numbers, URLs, pandemic notices, etc.) — only extract from "
    "the case content.\n"
    "6. For judge_name, extract the judge's full name.  If the "
    "header uses ALL-CAPS ('BEFORE THE HONORABLE JOSEPH WIDMAN'), "
    "convert to title case ('Joseph Widman').\n\n"
    "## Parties\n\n"
    "Extract plaintiff(s) and defendant(s) from the case caption. "
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
    "For 'denied without prejudice', map to 'denied'.\n"
    "For cases where the motion is MOOT but sanctions are awarded, "
    "use 'moot'.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "R12" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "CIVRS2502080" or null,\n'
    '      "extracted_case_title": "Carmell v. Genus-Robinson-Haywood" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "moot" or null,\n'
    '      "motion_type": "compel" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Carmell", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Genus-Robinson-Haywood", "role": "defendant", "confidence": "high"}\n'
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
# San Francisco-specific prompt (validated in eval #1965)
# ---------------------------------------------------------------------------

SAN_FRANCISCO_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from San Francisco Superior Court, "
    "Unified Family Court.\n\n"
    "You will receive the full text extracted from a PDF containing "
    "tentative rulings.  Your job is to identify EVERY individual "
    "case ruling in the document and extract structured data for each.\n\n"
    "## San Francisco Family Law Document Format\n\n"
    "San Francisco Family Law PDFs have this structure:\n"
    "1. **Cover pages** (first 1-2 pages): Contain 'Important Information "
    "for Tentative Rulings and Hearings' boilerplate, contact info for "
    "Departments 403/404/416, remote appearance instructions, Zoom details, "
    "etc. SKIP these pages entirely — they contain no rulings.\n"
    "2. **Case entries**: Each ruling starts with a full court header:\n"
    "   ```\n"
    "   SUPERIOR COURT OF CALIFORNIA\n"
    "   COUNTY OF SAN FRANCISCO\n"
    "   UNIFIED FAMILY COURT\n"
    "   ```\n"
    "   Followed by a structured caption block with parentheses:\n"
    "   ```\n"
    "   PETITIONER NAME,          ) Case Number: FPT-25-378624\n"
    "       Petitioner             ) Hearing Date: March 3, 2026\n"
    "   VS.                        ) Hearing Time: 9:00 AM\n"
    "   RESPONDENT NAME,           ) Department: 403\n"
    "       Respondent             ) Presiding: BOBBY P. LUNA\n"
    "   ```\n"
    "3. **Motion description**: After the caption, a line describes the "
    "request type (e.g., 'REQUEST FOR ORDER FOR CHANGE OF CHILD CUSTODY, "
    "ATTORNEY'S FEES AND COSTS, MOVE-AWAY; PASSPORT FOR CHILD').\n"
    "4. **TENTATIVE RULING** heading, then the ruling body with sections:\n"
    "   - A. Procedural History\n"
    "   - B. Findings and Order\n"
    "5. **Multi-case PDFs**: Most PDFs contain many cases (10+) for the "
    "same department and hearing date.  Each case starts with its own "
    "full court header and caption block.  Count each distinct case "
    "number as a separate ruling.\n\n"
    "## Case Number Formats\n\n"
    "San Francisco Family Law case numbers follow this pattern:\n"
    "- F + 2 uppercase letters + hyphen + 2-digit year + hyphen + 6 digits\n"
    "- Examples: FPT-25-378624, FMS-20-387302, FDI-14-781786, "
    "FDV-21-815942\n"
    "- Prefix codes: FPT (paternity), FMS (family motion), FDI (dissolution), "
    "FDV (domestic violence)\n"
    "- Extract case numbers EXACTLY as they appear in the document.\n\n"
    "## Rules\n\n"
    "1. Count and return one ruling per distinct case number.  "
    "If a ruling mentions a related/consolidated case number, it is still "
    "ONE ruling — use the primary case number from the caption.\n"
    "2. Extract the case number EXACTLY as printed (including hyphens).\n"
    "3. For case_title, use 'Petitioner v. Respondent' format.  "
    "Convert ALL-CAPS names to title case (e.g., 'MICHAEL EDWARD GRAVES' "
    "becomes 'Graves' for the short title).\n"
    "4. For ruling_text, include the COMPLETE ruling text — both "
    "Procedural History and Findings and Order sections.  Include ALL "
    "pages of the ruling.  Do NOT truncate or summarize.  Preserve the "
    "text VERBATIM.\n"
    "5. Skip the cover page boilerplate (Important Information, remote "
    "appearance instructions, Zoom details, phone numbers, URLs) — only "
    "extract from the case content.\n"
    "6. For judge_name, extract from the 'Presiding:' line.  Convert "
    "ALL-CAPS to title case ('BOBBY P. LUNA' becomes 'Bobby P. Luna').\n"
    "7. For motion_type, extract from the line(s) between the caption "
    "and the 'TENTATIVE RULING' heading.  Keep the full description.\n\n"
    "## Parties\n\n"
    "This is Family Court — parties are petitioner and respondent, "
    "NOT plaintiff and defendant.  Extract from the caption block:\n"
    "Each party is "
    '{"name": "...", "role": "petitioner", "confidence": "high"} or '
    '{"name": "...", "role": "respondent", "confidence": "high"}.\n'
    "Use the full name from the caption (title case), e.g., "
    "'Michael Edward Graves' for petitioner, 'Ranjie Long' for respondent.\n\n"
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted — motion/request was fully granted\n"
    "- denied — motion/request was fully denied\n"
    "- granted_in_part — some requests granted, some denied or modified\n"
    "- denied_in_part — partially denied\n"
    "- moot — motion is moot\n"
    "- continued — hearing was postponed/continued to a future date\n"
    "- off_calendar — hearing removed from calendar\n"
    "- submitted — taken under submission\n"
    "- other — none of the above fit\n\n"
    "For family law rulings with multiple sub-requests where some are "
    "granted and some denied, use 'granted_in_part'.\n"
    "When the matter is continued to a future hearing date with NO "
    "substantive ruling on the merits, use 'continued'.\n"
    "When the court orders parties to appear for discussion without "
    "granting or denying, use 'other'.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "403" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "FPT-25-378624" or null,\n'
    '      "extracted_case_title": "Graves v. Long" or null,\n'
    '      "case_type": "family" or null,\n'
    '      "outcome": "granted_in_part" or null,\n'
    '      "motion_type": "Request for Order for Change of Child Custody" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Michael Edward Graves", "role": "petitioner", "confidence": "high"},\n'
    '        {"name": "Ranjie Long", "role": "respondent", "confidence": "high"}\n'
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
    ("CA", "SAN BERNARDINO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SAN_BERNARDINO_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "SAN FRANCISCO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SAN_FRANCISCO_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
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
