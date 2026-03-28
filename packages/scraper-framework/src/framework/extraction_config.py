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
    "4. For case_title, use full party names in 'Plaintiff v. Defendant' "
    "format.  Riverside captions only show last names (e.g., 'YELDELL vs "
    "HENSS'), but the motion description often contains full names (e.g., "
    "'of LACHON YELDELL', 'by JOHN W. IRWIN').  When a full name appears "
    "anywhere in the entry, use it.  Convert ALL-CAPS to title case.  "
    "If only a last name is available, use the last name.\n"
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
    '      "extracted_case_title": "Lachon Yeldell v. Henss" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "denied" or null,\n'
    '      "motion_type": "demurrer" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Lachon Yeldell", "role": "plaintiff", "confidence": "high"},\n'
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
    "3. For case_title, use full party names in 'Plaintiff v. Defendant' "
    "format.  San Bernardino captions often use full names (e.g., "
    "'LORENZO SOLIS v. GENERAL MOTORS LLC').  Convert ALL-CAPS to title "
    "case.  When the caption only shows last names but the Movant/Respondent "
    "lines or ruling body contain full names, use the full names.\n"
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
    '      "extracted_case_title": "Angela Carmell v. '
    'Kathleen Janet Genus-Robinson-Haywood" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "moot" or null,\n'
    '      "motion_type": "compel" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Angela Carmell", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Kathleen Janet Genus-Robinson-Haywood", '
    '"role": "defendant", "confidence": "high"}\n'
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
    "Convert ALL-CAPS names to title case.  Use full names "
    "(e.g., 'MICHAEL EDWARD GRAVES' becomes 'Michael Edward Graves').\n"
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
    '      "extracted_case_title": "Michael Edward Graves v. Ranjie Long" or null,\n'
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
# Fresno-specific prompt (validated in eval #1964)
# ---------------------------------------------------------------------------

FRESNO_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Fresno County Superior Court.\n\n"
    "You will receive the full text extracted from a PDF containing "
    "tentative rulings for one department.  Your job is to identify "
    "EVERY individual case ruling in the document and extract "
    "structured data for each.\n\n"
    "## Fresno Document Format\n\n"
    "Fresno PDFs have this structure:\n"
    "1. **Cover page**: Title 'Tentative Rulings for [Date]' and "
    "'Department [NNN]', followed by boilerplate instructions about "
    "oral argument, appearances, continued cases, and a note "
    "'(Tentative Rulings begin at the next page)'.\n"
    "2. **Department title page**: 'Tentative Rulings for Department "
    "[NNN]' followed by 'Begin at the next page'.\n"
    "3. **Numbered rulings**: Each ruling starts with a number in "
    "parentheses followed by 'Tentative Ruling' as a header:\n"
    "   ```\n"
    "   (20)              Tentative Ruling\n"
    "   Re:               Lopez v. Fresno Unified School District\n"
    "                     Superior Court Case No. 25CECG03271\n"
    "   Hearing Date:     March 10, 2026 (Dept. 403)\n"
    "   Motion:           Demurrer to First Amended Complaint\n"
    "   ```\n"
    "4. **Ruling body**: After the header, the ruling text starts with "
    "'Tentative Ruling:' followed by the disposition (e.g., "
    "'To sustain the demurrers...'), then optionally an "
    "'Explanation:' section with detailed legal analysis.\n"
    "5. **Signature block**: Each ruling ends with:\n"
    "   ```\n"
    "   Tentative Ruling\n"
    "   Issued By:    [initials]    on    [date]\n"
    "                 (Judge's initials)  (Date)\n"
    "   ```\n"
    "6. **Multi-page rulings**: Some rulings span many pages "
    "(e.g., a PAGA settlement analysis can span 7+ pages). "
    "The ruling_text MUST include ALL pages of the ruling. "
    "Do NOT truncate.\n"
    "7. **Multiple motions per case**: Some cases list multiple "
    "motions (e.g., 'Motions (x2): Motion 1; Motion 2'). "
    "These are ONE ruling because they share the same case number.\n"
    "8. **Continued cases**: The cover page may list cases that "
    "have been continued to a different date. These are NOT "
    "tentative rulings -- skip them entirely.\n\n"
    "## Case Number Formats\n\n"
    "Fresno case numbers use these patterns:\n"
    "- Standard format: YYCECGNNNNN (e.g., 25CECG03271, 23CECG00266)\n"
    "- Sometimes written as 'Superior Court Case No. 25CECG03271' "
    "or 'Case No. 25CECG03271' or 'Court Case No. 23CECG03612'\n"
    "- Extract the case number in its standard format without any "
    "prefix text.\n\n"
    "## Rules\n\n"
    "1. Count and return one ruling per numbered entry (the number "
    "in parentheses like (20), (47), (37), etc.). Multiple motions "
    "under the same numbered entry are ONE ruling.\n"
    "2. Extract the case number EXACTLY as printed (e.g., "
    "'25CECG03271', '23CECG00266'). Remove any prefix like "
    "'Superior Court Case No.' or 'Case No.'.\n"
    "3. For case_title, use the text after 'Re:' verbatim "
    "(e.g., 'Lopez v. Fresno Unified School District'). "
    "If the title is in italic or bold formatting, extract the "
    "plain text.\n"
    "4. For ruling_text, include the COMPLETE ruling text -- "
    "everything from 'Tentative Ruling:' through to the end of "
    "the ruling (just before the signature block). Include the "
    "full Explanation section. Do NOT truncate or summarize. "
    "Preserve the text VERBATIM.\n"
    "5. Skip cover page boilerplate, department title pages, "
    "and continued case listings. Only extract actual "
    "tentative rulings.\n"
    "6. For judge_name: Fresno PDFs only contain judge initials "
    "(e.g., 'lmg', 'DTT', 'KCK', 'JS') in the signature block, "
    "not full names. Set extracted_judge_name to null.\n"
    "7. For hearing_date, extract from the 'Hearing Date:' line "
    "in each ruling header.\n\n"
    "## Parties\n\n"
    "Extract plaintiff(s) and defendant(s) from the case title "
    "(the 'Re:' line). For 'X v. Y' format, X is plaintiff and "
    "Y is defendant. For 'In re: Name' or just a name (e.g., "
    "'Brody Peterson'), use role 'petitioner'. "
    'Each party is {"name": "...", "role": "plaintiff", '
    '"confidence": "high"} or '
    '{"name": "...", "role": "defendant", '
    '"confidence": "high"}.\n\n'
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted -- motion was fully granted\n"
    "- denied -- motion was fully denied\n"
    "- granted_in_part -- partially granted and partially denied\n"
    "- denied_in_part -- partially denied\n"
    "- moot -- motion is moot\n"
    "- continued -- hearing was postponed\n"
    "- off_calendar -- hearing removed from calendar (including "
    "'taken off calendar' and 'stayed pending appeal')\n"
    "- submitted -- taken under submission\n"
    "- other -- none of the above fit\n\n"
    "For 'overruled' (demurrers), map to 'denied'.\n"
    "For 'sustained' (demurrers), map to 'granted'.\n"
    "For demurrers that are partly sustained and partly overruled, "
    "map to 'granted_in_part'.\n"
    "For 'denied without prejudice', map to 'denied'.\n"
    "For motions 'taken off calendar', map to 'off_calendar'.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "403" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "25CECG03271" or null,\n'
    '      "extracted_case_title": "Lopez v. Fresno Unified School District" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "granted_in_part" or null,\n'
    '      "motion_type": "demurrer" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Lopez", "role": "plaintiff", '
    '"confidence": "high"},\n'
    '        {"name": "Fresno Unified School District", '
    '"role": "defendant", "confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "judge": "low",\n'
    '        "ruling_text": "high",\n'
    '        "outcome": "high"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}"
)

# ---------------------------------------------------------------------------
# Santa Clara-specific prompt (validated in eval #1962)
# ---------------------------------------------------------------------------

SANTA_CLARA_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Santa Clara County Superior Court.\n\n"
    "You will receive the full text extracted from a PDF containing "
    "tentative rulings for one department on one hearing day.  Your job "
    "is to identify EVERY individual case ruling and extract structured "
    "data for each.\n\n"
    "## Santa Clara Document Format\n\n"
    "Santa Clara PDFs have this structure:\n"
    "1. **Header block** (repeated on each page): Contains:\n"
    "   - 'SUPERIOR COURT, STATE OF CALIFORNIA'\n"
    "   - 'COUNTY OF SANTA CLARA'\n"
    "   - 'Department N'\n"
    "   - 'Honorable Firstname Lastname, Presiding'\n"
    "   - 'DATE: Month DD, YYYY' or just 'Month DD, YYYY'\n"
    "   - Boilerplate about contesting rulings, appearances, etc.\n"
    "2. **Summary table**: A table with columns LINE, CASE NO., CASE TITLE, "
    "TENTATIVE RULING.  Each row has:\n"
    "   - A time slot (e.g., '9:00', '9:01')\n"
    "   - A line number (e.g., '1', '2', '3-5')\n"
    "   - A case number (e.g., '24CV443183', '25PR199782')\n"
    "   - A case title (parties)\n"
    "   - Either a brief ruling or 'See Line N below for complete "
    "tentative ruling'\n"
    "3. **Detailed rulings**: For complex cases, the full ruling "
    "text appears after the summary table, headed by:\n"
    "   - 'Line N' (section header)\n"
    "   - 'Case Name: ...' and 'Case No.: ...'\n"
    "   - Full legal analysis spanning multiple pages\n\n"
    "## Case Splitting Rules\n\n"
    "**CRITICAL: Group by case number, not by LINE number.**\n"
    "1. Each distinct case number is ONE ruling, even if it appears "
    "on multiple LINE entries (e.g., Lines 3-4 for the same case, "
    "or Lines 6-7 addressing different motions in the same case).  "
    "For example, if LINE 1 and LINE 2 both have case number "
    "23CV419582 (e.g., two minor claimants in the same case), "
    "output ONE ruling with case_number '23CV419582'.\n"
    "2. If the same case number appears under different LINE entries "
    "with different motions (e.g., a motion to compel on Line 3 and "
    "a motion regarding interrogatories on Line 4), combine them "
    "into ONE ruling.  The ruling_text should include text from ALL "
    "motions for that case.  For outcome, use the primary outcome "
    "(if mixed, use 'granted_in_part').\n"
    "3. A case that appears in BOTH the summary table AND a detailed "
    "section below should use the DETAILED ruling text (not the "
    "summary table snippet).  The summary table entry is just a "
    "brief preview.\n"
    "4. Some PDFs have two time blocks (e.g., 9:01 and 9:00) with "
    "separate LINE numbering.  Treat these as part of the same "
    "document -- combine by case number across time blocks.\n\n"
    "## Case Number Formats\n\n"
    "Santa Clara case numbers use these patterns:\n"
    "- 2-digit year + CV or PR + 6 digits: 24CV443183, 25PR199782\n"
    "- CV = civil, PR = probate\n\n"
    "## Rules\n\n"
    "1. Count and return one ruling per distinct case number.\n"
    "2. Extract the case number EXACTLY as it appears.\n"
    "3. For case_title, use 'Plaintiff v. Defendant' format.  Use the "
    "names from the case caption or summary table.  Convert ALL-CAPS "
    "to title case if needed.\n"
    "4. For ruling_text, include the COMPLETE ruling text for the case.  "
    "If a detailed ruling exists below the summary table, use that "
    "(it is the full version).  If only the summary table entry "
    "exists, use that.  Include ALL pages of the ruling.  Do NOT "
    "truncate or summarize.  Preserve text VERBATIM (but strip page "
    "footers and repeated header blocks).\n"
    "5. Skip the header boilerplate (appearance instructions, phone "
    "numbers, URLs, recording prohibitions, etc.) -- only extract "
    "from the case content.\n"
    "6. For judge_name, extract from 'Honorable Firstname Lastname, "
    "Presiding' in the header.  Use proper case.\n"
    "7. For department, extract the number from 'Department N' in the "
    "header.\n"
    "8. For hearing_date, extract from 'DATE: Month DD, YYYY' or the "
    "standalone date line.\n\n"
    "## Parties\n\n"
    "Extract plaintiff(s) and defendant(s) from the case caption or "
    "summary table.  Each party is "
    '{"name": "...", "role": "plaintiff", "confidence": "high"} or '
    '{"name": "...", "role": "defendant", "confidence": "high"}.\n'
    "Use proper case (title case), not ALL CAPS.  For 'et al.' parties, "
    "include only the named party, not 'et al.'.\n\n"
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted -- motion was fully granted\n"
    "- denied -- motion was fully denied\n"
    "- granted_in_part -- partially granted and partially denied\n"
    "- denied_in_part -- partially denied\n"
    "- moot -- motion is moot\n"
    "- continued -- hearing was postponed\n"
    "- off_calendar -- hearing removed from calendar\n"
    "- submitted -- taken under submission\n"
    "- other -- none of the above fit (e.g., 'cases transferred')\n\n"
    "For 'overruled' (demurrers), map to 'denied'.\n"
    "For 'sustained' (demurrers), map to 'granted'.\n"
    "For 'denied without prejudice', map to 'denied'.\n"
    "For 'conditionally granted', map to 'granted'.\n\n"
    "## Motion type labels\n\n"
    "Use a short descriptive label.  Common values:\n"
    "demurrer, msj, motion_to_compel, motion_to_dismiss, "
    "motion_to_strike, writ_of_attachment, tro, "
    "preliminary_injunction, compromise_of_minor_claim, "
    "motion_to_be_relieved_as_counsel, pro_hac_vice, "
    "confirm_arbitration, terminating_sanctions, "
    "motion_to_amend, motion_for_attorney_fees, other.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "1" or "6" or "16" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "24CV443183" or null,\n'
    '      "extracted_case_title": "Huynh v. Redis Labs" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "off_calendar" or null,\n'
    '      "motion_type": "other" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Huynh", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Redis Labs", "role": "defendant", "confidence": "high"}\n'
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
# Ventura-specific prompt (validated in eval #1966)
# ---------------------------------------------------------------------------

VENTURA_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Ventura County Superior Court.\n\n"
    "You will receive the text of a SINGLE ruling document for one case.  "
    "Unlike multi-case PDFs, each Ventura document covers exactly one case.  "
    "You also receive the case_number and motion_type as known context "
    "(already extracted from the court's search results table).\n\n"
    "Your job is to extract three fields that are NOT yet available from "
    "the table:\n"
    "  1. outcome\n"
    "  2. parties (with roles)\n"
    "  3. ruling_text (trimmed to the substantive ruling content)\n\n"
    "## Ventura Document Format\n\n"
    "Ventura ruling documents (HTML or PDF) typically have this structure:\n"
    "1. **Header**: Court name, county, department number\n"
    "2. **Case info block**: Case number, hearing date, party names in "
    "ALL-CAPS (e.g., 'MARTINEZ CONSTRUCTION, INC. vs. GREENFIELD "
    "PROPERTIES, LLC'), nature of proceedings\n"
    "3. **TENTATIVE RULING heading**: The ruling disposition in the first "
    "paragraph (e.g., 'is GRANTED', 'is DENIED', 'is SUSTAINED WITH "
    "LEAVE TO AMEND')\n"
    "4. **BACKGROUND section**: Facts and procedural history\n"
    "5. **ANALYSIS section**: Legal reasoning\n"
    "6. **Standard footer**: 'If this tentative ruling is not contested, "
    "it shall become the order of the court.'\n\n"
    "## Party Extraction\n\n"
    "Party names appear in the case info block, typically in ALL-CAPS.  "
    "Convert to title case.  Determine roles from context:\n"
    "- Civil cases: plaintiff and defendant (look for 'vs.' separator)\n"
    "- Probate/estate cases: petitioner (and sometimes respondent)\n"
    "- 'IN THE MATTER OF' cases: the named person is the subject, the "
    "filing party is the petitioner\n"
    "- 'et al.' indicates additional parties -- extract the named party "
    "and note 'et al.' in the name if present\n"
    "- Use the FULL party name from the document (company names, etc.)\n\n"
    "## Outcome Taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted -- motion/petition was fully granted\n"
    "- denied -- motion was fully denied (includes 'overruled' for demurrers)\n"
    "- granted_in_part -- some parts granted, some denied (includes "
    "'sustained in part' for demurrers, or mixed rulings on multiple "
    "issues)\n"
    "- denied_in_part -- partially denied\n"
    "- moot -- motion is moot\n"
    "- continued -- hearing was postponed/continued to a future date\n"
    "- off_calendar -- hearing removed from calendar\n"
    "- submitted -- taken under submission\n"
    "- other -- none of the above fit\n\n"
    "Mapping rules:\n"
    "- 'OVERRULED' (demurrer) maps to 'denied'\n"
    "- 'SUSTAINED' (demurrer) maps to 'granted'\n"
    "- 'SUSTAINED WITH LEAVE TO AMEND' maps to 'granted' (the demurrer "
    "succeeded)\n"
    "- 'SUSTAINED in part, OVERRULED in part' or mixed rulings on "
    "multiple causes of action maps to 'granted_in_part'\n"
    "- 'GRANTED WITHOUT LEAVE TO AMEND' on some parts and 'DENIED' on "
    "others maps to 'granted_in_part'\n\n"
    "## Ruling Text\n\n"
    "For ruling_text, include the substantive ruling content starting "
    "from the TENTATIVE RULING heading through the end of the analysis.  "
    "EXCLUDE:\n"
    "- The court header (court name, county, department)\n"
    "- The case info block (case number, hearing date, party names line)\n"
    "- The standard footer ('If this tentative ruling is not contested...')\n"
    "Include the ruling disposition, background, analysis, and any orders.\n\n"
    "## Output Format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "outcome": "granted" or "denied" or "granted_in_part" or null,\n'
    '  "case_title": "Plaintiff v. Defendant" or '
    '"In the Matter of ..." or null,\n'
    '  "parties": [\n'
    '    {"name": "Full Name", "role": "plaintiff", '
    '"confidence": "high"},\n'
    '    {"name": "Full Name", "role": "defendant", '
    '"confidence": "high"}\n'
    "  ],\n"
    '  "ruling_text": "Full ruling text starting from TENTATIVE RULING...",\n'
    '  "confidence": {\n'
    '    "outcome": "high",\n'
    '    "parties": "high",\n'
    '    "ruling_text": "high"\n'
    "  }\n"
    "}"
)

# ---------------------------------------------------------------------------
# Contra Costa-specific prompt (validated in eval #2085, consolidated #2093)
# ---------------------------------------------------------------------------

CONTRA_COSTA_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Contra Costa County Superior Court.\n\n"
    "You will receive the full text extracted from a PDF containing "
    "tentative rulings for one department.  Your job is to identify "
    "EVERY individual case entry in the document and extract "
    "structured data for each.\n\n"
    "## Contra Costa Document Format\n\n"
    "Contra Costa PDFs have two distinct formats:\n\n"
    "### Format A: Civil departments (e.g., Dept 14, 16)\n"
    "1. **Header**: Court name, location, department number, "
    "judicial officer name, and hearing date, followed by "
    "boilerplate instructions about contesting the tentative.\n"
    "2. **Numbered case entries**: Each case starts with a numbered "
    "line like:\n"
    "   `1. 9:00 AM CASE NUMBER: L23-06679`\n"
    "   followed by:\n"
    "   - `CASE NAME: DISCOVER BANK VS. GERALD GILCHRIST`\n"
    "   - `*HEARING ON MOTION IN RE: ...` (the motion description)\n"
    "   - `FILED BY: ...`\n"
    "   - `*TENTATIVE RULING:*` followed by the ruling text\n"
    "3. **Sub-sections**: Some departments have section headers "
    "like 'Law & Motion', 'Courtroom Clerk's Session', "
    "'Discovery Law & Motion' that divide the calendar.\n"
    "4. **Same case, multiple motions**: A single case number may "
    "appear on multiple lines (e.g., lines 7, 8, 9 all for "
    "C23-02436). Each line is a SEPARATE ruling entry because "
    "each addresses a different motion.\n\n"
    "### Format B: Probate departments (e.g., Dept 30)\n"
    "1. **Header**: Court name, location (PR - MARTINEZ-WAKEFIELD "
    "TAYLOR COURTHOUSE), calendar date, department, judicial "
    "officer.\n"
    "2. **Numbered entries**: Each case starts with:\n"
    "   `1. N25-2307 IN THE MATTER OF: AJAY BHALLA`\n"
    "   or `5. MSP19-01440 CONS. OF MARIE ROST`\n"
    "   or `7. P24-00230 CONSERVATORSHIP OF: JUNE STONE`\n"
    "   followed by:\n"
    "   - Hearing time and type\n"
    "   - Examiner notes / ruling text\n"
    "3. **Sub-entries**: Some entries have A/B sub-entries "
    "(e.g., 17A and 17B for the same case number with different "
    "petitions, or 22A and 22B). Each sub-entry is a SEPARATE "
    "ruling entry.\n"
    "4. **Page headers repeat**: The court header block repeats "
    "at the top of every page in probate PDFs. Ignore these "
    "repeated headers.\n\n"
    "## Case Number Formats\n\n"
    "Contra Costa uses several case number formats:\n"
    "- `C##-#####` -- civil unlimited (e.g., C24-02490, C23-00908)\n"
    "- `L##-#####` -- limited civil (e.g., L23-06679, L25-01552)\n"
    "- `N##-####` -- name change / probate (e.g., N25-2307)\n"
    "- `P##-#####` -- probate (e.g., P23-01484, P26-00022)\n"
    "- `RS##-####` -- Rossmoor/Richmond (e.g., RS24-0953)\n"
    "- `A##-#####` -- adoption (e.g., A26-00006)\n"
    "- `MS####` -- miscellaneous (e.g., MS5031)\n"
    "- `MSP##-#####` -- miscellaneous probate (e.g., MSP19-01440)\n\n"
    "## Rules\n\n"
    "1. Return one ruling entry per numbered line item in the "
    "document. If the same case number appears on multiple "
    "lines with different motions, return each as a SEPARATE "
    "ruling entry.\n"
    "2. Extract the case number EXACTLY as it appears in the PDF.\n"
    "3. For case_title:\n"
    "   - Civil: construct 'Plaintiff v. Defendant' from the "
    "CASE NAME line. Convert ALL-CAPS to title case.\n"
    "   - Probate: use the name as given (e.g., 'In the Matter "
    "of: Ajay Bhalla', 'Conservatorship of: June Stone'). "
    "Convert ALL-CAPS to title case.\n"
    "4. For ruling_text: include the COMPLETE text of the ruling "
    "or examiner notes for that entry. Preserve it VERBATIM. "
    "Do NOT include text from other entries.\n"
    "5. Skip the department header boilerplate (appearance "
    "instructions, Zoom links, email addresses, etc.) -- "
    "only extract from the case entries.\n"
    "6. For probate entries where the ruling is just procedural "
    "notes (e.g., 'Need: 1. Appearances ...'), still extract "
    "it as the ruling_text.\n"
    "7. For entries with no case name (e.g., entry 21 in Dept 30 "
    "with only 'A26-00006' and no title after it), set "
    "case_title to null.\n\n"
    "## Parties\n\n"
    "For civil cases, extract plaintiff(s) and defendant(s) from "
    "the CASE NAME. "
    'Each party is {"name": "...", "role": "plaintiff", '
    '"confidence": "high"} or '
    '{"name": "...", "role": "defendant", '
    '"confidence": "high"}.\n'
    "For probate cases, extract the subject of the petition "
    "as a party with role 'petitioner' or 'subject'.\n\n"
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted -- motion/petition was fully granted (including "
    "'petition approved')\n"
    "- denied -- motion was fully denied\n"
    "- granted_in_part -- partially granted\n"
    "- denied_in_part -- partially denied\n"
    "- moot -- motion is moot\n"
    "- continued -- hearing was postponed/continued\n"
    "- off_calendar -- hearing removed from calendar, vacated, "
    "or dismissed\n"
    "- submitted -- taken under submission\n"
    "- other -- none of the above fit (use for procedural notes "
    "like 'Need appearances', entries with examiner notes "
    "listing required items, etc.)\n\n"
    "For 'overruled' (demurrers), map to 'denied'.\n"
    "For 'sustained' (demurrers), map to 'granted'.\n"
    "For entries that say 'Drop, per Court approved Request for "
    "Dismissal', map to 'off_calendar'.\n\n"
    "## Motion type labels\n\n"
    "Use a short descriptive label. Common values:\n"
    "msj, msj_partial, demurrer, motion_to_compel, "
    "motion_to_strike, motion_for_leave_to_amend, "
    "motion_for_sanctions, motion_for_attorney_fees, "
    "motion_to_be_relieved_as_counsel, motion_to_vacate, "
    "motion_to_enter_judgment, motion_to_set_aside, "
    "motion_to_continue_trial, motion_for_protective_order, "
    "motion_for_leave_to_file_cross_complaint, "
    "motion_for_judgment_on_pleadings, "
    "case_management_conference, "
    "petition, status_hearing, other.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "14" or "16" or "30" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "line_number": 1,\n'
    '      "extracted_case_number": "L23-06679" or null,\n'
    '      "extracted_case_title": "Discover Bank v. Gerald '
    'Gilchrist" or null,\n'
    '      "case_type": "civil" or "probate" or null,\n'
    '      "outcome": "granted" or null,\n'
    '      "motion_type": "motion_to_compel" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Discover Bank", "role": "plaintiff", '
    '"confidence": "high"},\n'
    '        {"name": "Gerald Gilchrist", "role": "defendant", '
    '"confidence": "high"}\n'
    "      ]\n"
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
    ("CA", "FRESNO"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=FRESNO_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "SANTA CLARA"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=SANTA_CLARA_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "VENTURA"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=VENTURA_SYSTEM_PROMPT,
        provider="google",
        model="gemini-2.5-flash-lite",
        max_output_tokens=32768,
    ),
    ("CA", "CONTRA COSTA"): CountyExtractionConfig(
        method=ExtractionMethod.LLM,
        system_prompt=CONTRA_COSTA_SYSTEM_PROMPT,
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
