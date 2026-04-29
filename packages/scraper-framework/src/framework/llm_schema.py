"""Pydantic models for LLM-based structured extraction of court rulings.

These models define the **raw extraction** schema — what the LLM sees on the
page and outputs.  Fields prefixed with ``extracted_`` are populated by the
LLM.  Enrichment-resolved fields (e.g., resolved party IDs, canonical judge
records) are NOT part of this schema — they live in the ingestion pipeline's
downstream models.

The system prompt for the LLM extractor is co-located here so that the
schema and prompt stay in sync.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ConfidenceLevel(StrEnum):
    """Confidence level for an extracted field.

    - **high** — clearly present and unambiguous in the source text
    - **medium** — present but partially obscured, abbreviated, or garbled
    - **low** — inferred or uncertain (e.g., judge name not explicit)
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionOutcome(StrEnum):
    """Ruling outcome taxonomy — matches the ``ruling_outcome`` PostgreSQL enum."""

    GRANTED = "granted"
    DENIED = "denied"
    GRANTED_IN_PART = "granted_in_part"
    DENIED_IN_PART = "denied_in_part"
    MOOT = "moot"
    CONTINUED = "continued"
    OFF_CALENDAR = "off_calendar"
    SUBMITTED = "submitted"
    OTHER = "other"


class ExtractionCaseType(StrEnum):
    """Case type taxonomy — matches the ``cases.case_type`` TEXT column."""

    CIVIL = "civil"
    CRIMINAL = "criminal"
    FAMILY = "family"
    PROBATE = "probate"
    SMALL_CLAIMS = "small_claims"
    JUVENILE = "juvenile"
    TRAFFIC = "traffic"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Confidence scores
# ---------------------------------------------------------------------------


class FieldConfidence(BaseModel):
    """Per-field confidence scores for an extracted ruling."""

    case_number: ConfidenceLevel = ConfidenceLevel.HIGH
    case_title: ConfidenceLevel = ConfidenceLevel.HIGH
    parties: ConfidenceLevel = ConfidenceLevel.HIGH
    judge: ConfidenceLevel = ConfidenceLevel.HIGH
    ruling_text: ConfidenceLevel = ConfidenceLevel.HIGH
    outcome: ConfidenceLevel = ConfidenceLevel.HIGH


# ---------------------------------------------------------------------------
# Extracted party
# ---------------------------------------------------------------------------


class ExtractedParty(BaseModel):
    """A party extracted from a case caption by the LLM.

    This is a *raw extraction* — the ``name`` is exactly as it appears in the
    source text.  Downstream enrichment may resolve it to a canonical party
    record, normalize casing, or split compound names.
    """

    name: str
    role: str  # e.g., "plaintiff", "defendant", "petitioner", "respondent"
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH


# ---------------------------------------------------------------------------
# Extracted ruling (single case within a document)
# ---------------------------------------------------------------------------


class ExtractedRuling(BaseModel):
    """A single ruling extracted from a court calendar page.

    All fields here are **raw extractions** — populated directly by the LLM
    from the source text.  The ``extracted_`` prefix distinguishes these from
    enrichment-resolved values that appear in downstream pipeline models
    (e.g., resolved party IDs, canonical judge records, validated case types).
    """

    extracted_case_number: str | None = None
    extracted_case_title: str | None = None
    extracted_parties: list[ExtractedParty] = Field(default_factory=list)
    extracted_judge_name: str | None = None
    department: str | None = None
    motion_type: str | None = None
    outcome: ExtractionOutcome | None = None
    ruling_text: str | None = None
    hearing_date: str | None = None  # ISO format "YYYY-MM-DD"
    case_type: ExtractionCaseType | None = None
    confidence: FieldConfidence = Field(default_factory=FieldConfidence)
    cross_reference_source: int | None = None  # entry_number ruling_text was copied from
    # Calendar line number from the source PDF; preserved so that
    # _resolve_cross_references can run on the cache-hit path (#3608).
    entry_number: int | None = None


# ---------------------------------------------------------------------------
# Top-level extraction result
# ---------------------------------------------------------------------------


class ExtractionResult(BaseModel):
    """Top-level result from LLM extraction of a court calendar page.

    Contains document-level metadata and an array of per-ruling extractions.
    All fields are **raw LLM output** — no enrichment or resolution has been
    applied.
    """

    extracted_judge_name: str | None = None
    hearing_date: str | None = None  # ISO format "YYYY-MM-DD"
    department: str | None = None
    rulings: list[ExtractedRuling] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompt — co-located with the schema so they stay in sync
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings.\n\n"
    "Given a court ruling document, extract ALL structured "
    "fields into JSON.\n\n"
    "## Rules\n\n"
    "1. **Multi-ruling documents:** A single document may "
    "contain rulings for MANY cases (sometimes 10+). You MUST "
    "return an array of ALL cases found in the entire document. "
    "Read through the ENTIRE document systematically — do not "
    "stop after the first few cases. Count every case number "
    "you find. Common patterns: numbered lists (#1, #2, ...), "
    "case headers with case numbers, or page breaks between "
    "cases.\n\n"
    "2. **Multi-department documents:** Some documents contain "
    "rulings from multiple departments (e.g. Dept N14 on page 1, "
    "Dept N6 on page 15). Extract ALL rulings from ALL "
    "departments. The top-level judge_name and department should "
    "reflect the FIRST department in the document. If different "
    "departments have different judges, note the judge for each "
    "department in the rulings where applicable.\n\n"
    "3. **Outcome taxonomy** — use EXACTLY one of these "
    "values:\n"
    "   - granted — motion was fully granted, INCLUDING "
    "'granted with conditions'\n"
    "   - denied — motion was fully denied, INCLUDING "
    "'denied without prejudice'\n"
    "   - granted_in_part — motion was partially granted "
    "and partially denied\n"
    "   - denied_in_part — motion was partially denied\n"
    "   - moot — motion is moot (no longer relevant)\n"
    "   - continued — hearing was postponed to a future date\n"
    "   - off_calendar — the hearing was REMOVED from the "
    "calendar entirely. This is NOT the same as 'continued'. "
    "'Off calendar' means the matter will not be heard. "
    "Look for phrases like 'taken off calendar', "
    "'off calendar', 'removed from calendar', "
    "'OCAL', 'OC', or 'vacated'.\n"
    "   - submitted — matter was taken under submission "
    "for the judge to decide later\n"
    "   - other (only if none of the above fit)\n\n"
    "4. **Case number normalization:** Strip any county "
    "prefix digits before the year. For example, "
    '"30-2024-01393434" becomes "2024-01393434". '
    "Keep the full number for formats like "
    '"24NNCV02551".\n\n'
    "5. **Department normalization:** Preserve the department "
    "identifier exactly as it appears in the document, "
    "INCLUDING leading zeros. For example, 'CM02' stays "
    "'CM02' (do NOT simplify to 'CM2'). 'CM05' stays "
    "'CM05'. 'N14' stays 'N14'.\n\n"
    "6. **Parties:** Extract plaintiff(s) and defendant(s) "
    "from case captions. Each party is "
    '{"name": "...", "role": "plaintiff", "confidence": "high"} or '
    '{"name": "...", "role": "defendant", "confidence": "high"}.\n\n'
    "7. **Case type:** Classify the case using EXACTLY one "
    "of these values:\n"
    "   - civil (general civil litigation, torts, contracts, "
    "employment, PI — includes 'unlimited civil' and "
    "'limited civil')\n"
    "   - criminal\n"
    "   - family (divorce, custody, domestic violence)\n"
    "   - probate (wills, estates, conservatorships, "
    "guardianships)\n"
    "   - small_claims\n"
    "   - juvenile\n"
    "   - traffic\n"
    "   - other (only if none of the above fit)\n\n"
    "   Inference hints: case numbers starting with SC or "
    "containing 'SC' often indicate small claims; "
    "'CV', 'CIV', 'STCV' indicate civil; "
    "'CR', 'F' indicate criminal; "
    "'FL', 'DV' indicate family; "
    "'PR', 'BP' indicate probate. "
    "Motion types like 'demurrer', 'msj', 'anti_slapp' "
    "are civil. "
    "If the case type cannot be determined, use null.\n\n"
    "8. **Motion type:** Use a short descriptive label. "
    "Common values: "
    '"msj" (summary judgment), '
    '"msj_partial" (summary adjudication), '
    '"mtd" (motion to dismiss), '
    '"mil" (motion in limine), '
    '"demurrer", "motion_to_compel", '
    '"motion_to_strike", "anti_slapp", '
    '"preliminary_injunction", '
    '"ex_parte_application", "petition", '
    '"default_judgment", '
    '"motion_for_attorney_fees", '
    '"motion_to_be_relieved_as_counsel", '
    '"motion_for_leave_to_amend", '
    '"motion_for_sanctions", '
    '"motion_for_judgment_on_the_pleadings", '
    '"motion_for_protective_order", '
    '"osc" (order to show cause), "other".\n\n'
    "9. **Hearing date:** Return as ISO format "
    '"YYYY-MM-DD".\n\n'
    "10. **Judge name:** Extract the judge's full name. "
    'Do not include titles like "Hon.", "Judge", or '
    '"Commissioner". Check ALL of these locations for '
    "the judge's name:\n"
    "   - Document headers and footers\n"
    "   - Department headings (e.g. 'DEPT C25 / "
    "Judge Gassia Apkarian')\n"
    "   - Signature blocks at the end of rulings\n"
    "   - PDF metadata or page headers\n"
    "   - The ruling text itself\n"
    "   If no judge name is found anywhere, return null.\n\n"
    "11. If metadata is provided (judge_name, department), "
    "treat it as authoritative — use it directly rather "
    "than extracting from the document.\n\n"
    "12. **Confidence scores:** For each ruling, provide a "
    '"confidence" object with scores for key fields. Use:\n'
    '   - "high" — clearly present and unambiguous\n'
    '   - "medium" — present but partially obscured or abbreviated\n'
    '   - "low" — inferred or uncertain\n\n'
    "13. **Ruling text:** The ruling_text field must contain "
    "the COMPLETE text of the tentative ruling for this case. "
    "Include EVERY section verbatim — procedural background, "
    "legal standard, analysis, discussion, conclusion, "
    "disposition, and any other content the judge wrote about this case. "
    "Do not summarize, rephrase, or omit "
    "anything. When in doubt, include it. This is the primary "
    "field attorneys will read — missing text means lost data.\n\n"
    "14. **Case title:** The extracted_case_title field must "
    "contain ONLY the party names from the case caption, "
    "e.g. 'Marquez v. Kohl\\'s Department Stores, Inc.' — "
    "do NOT include hearing times, case numbers, motion "
    "descriptions, or ruling text in this field. "
    "Never include any portion of the case number, court prefix, "
    "or hearing-line digits in extracted_case_title. "
    "If the case number is illegible, set extracted_case_number "
    "to null rather than copying a fragment of it into the title.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "C25" or "N14" or "CM02" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "24NNCV02551" or null,\n'
    '      "extracted_case_title": "Smith v. Jones" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "granted" or null,\n'
    '      "motion_type": "msj" or null,\n'
    '      "ruling_text": "The motion for summary judgment is GRANTED..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "John Smith", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Jane Jones", "role": "defendant", "confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "judge": "medium",\n'
    '        "ruling_text": "high",\n'
    '        "outcome": "high"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "## Few-shot examples\n\n"
    "### Example 1: LA Superior Court HTML (2 rulings)\n\n"
    "Input (preprocessed HTML text):\n"
    "DEPARTMENT 205 LAW AND MOTION RULINGS\n"
    "Case Number: 22SMCV01940\n"
    "Hernandez v. Pacific Coast Properties LLC\n"
    "Hearing Date: March 3, 2026\n"
    "Motion: Motion for Summary Judgment\n"
    "Background\n"
    "This is a personal injury action arising from a slip and fall "
    "at defendant's commercial property on January 15, 2020. "
    "Plaintiff alleges that defendant failed to maintain the "
    "parking lot in a safe condition.\n"
    "Legal Standard\n"
    "A party may move for summary judgment when there is no "
    "triable issue of material fact. (Code Civ. Proc. § 437c.)\n"
    "Analysis\n"
    "The motion for summary judgment is GRANTED. Defendant has "
    "met its burden of showing that plaintiff cannot establish "
    "the element of notice. The undisputed evidence shows "
    "defendant inspected the lot 30 minutes before the incident "
    "and the hazard was not present.\n\n"
    "Case Number: 25SMCV01132\n"
    "Kim v. Westfield Group\n"
    "Motion: Demurrer to First Amended Complaint\n"
    "The demurrer is SUSTAINED with 20 days leave to amend "
    "as to the second cause of action for fraud. The FAC "
    "fails to plead fraud with the specificity required "
    "under the heightened pleading standard.\n\n"
    "Expected output:\n"
    "{\n"
    '  "extracted_judge_name": null,\n'
    '  "hearing_date": "2026-03-03",\n'
    '  "department": "205",\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "22SMCV01940",\n'
    '      "extracted_case_title": "Hernandez v. Pacific Coast Properties LLC",\n'
    '      "case_type": "civil",\n'
    '      "outcome": "granted",\n'
    '      "motion_type": "msj",\n'
    '      "ruling_text": "Background\\nThis is a personal injury action '
    "arising from a slip and fall at defendant's commercial property on "
    "January 15, 2020. Plaintiff alleges that defendant failed to maintain "
    "the parking lot in a safe condition.\\nLegal Standard\\nA party may "
    "move for summary judgment when there is no triable issue of material "
    "fact. (Code Civ. Proc. § 437c.)\\nAnalysis\\nThe motion for summary "
    "judgment is GRANTED. Defendant has met its burden of showing that "
    "plaintiff cannot establish the element of notice. The undisputed "
    "evidence shows defendant inspected the lot 30 minutes before the "
    'incident and the hazard was not present.",\n'
    '      "extracted_parties": [\n'
    '        {"name": "Hernandez", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Pacific Coast Properties LLC", "role": "defendant", '
    '"confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "judge": "low",\n'
    '        "ruling_text": "high",\n'
    '        "outcome": "high"\n'
    "      }\n"
    "    },\n"
    "    {\n"
    '      "extracted_case_number": "25SMCV01132",\n'
    '      "extracted_case_title": "Kim v. Westfield Group",\n'
    '      "case_type": "civil",\n'
    '      "outcome": "granted",\n'
    '      "motion_type": "demurrer",\n'
    '      "ruling_text": "The demurrer is SUSTAINED with 20 days leave '
    "to amend as to the second cause of action for fraud. The FAC fails "
    "to plead fraud with the specificity required under the heightened "
    'pleading standard.",\n'
    '      "extracted_parties": [\n'
    '        {"name": "Kim", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "Westfield Group", "role": "defendant", "confidence": "high"}\n'
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
    "}\n\n"
    "### Example 2: OC Superior Court PDF (department header with judge)\n\n"
    "Input (extracted PDF text):\n"
    "TENTATIVE RULINGS\n"
    "LAW & MOTION\n"
    "DEPT C25 / Judge Gassia Apkarian\n"
    "February 24, 2026\n\n"
    "Case No. 2024-01393434\n"
    "Martinez v. ABC Manufacturing Inc.\n"
    "Motion for Summary Adjudication\n"
    "Procedural History\n"
    "Plaintiff filed this products liability action on "
    "March 10, 2024. Defendant moves for summary adjudication "
    "of the strict liability cause of action.\n"
    "Discussion\n"
    "The motion for summary adjudication is DENIED. Defendant "
    "has not met its initial burden as the declaration of its "
    "expert fails to address the design defect theory.\n\n"
    "Expected output:\n"
    "{\n"
    '  "extracted_judge_name": "Gassia Apkarian",\n'
    '  "hearing_date": "2026-02-24",\n'
    '  "department": "C25",\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "2024-01393434",\n'
    '      "extracted_case_title": "Martinez v. ABC Manufacturing Inc.",\n'
    '      "case_type": "civil",\n'
    '      "outcome": "denied",\n'
    '      "motion_type": "msj_partial",\n'
    '      "ruling_text": "Procedural History\\nPlaintiff filed this products '
    "liability action on March 10, 2024. Defendant moves for summary "
    "adjudication of the strict liability cause of action.\\n"
    "Discussion\\nThe motion for summary adjudication is DENIED. "
    "Defendant has not met its initial burden as the declaration of its "
    'expert fails to address the design defect theory.",\n'
    '      "extracted_parties": [\n'
    '        {"name": "Martinez", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "ABC Manufacturing Inc.", "role": "defendant", '
    '"confidence": "high"}\n'
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
    "}\n\n"
    "### Example 3: Riverside Superior Court PDF (multiple rulings)\n\n"
    "Input (extracted PDF text):\n"
    "Tentative Rulings for March 2, 2026\n"
    "Department PS1\n"
    "Honorable Arthur Hester III\n\n"
    "#1 CVPS2306157\n"
    "Garcia v. State Farm Insurance\n"
    "Motion to Compel Further Discovery Responses\n"
    "The motion is GRANTED. Defendant shall provide "
    "further responses to Form Interrogatories Nos. 15.1 "
    "and 17.1 within 20 days. The responses provided were "
    "evasive and incomplete. Sanctions of $1,500 are imposed "
    "against defendant and defense counsel, jointly and "
    "severally, payable within 30 days.\n\n"
    "#2 CVPS2400892\n"
    "Thompson v. City of Palm Springs\n"
    "Demurrer to Second Amended Complaint\n"
    "The demurrer is OVERRULED. The SAC adequately pleads "
    "a dangerous condition of public property under "
    "Government Code section 835. Defendant shall file "
    "its answer within 10 days.\n\n"
    "Expected output:\n"
    "{\n"
    '  "extracted_judge_name": "Arthur Hester III",\n'
    '  "hearing_date": "2026-03-02",\n'
    '  "department": "PS1",\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "CVPS2306157",\n'
    '      "extracted_case_title": "Garcia v. State Farm Insurance",\n'
    '      "case_type": "civil",\n'
    '      "outcome": "granted",\n'
    '      "motion_type": "motion_to_compel",\n'
    '      "ruling_text": "The motion is GRANTED. Defendant shall provide '
    "further responses to Form Interrogatories Nos. 15.1 and 17.1 within "
    "20 days. The responses provided were evasive and incomplete. Sanctions "
    "of $1,500 are imposed against defendant and defense counsel, jointly "
    'and severally, payable within 30 days.",\n'
    '      "extracted_parties": [\n'
    '        {"name": "Garcia", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "State Farm Insurance", "role": "defendant", '
    '"confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "judge": "high",\n'
    '        "ruling_text": "high",\n'
    '        "outcome": "high"\n'
    "      }\n"
    "    },\n"
    "    {\n"
    '      "extracted_case_number": "CVPS2400892",\n'
    '      "extracted_case_title": "Thompson v. City of Palm Springs",\n'
    '      "case_type": "civil",\n'
    '      "outcome": "denied",\n'
    '      "motion_type": "demurrer",\n'
    '      "ruling_text": "The demurrer is OVERRULED. The SAC adequately '
    "pleads a dangerous condition of public property under Government Code "
    'section 835. Defendant shall file its answer within 10 days.",\n'
    '      "extracted_parties": [\n'
    '        {"name": "Thompson", "role": "plaintiff", "confidence": "high"},\n'
    '        {"name": "City of Palm Springs", "role": "defendant", '
    '"confidence": "high"}\n'
    "      ],\n"
    '      "confidence": {\n'
    '        "case_number": "high",\n'
    '        "case_title": "high",\n'
    '        "parties": "high",\n'
    '        "judge": "high",\n'
    '        "ruling_text": "high",\n'
    '        "outcome": "medium"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}"
)
