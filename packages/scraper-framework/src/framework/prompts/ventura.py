"""Ventura County LLM extraction prompts.

Two prompts are maintained:

- ``VENTURA_SYSTEM_PROMPT`` — used by the Ventura scraper's own
  ``_ventura_llm_extract`` function (flat JSON output, no ``rulings``
  array).  Kept for backward compatibility with the live scraper path.
- ``VENTURA_FRAMEWORK_PROMPT`` — used by the framework ``LlmExtractor``
  via ``extraction_config.py`` during rebuilds and reingests.  Produces
  the ``{"rulings": [...]}`` format expected by
  ``LlmExtractor._parse_response()``.

The prompts share the same domain knowledge (Ventura document format,
outcome taxonomy, party extraction rules) but differ in output format.

Fix for #2271: the original ``VENTURA_SYSTEM_PROMPT`` produced a flat
JSON object that ``_parse_response()`` could not parse (it expects a
``"rulings"`` array), causing all Ventura outcomes to be lost during
rebuilds.
"""

from __future__ import annotations

# --- Shared domain knowledge for both prompts ---

_VENTURA_DOC_FORMAT = (
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
    "Some Ventura documents are probate/conservatorship proceedings that "
    "may not follow this structure. These may contain status reports, "
    "continuances, or administrative orders without a clear TENTATIVE "
    "RULING heading.\n\n"
)

_VENTURA_PARTY_RULES = (
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
)

_VENTURA_OUTCOME_TAXONOMY = (
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
    "others maps to 'granted_in_part'\n"
    "- 'continues this matter' or 'is continued' maps to 'continued'\n"
    "- Status reports, conservatorship reviews, and administrative "
    "orders without a clear motion outcome: use 'other'\n\n"
)

_VENTURA_RULING_TEXT_RULES = (
    "## Ruling Text\n\n"
    "For ruling_text, include the substantive ruling content starting "
    "from the TENTATIVE RULING heading through the end of the analysis.  "
    "EXCLUDE:\n"
    "- The court header (court name, county, department)\n"
    "- The case info block (case number, hearing date, party names line)\n"
    "- The standard footer ('If this tentative ruling is not contested...')\n"
    "Include the ruling disposition, background, analysis, and any orders.\n"
    "For probate/conservatorship documents without a TENTATIVE RULING "
    "heading, include the full substantive content.\n\n"
)

# --- Scraper prompt (flat JSON, used by _ventura_llm_extract) ---

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
    + _VENTURA_DOC_FORMAT
    + _VENTURA_PARTY_RULES
    + _VENTURA_OUTCOME_TAXONOMY
    + _VENTURA_RULING_TEXT_RULES
    + "## Output Format\n\n"
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


# --- Framework prompt ({"rulings": [...]} format for LlmExtractor) ---

VENTURA_FRAMEWORK_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Ventura County Superior Court.\n\n"
    "You will receive the text of a Ventura ruling document.  Each "
    "document covers ONE case unless it explicitly contains rulings for "
    "two or more distinct case numbers.  Extract ALL structured "
    "fields from the document.\n\n" + _VENTURA_DOC_FORMAT + "## Single-Ruling Rule\n\n"
    "Each Ventura PDF is ONE ruling. Output exactly one entry in the "
    "rulings array unless the document literally contains tentative "
    "rulings on two or more distinct case numbers.\n\n"
    "DO NOT split a Tentative Case Management Order (or any single-case "
    "Ventura document) by internal topic headings: NOTICE OF ASSIGNMENT, "
    "Arbitration, Settlement, Phased Discovery, Class List Discovery, "
    "Class Certification Motion, Mediation, Informal Discovery Conferences, "
    "Stipulated Protective & ESI Orders, Trial, etc. These are sub-sections "
    "inside ONE ruling, not separate rulings.\n\n"
    "**Negative example** (critical — do NOT do this): if a Case Management "
    "Order has 12 topic headings (NOTICE OF ASSIGNMENT, Arbitration, "
    "Settlement, Mediation, Trial, etc.), return ONE entry whose "
    "``ruling_text`` contains the full document text — not 12 entries "
    "with ``outcome='other'`` and ``motion_type='other'``.\n\n" + "## Case Number Formats\n\n"
    "Ventura uses TWO case number formats — extract either exactly as "
    "printed, never substitute 'UNKNOWN':\n\n"
    "**New format (post-2023):** 4-digit year + 4-letter type code + "
    "6-digit sequence number.\n"
    "Examples: 2024CUBC038456, 2025CUBC040123, 2025CUPR042345\n"
    "Type codes: CUBC (civil business), CUBT (civil breach of trust), "
    "CUPR (probate), etc.\n\n"
    "**Old format (pre-2023 probate):** 4-digit year + 6-digit sequence "
    "+ 'PR' + 2-letter subtype code (14-16 chars total).\n"
    "Examples: 202200570654PRLP, 201500471558PRCE, 202200572564PRLP\n"
    "Subtype codes: PRLP (letters of probate), PRCE (conservatorship), "
    "PRGU (guardianship), etc.\n\n"
    "If the document shows an old-format case number like "
    "'202200570654PRLP', extract it verbatim.  Do NOT return 'UNKNOWN'.\n\n"
    "## Case Title\n\n"
    "Extract the case title EXACTLY ONCE.  Some Ventura documents "
    "repeat the caption 2-3 times in the header -- output only a single "
    "copy.  Do NOT append the case number to the title.\n\n"
    "For probate cases, use patterns like:\n"
    "- 'In the Matter of <Name>'\n"
    "- 'Estate of <Name>'\n"
    "- 'Conservatorship of <Name>'\n"
    "- '<Name> Trust'\n\n"
    "## Hearing Date\n\n"
    "The hearing_date is the date of the HEARING being ruled on -- "
    "typically shown near the top of the document in the case info block.  "
    "It is NOT:\n"
    "- A continuance date mentioned in the ruling body ('continued to "
    "May 14, 2026')\n"
    "- A prior minute-order date referenced in the analysis\n"
    "- A filing date\n\n"
    "The correct hearing date is usually on or within a day or two of "
    "the document's capture date.  If the only date you can find is "
    "weeks or months in the future, it is almost certainly a continuance "
    "date -- return null for hearing_date rather than guessing.\n\n"
    "## Judge Name\n\n"
    "Extract the presiding judge's name from the document header or "
    "signature line.  For probate cases, the decedent's or conservatee's "
    "name is prominently printed in the caption (e.g. 'Estate of Delbert "
    "L. Webb') -- this is NOT the judge.  Do not return the decedent, "
    "conservatee, or trustor name as the judge.\n\n"
    "If no judge name is clearly identified in the document (only the "
    "department number is given), return null for extracted_judge_name "
    "rather than guessing.\n\n"
    + _VENTURA_PARTY_RULES
    + _VENTURA_OUTCOME_TAXONOMY
    + "## Motion Type\n\n"
    "Extract the motion type from the 'Nature of Proceedings' line "
    "or from the ruling text.  Common types:\n"
    "- demurrer\n"
    "- motion_to_compel\n"
    "- msj (motion for summary judgment)\n"
    "- motion_to_strike\n"
    "- petition (for probate matters)\n"
    "- motion_to_be_relieved_as_counsel\n"
    "Use the most specific type that applies.  Use null if unclear.\n\n"
    + _VENTURA_RULING_TEXT_RULES
    + "## Output Format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "<full judge name>" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "20" or null,\n'
    '  "rulings": [  // exactly one entry per case number — Ventura PDFs typically cover one case\n'
    "    {\n"
    '      "extracted_case_number": "2024CUBC038456" or null,\n'
    '      "extracted_case_title": "Plaintiff v. Defendant" or null,\n'
    '      "case_type": "civil" or "probate" or null,\n'
    '      "outcome": "granted" or "denied" or null,\n'
    '      "motion_type": "demurrer" or null,\n'
    '      "ruling_text": "Full ruling text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Full Name", "role": "plaintiff", '
    '"confidence": "high"},\n'
    '        {"name": "Full Name", "role": "defendant", '
    '"confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "outcome": "high",\n'
    '        "ruling_text": "high"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}"
)
