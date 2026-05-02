"""LLM-based structured extraction of court rulings.

The ``LlmExtractor`` class is the framework-level entry point for
converting raw court calendar text into structured ``ExtractedRuling``
models via a configurable LLM provider (Anthropic or Google GenAI).

Design principles:

- **Stateless**: no DB access, no side effects.  Pure function:
  text in, structured data out.
- **Configurable**: provider, model, and API key are configurable;
  defaults to Anthropic Claude Haiku 4.5 for cost efficiency.
- **Multimodal**: supports both text extraction (``extract()``) and
  PDF image extraction (``extract_from_pdf()``).  PDF extraction uses
  **per-page** LLM calls (one page per call, then joins results) to
  stay within output token limits and improve accuracy.
- **Resilient**: retries on transient API errors (429, 500, 529) with
  exponential backoff.
- **Observable**: logs token usage per call for cost monitoring.
- **Chunking-aware**: automatically splits large documents and merges
  results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import anthropic
import structlog
from judgemind_config import DEFAULT_HAIKU_MODEL

from .llm_schema import (
    EXTRACTION_SYSTEM_PROMPT,
    ConfidenceLevel,
    ExtractedParty,
    ExtractedRuling,
    ExtractionCaseType,
    ExtractionOutcome,
    ExtractionResult,
    FieldConfidence,
)
from .llm_utils import parse_llm_json, strip_llm_json_fences

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Accumulated token usage across one or more LLM calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

# HTTP status codes that are considered transient and should be retried.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 529})

# Default retry parameters.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 2.0
_DEFAULT_MAX_DELAY = 60.0

# ---------------------------------------------------------------------------
# Chunking constants
# ---------------------------------------------------------------------------

# Default per-chunk character budget.  80K chars leaves room for the
# system prompt and output tokens within the model's context window.
_DEFAULT_MAX_CHARS = 80_000

# Maximum number of chunks per document.
_MAX_CHUNKS = 10

# Overlap characters between consecutive chunks.
_CHUNK_OVERLAP = 500

# Retry-with-smaller-chunks limits (#2136).
# Maximum recursion depth for splitting failed chunks.
_MAX_RETRY_DEPTH = 1
# Minimum chunk size (chars) to attempt a retry split.
_MIN_RETRY_CHUNK_CHARS = 2000

# Patterns for natural split boundaries.
_PAGE_BREAK_RE = re.compile(r"\f")
_CASE_BOUNDARY_RE = re.compile(
    r"\n(?=\s*(?:Case\s+(?:Number|No\.?)|CASE\s+(?:NUMBER|NO\.?))\s*[:\s])",
    re.IGNORECASE,
)

# OC-style county prefix: "30-2024-01393434" -> "2024-01393434"
_COUNTY_PREFIX_RE = re.compile(r"^\d{2,4}-(\d{4}-\d+)$")

# Riverside case-number pattern (duplicated from courts.ca.riverside_tentatives
# to avoid a courts→framework import inversion).  Used by the cross-case
# ruling_text truncation sanitizer (#2564).
# Covers:
#   CV + 2-4 letter location code + 6-8 digits (e.g. CVPS2306157, CVRI2500796)
#   RIC, MCC, PSC, SWC, INC, CIV, MVC, TEC, UDPS + 0-4 letters + 6-10 digits
_RIVERSIDE_CASE_NUMBER_RE = re.compile(
    r"\b(?:CV[A-Z]{2,4}|(?:RIC|MCC|PSC|SWC|INC|CIV|MVC|TEC|UDPS)[A-Z]{0,4})\d{6,10}\b",
    re.IGNORECASE,
)

# San Bernardino case-number pattern.  Covers CIVSB*/CIVRS* prefixes used by
# San Bernardino (SB) and Rancho Cucamonga (RS) divisions.
# Spaces inside the case number (e.g. "CIVSB 2600093") are normalised by the
# scraper before reaching this layer, so the pattern does not need to handle
# internal whitespace.  See #2565.
_SB_CASE_NUMBER_RE = re.compile(
    r"\bCIV(?:SB|RS)\s*\d{5,8}\b",
    re.IGNORECASE,
)

# Matches a case_title that is a role-literal placeholder: the LLM emitted the
# role word ("Plaintiff", "Defendant", "Petitioner", "Respondent") as the party
# name instead of the real name from the ruling body.  Both singular and plural
# forms are covered.  See #2565.
_ROLE_LITERAL_TITLE_RE = re.compile(
    r"^\s*(?:Plaintiff|Defendant|Petitioner|Respondent)s?\s+v[s]?\.?\s",
    re.IGNORECASE,
)

# Matches a case_title that contains an LLM-hallucinated bracketed placeholder
# for a party name, e.g. "Ezra Arce v. [Defendant not specified]" or
# "[Plaintiff name unknown] v. Smith".  The bracket must contain a role word
# (plaintiff/defendant/petitioner/respondent/party/name/case) followed by a
# qualifier (not specified, unknown, missing, not listed, not provided, tbd).
# No anchor — the bracket can appear anywhere in the title.  See #3988.
#
# Explicit non-match envelope (do NOT widen without product confirmation — #4002):
#   - Role-only:        [DEFENDANT], [Defendant 1]  — no qualifier word present
#   - Qualifier-only:   [TBD], [Insert defendant here]  — no leading role word
#   - Possessives:      [defendant's name]  — possessive breaks the role-word token
#   - Ordinals:         [Defendant 1]  — digit suffix is not a qualifier keyword
#   - Free-form:        [Name to be determined]  — "Name" alone is not a role word
#                       used in this position; qualifier phrase not in the allowlist
_BRACKETED_PLACEHOLDER_TITLE_RE = re.compile(
    r"\[(?:plaintiff|defendant|petitioner|respondent|party|name|case)[^\]]*"
    r"(?:not specified|unknown|missing|not listed|not provided|tbd)[^\]]*\]",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# LLM result cache
# ---------------------------------------------------------------------------

# Cache lives in S3 at llm-cache/{provider}-{model}/prompt-{hash}/{content_hash}.json
# Locally, flows through S3_CACHE_DIR via CachedS3Client for fast reads.
# On ECS, reads/writes go directly to S3.


def _prompt_hash(prompt: str) -> str:
    """Short hash of the system prompt for cache key."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def _content_hash_for_cache(content: str | bytes, metadata: dict[str, str] | None = None) -> str:
    """Hash content + metadata for cache lookup.

    Metadata (judge_name, department, etc.) is included because the LLM
    output may differ when metadata changes, even for the same content.
    """
    h = hashlib.sha256()
    if isinstance(content, str):
        h.update(content.encode())
    else:
        h.update(content)
    if metadata:
        h.update(json.dumps(metadata, sort_keys=True).encode())
    return h.hexdigest()


class _LlmCache:
    """S3-backed cache for LLM extraction results.

    S3 key: llm-cache/{provider}-{model}/prompt-{hash}/{content_hash}.json

    When S3_CACHE_DIR is set locally, reads are served from local disk
    via CachedS3Client (fast). On ECS, reads/writes go directly to S3.
    Either way, the cache is shared across all environments.
    """

    def __init__(self, s3_client: object, bucket: str, provider: str, model: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._provider = provider
        self._model = model

    def _key(self, prompt: str, content_key: str) -> str:
        return (
            f"llm-cache/{self._provider}-{self._model}"
            f"/prompt-{_prompt_hash(prompt)}"
            f"/{content_key}.json"
        )

    def get(self, prompt: str, content_key: str) -> list[dict] | None:
        """Return cached extraction result from S3, or None if not cached."""
        key = self._key(prompt, content_key)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(response["Body"].read())
        except Exception:
            return None

    def put(self, prompt: str, content_key: str, rulings: list[dict]) -> None:
        """Cache an extraction result to S3."""
        key = self._key(prompt, content_key)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(rulings, indent=2).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            logger.warning("llm_cache.write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Per-page PDF extraction prompt and join patterns (#1590)
# ---------------------------------------------------------------------------

# System prompt for per-page multimodal extraction from court ruling PDFs.
# Each page is sent individually.  The LLM returns a JSON object with
# per-case entries and optional page-level header info.  Supports all
# California court PDF layouts: OC 3-column tables, Riverside mini-tables,
# Santa Clara 4-column tables, Contra Costa bordered boxes, San Bernardino/
# Ventura/San Francisco free-form prose, Fresno label-value forms, etc.
#
# Note: page_header extraction (department, judge, hearing_date) straddles
# the transcription/enrichment boundary — it extracts metadata from page
# visual structure rather than from ruling text content.  This is pragmatic:
# the data is visible on the page image and avoids a second pass.  The
# authoritative case_title should come from enrichment (#2212).
PDF_PER_PAGE_PROMPT = (
    "You are a court ruling transcriber. You will receive a single page "
    "image from a California court tentative ruling PDF.\n\n"
    "## Your task\n\n"
    "Extract every tentative ruling on this page into a JSON object.\n\n"
    "## Output format\n\n"
    "Return a JSON object with two keys:\n\n"
    '**"page_header"** (object or null): If this page has a document '
    "header with the department, judge name, or hearing date, extract "
    "them here. Return null if none are visible on this page.\n"
    "  - **department**: The department NUMBER or CODE only (e.g. '16', "
    "'C25', 'PS1', 'R17'), NOT the word 'Department'.\n"
    "  - **judge_name**: The judge's full name WITHOUT titles like "
    "'Hon.', 'Judge', 'Honorable', or 'Commissioner'.\n"
    "  - **hearing_date**: In ISO format 'YYYY-MM-DD'.\n\n"
    '**"rulings"** (array): One object per case ruling on this page. '
    "Each object has these fields:\n\n"
    "- **entry_number** (string): Calendar line number, item number, "
    "or sequence number (e.g. '1', '101', '(47)', 'Line 2'). "
    "Empty string if none.\n"
    "- **case_number** (string): The FULL case number exactly as "
    "printed, including any letter prefix (e.g. 'C22-01971', "
    "'2024-01393434', 'CVRI2401570', '22SMCV01940', 'N25-2112'). "
    "The letter prefix (C, N, etc.) is part of the case number, not "
    "part of the case title. Empty string if not visible. **Do NOT "
    "put the case title, party names, or the word 'v.'/'vs.' in "
    "case_number — if the PDF row shows no explicit alphanumeric "
    "case number (many OC calendar PDFs have only a # column and a "
    "CASE NAME column), return case_number as empty string.**\n"
    "- **case_title** (string): ONLY the party names from the case "
    "caption (e.g. 'Constantina Marquez vs. Kohl\\'s Department "
    "Stores, Inc.'). This is JUST the names — plaintiff vs. "
    "defendant. Do NOT include case numbers, letter prefixes (C, N), "
    "hearing times, motion descriptions, 'HEARING ON...', "
    "'PETITION OF...', cause of action descriptions, or any other "
    "text. Empty string if not visible.\n"
    "- **ruling_text** (string): The COMPLETE text the judge wrote "
    "about this case. Include EVERYTHING — motion type/description, "
    "procedural background, legal standard, analysis, discussion, "
    "conclusion, disposition, orders, and any other content the "
    "judge wrote about this case. Do not summarize or omit anything. "
    "**If the row's tentative-ruling column/cell is visibly empty (the "
    "column is present but the body cell is blank), return "
    '`ruling_text=""`. Do NOT substitute the case caption, motion '
    "label, case number, or any other nearby text. Emit empty string.**\n\n"
    "## How to handle different page layouts\n\n"
    "California courts use many different PDF formats. Identify which "
    "layout this page uses and extract accordingly:\n\n"
    "**Tables** (columns separated by vertical lines): "
    "Return one object per table row. The narrow column(s) have the "
    "entry number and/or case info; the wide column has the ruling "
    "text. **When a row's ruling-text column is empty/blank, emit "
    '`ruling_text=""` for that row even though entry_number / '
    "case_number / case_title are filled.**\n\n"
    "**Bordered boxes** (each case in a rectangular border): "
    "Return one object per box. The box header has the item number, "
    "time, case number, and case name. The ruling text is everything "
    "the judge wrote — typically everything after '*TENTATIVE "
    "RULING:*' or similar marker, but also include any motion "
    "description header that precedes the marker.\n\n"
    "**Free-form prose** (no table, no boxes — plain document text): "
    "Identify case boundaries by case number headers, bold case "
    "captions, centered titles, horizontal rules, or signature blocks. "
    "Return one object per case.\n\n"
    "**Label-value forms** (fields like 'Re:', 'Motion:', "
    "'Tentative Ruling:', 'Explanation:'): The case_number and "
    "case_title come from the Re: and case number fields. The "
    "ruling_text is EVERYTHING from the tentative ruling/explanation "
    "onward, including the explanation.\n\n"
    "**Continuation pages** (no new case header — just ongoing text "
    "from a previous page): Return one object with empty entry_number, "
    "empty case_number, empty case_title, and the continuation text "
    "in ruling_text.\n\n"
    "**Boilerplate-only pages** (instructions about contesting "
    "rulings, Zoom links, court reporter notices, appearance "
    "procedures — no actual case rulings): Return an empty rulings "
    "array. Still extract page_header if the department, judge, or "
    "hearing date is visible.\n\n"
    "**Calendar listings without a ruling column** (rows that show "
    "only entry number and case name with no dedicated ruling-body "
    "column — e.g. a pure calendar agenda): Return one object per row "
    "with entry_number / case_number / case_title populated and "
    '`ruling_text=""`. Downstream filters will drop these rows; do '
    "NOT copy the case caption or motion label into ruling_text.\n\n"
    "**Orange County multi-line tabular layout** (OC Superior Court "
    "PDFs): The case number sits in a right-hand column next to the "
    "case name. In many OC PDFs, the case number column wraps across "
    "two visual lines — the case name appears on one line and the "
    "case number appears on the next line in the same row. Additionally, "
    "the printed number may show only the year-and-sequence portion "
    "(e.g. ``2023-01329371``) without the ``30-`` court prefix. Always "
    "capture the full case number as printed in case_number even when "
    "the layout splits it visually across lines. Return empty string "
    "ONLY when the case number is genuinely illegible — not when it is "
    "merely hard to align with the case name because of the multi-line "
    "table layout.\n\n"
    "## Formatting rules\n\n"
    "- Transcribe ruling_text as **Markdown** preserving formatting:\n"
    "  - Use **bold** for bold text and headings\n"
    "  - Use *italic* for italic text\n"
    "  - Use numbered lists (1. 2. 3.) for numbered paragraphs\n"
    "  - Use blank lines between paragraphs\n"
    "  - Preserve ALL content — do not summarize or omit\n"
    "- SKIP page headers/footers, page numbers, and watermarks.\n"
    "- SKIP boilerplate (instructions for contesting rulings, Zoom "
    "info, court reporter notices, appearance procedures).\n"
    "- If a field is blank or not applicable, use empty string.\n\n"
    "## Example output\n\n"
    "{\n"
    '  "page_header": {\n'
    '    "department": "C25",\n'
    '    "judge_name": "Gassia Apkarian",\n'
    '    "hearing_date": "2026-03-25"\n'
    "  },\n"
    '  "rulings": [\n'
    "    {\n"
    '      "entry_number": "101",\n'
    '      "case_number": "2024-01393434",\n'
    '      "case_title": "Smith vs Jones",\n'
    '      "ruling_text": "**MOTION FOR SUMMARY JUDGMENT**\\n\\n'
    "**Background**\\n\\nThis is a personal injury action arising from "
    "a slip and fall at defendant's commercial property.\\n\\n"
    "**Analysis**\\n\\nDefendant's motion for summary judgment is "
    "**GRANTED**.\\n\\n"
    "1. The moving party has met its initial burden...\\n"
    '2. Plaintiff fails to raise a triable issue..."\n'
    "    },\n"
    "    {\n"
    '      "entry_number": "",\n'
    '      "case_number": "",\n'
    '      "case_title": "",\n'
    '      "ruling_text": "continuation from previous page..."\n'
    "    },\n"
    "    {\n"
    '      "entry_number": "5",\n'
    '      "case_number": "2025-00912345",\n'
    '      "case_title": "Malki vs. Acme Corp.",\n'
    '      "ruling_text": ""\n'
    "    },\n"
    "    {\n"
    '      "entry_number": "6",\n'
    '      "case_number": "2025-00956789",\n'
    '      "case_title": "Johnson vs. Smith — Motion to Compel",\n'
    '      "ruling_text": ""\n'
    "    }\n"
    "  ]\n"
    "}"
)

# Pattern to detect case numbers across all California county formats:
#   OC:          2024-01393434, 25D006297
#   Riverside:   CVRI2401570, CVPS2305159, CVSW2303829
#   Santa Clara: 26CV484550, 22CV407249, 2010-1-CV-163328
#   Fresno:      18CECG00898, 24CECG04476
#   SF:          FPT-24-378499, FDI-22-796758, FMS-15-386703
#   CC:          C22-01971, N25-2112
#   SB:          CIVSB2116995, CIVRS2510003
_CASE_NUMBER_RE = re.compile(
    r"\b[A-Z]{0,4}\d{2,4}-\d{1,2}-[A-Z]{2}-\d{5,8}\b"  # 2010-1-CV-163328
    r"|\b[A-Z]{1,4}-\d{2,4}-\d{5,8}\b"  # FPT-24-378499
    r"|\b[A-Z]{1,2}\d{2,4}-\d{4,8}\b"  # C22-01971, N25-2112
    r"|\b\d{2,4}-\d{5,8}\b"  # 2024-01393434, 22-02520
    r"|\b[A-Z]{2,6}\d{7,10}\b"  # CVRI2401570, CIVSB2116995, 18CECG00898
    r"|\b\d{2,4}[A-Z]{1,4}\d{5,8}\b"  # 26CV484550, 25D006297, 2024CUOR027466
    r"|\b\d{7,8}\b"  # bare 7-8 digit numbers
)

# Pattern to detect case titles (vs / v.).
_VS_RE = re.compile(r"\bv(?:s)?\.?\s", re.IGNORECASE)

# Pattern matching corporate-entity suffixes that typically terminate a
# defendant party name, e.g. "Orthodontics, Inc.", "Ellis & Son Trucking,
# Inc.", "Acme LLC".  Used to detect fusion boundaries between two adjacent
# cases in multimodal PDF extraction (#2500).  The suffix must be followed
# by a word boundary (whitespace, punctuation, or end of string) so we do
# not match mid-word substrings.  Trailing period is optional since the
# LLM occasionally drops it.
_ENTITY_SUFFIX_RE = re.compile(
    r"\b(?:Inc|LLC|L\.L\.C|Corp|Corporation|Ltd|L\.P|LP|P\.C|PC|Co"
    r"|N\.A|N\.V|LLP|L\.L\.P)\.?(?=$|[\s,;.])",
    re.IGNORECASE,
)

# Patterns for cleaning case title fragments from multimodal extraction.
# OC case numbers follow the format: {county}-{year}-{seq}-{type}-{division}
# e.g., 30-2024-01393434-CU-OR-CJC.  The LLM sometimes splits these across
# newlines in case_info, leaving orphaned fragments.
_CASE_NUMBER_FRAGMENT_RE = re.compile(
    r"""
    \b\d{2,4}-\d{4}-     # county-year prefix with trailing dash (e.g. "30-2024-")
    | \b\d{2,4}-(?=\s|$)  # bare county prefix residual (e.g. "30-")
    | -[A-Z]{2,4}-        # type code fragment (e.g. "-CU-", "-CL-", "-PR-")
    | \b[A-Z]{2}-[A-Z]{3}\b  # division-court suffix (e.g. "OR-CJC")
    """,
    re.VERBOSE,
)

# Court and county name fragments that sometimes appear in case_info.
_COURT_NAME_RE = re.compile(
    r"Superior\s+Court\s+of\s+(?:the\s+)?(?:State\s+of\s+)?California"
    r"|County\s+of\s+\w+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Post-processing: calendar header detection and deduplication (#2096)
# ---------------------------------------------------------------------------

# Pattern to detect calendar header / boilerplate text that the LLM sometimes
# returns as ruling_text.  These contain the court name + "TENTATIVE RULINGS"
# + department info, and are not actual case rulings.
_CALENDAR_HEADER_RE = re.compile(
    r"TENTATIVE\s+RULINGS?\s+"
    r"(?:DEPARTMENT|DEPT\.?)\s+[A-Z]?\d+",
    re.IGNORECASE,
)

# Minimum ruling text length to consider for deduplication.  Very short texts
# (e.g. "GRANTED." or "DENIED.") may legitimately appear on multiple cases.
_DEDUP_MIN_LENGTH = 200

# Minimum substantive ruling text length for a ruling to be considered real
# when its case title matches a citation pattern.  Real tentative rulings are
# multi-paragraph analyses; citation artifacts are short blurbs summarising
# the cited order's outcome.  See #2448.
_CITATION_MIN_RULING_LENGTH = 400

# Signals that an ``extracted_case_title`` is a legal citation to another
# court's order rather than a primary case on the calendar.  Matches anywhere
# in the title because the LLM sometimes prepends or interleaves citator
# tokens with the caption.  See #2448.
_CITATION_TITLE_SUFFIX_RE = re.compile(
    r"("
    # Federal district-court reporters: C.D. Cal., N.D. Cal., E.D. Cal., S.D. Cal.
    r"\b[CNES]\.?\s*D\.?\s*Cal\.?\b"
    # California appellate reporter: "Cal.App.5th", "Cal. App. 4th", etc.
    r"|\bCal\.?\s*App\b"
    # California supreme-court reporter: "9 Cal.5th 1", "123 Cal.4th 456".
    r"|\b\d{1,3}\s+Cal\.?\s*\d+(?:st|nd|rd|th)?\b"
    # Federal supplement reporter: "F.Supp", "F. Supp. 2d", "F.Supp.3d".
    r"|\bF\.?\s*Supp\b"
    # Federal reporter volumes: "F.2d", "F.3d", "F.4th".
    r"|\bF\.\s*\d+(?:d|th)?\b"
    # California reporter: "Cal.Rptr.", "Cal. Rptr. 3d".
    r"|\bCal\.?\s*Rptr\b"
    # Federal case-number form: "8:22-cv-01092", "5:21-cv-01346".
    r"|\b\d+:\d{2}-cv-\d+"
    # LA Superior Court old-format BC number (e.g. BC722351).
    r"|\bBC\d{6,}\b"
    # San Diego Superior Court compact form: "18CV475JM(BGS)".
    r"|\b\d{2}CV\d{3,}[A-Z]{2,3}\s*\([A-Z]{2,3}\)"
    r")",
    re.IGNORECASE,
)

# Signals that an ``extracted_case_number`` is a citation to another
# court's case rather than a primary case on the calendar.  When the LLM
# enumerates cited orders, the case-number field often reveals the other
# county even when the title is clean.  These patterns match LA Superior
# Court formats — LASC is the single most common source of cited cases in
# Song-Beverly Act attorney-fee orders.  Other counties' formats are
# intentionally not enumerated here to keep the filter conservative;
# title-based detection will catch most of them.  See #2448.
_CITATION_CASE_NUMBER_RE = re.compile(
    r"("
    # LASC modern form: 21STCV23811, 22VECV01398, 18LBCV04321, etc.
    # Two-digit year + LASC district code + 5-6 digit serial.
    # LASC cases are never primary on the non-LA scrapers that run through
    # this LLM path (LA uses REGEX extraction), so filtering these never
    # drops legitimate primary cases.
    r"\b\d{2}(?:STCV|STCP|STLC|STPB|STPR|VECV|VECP|LBCV|LBCP|"
    r"SMCV|SMCP|CMCV|CMCP|NWCV|NWCP|PSCV|PSCP|BBCV|BBCP|CHCV|CHCP|"
    r"GDCV|GDCP|TRCV|TRCP|AVCV|AVCP)\d{4,6}\b"
    # LASC old format: BC\d{6,}, BS\d{6,}, SC\d{6,}, VC\d{6,}, PC\d{6,}, LC\d{6,}.
    r"|\b(?:BC|BS|SC|VC|PC|LC|KC|NC|GC|MC|EC|YC)\d{6,}\b"
    r")",
    re.IGNORECASE,
)


def _is_citation_artifact(ruling: ExtractedRuling) -> bool:
    """Return True if ``ruling`` looks like a citation to another court's order.

    The LLM occasionally misinterprets inline citations inside a Request for
    Judicial Notice (common in Song-Beverly attorney-fee orders) as separate
    cases on the calendar, producing 10-24 "rulings" for a PDF that has only
    one primary case.  See #2448.

    A ruling is flagged as a citation artifact when BOTH signals fire:

    1. Either its ``extracted_case_title`` contains a citator pattern
       (``_CITATION_TITLE_SUFFIX_RE``) OR its ``extracted_case_number``
       matches an out-of-county LASC pattern (``_CITATION_CASE_NUMBER_RE``).
    2. Its ``ruling_text`` is shorter than ``_CITATION_MIN_RULING_LENGTH``
       (or is ``None``).

    The length conjunction is deliberately conservative — legitimate short
    rulings (e.g. ``outcome=off_calendar`` with a one-sentence disposition)
    have clean case captions without citator suffixes and are not filtered.
    Legitimate long rulings with an unusually-formatted title are also
    exempted to avoid false positives on real cases.
    """
    title = ruling.extracted_case_title
    case_number = ruling.extracted_case_number

    title_hit = bool(title and _CITATION_TITLE_SUFFIX_RE.search(title))
    case_number_hit = bool(case_number and _CITATION_CASE_NUMBER_RE.search(case_number))
    if not title_hit and not case_number_hit:
        return False

    text = ruling.ruling_text
    if text is None:
        return True
    return len(text) < _CITATION_MIN_RULING_LENGTH


def _filter_citation_artifacts(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Remove citation artifacts from a list of extracted rulings (#2448).

    Applies :func:`_is_citation_artifact` to each ruling.  Preserves the
    relative order of kept rulings.

    Safeguards:

    * If the input has 0 or 1 entries, return it unchanged.  A single-ruling
      document is never filtered down to zero — even if the one ruling
      happens to match the citation pattern, emitting zero rulings would
      produce an orphan document downstream.
    * If every ruling in a multi-entry list is flagged as a citation
      artifact, keep the single longest-text candidate as a fallback.  This
      prevents ambiguous signals from emptying out a document.

    Emits one ``llm_extractor.citation_filtered`` structured log per
    removed ruling, plus a ``llm_extractor.citation_filter_fallback`` log
    when the fallback path fires.
    """
    if len(rulings) <= 1:
        return rulings

    kept: list[ExtractedRuling] = []
    removed: list[ExtractedRuling] = []
    for ruling in rulings:
        if _is_citation_artifact(ruling):
            removed.append(ruling)
            logger.info(
                "llm_extractor.citation_filtered",
                case_title=ruling.extracted_case_title,
                case_number=ruling.extracted_case_number,
                text_length=len(ruling.ruling_text) if ruling.ruling_text else 0,
            )
        else:
            kept.append(ruling)

    if not kept:
        # Fallback: every candidate looked like a citation.  Keep the one
        # with the longest ruling_text — it's the most likely real ruling.
        longest = max(
            removed,
            key=lambda r: len(r.ruling_text) if r.ruling_text else 0,
        )
        logger.warning(
            "llm_extractor.citation_filter_fallback",
            original_count=len(rulings),
            kept_case_title=longest.extracted_case_title,
            kept_text_length=len(longest.ruling_text) if longest.ruling_text else 0,
        )
        return [longest]

    return kept


def _is_calendar_header(text: str | None) -> bool:
    """Return True if ``text`` looks like a calendar header, not a ruling.

    Calendar headers contain boilerplate like "TENTATIVE RULINGS DEPARTMENT C27"
    and are not actual case ruling text.  The LLM sometimes assigns these as
    ``ruling_text`` for individual cases (#2096).
    """
    if not text:
        return False
    return bool(_CALENDAR_HEADER_RE.search(text))


def _deduplicate_ruling_texts(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Null out duplicate ruling_text across cases in the same PDF (#2096).

    When the LLM produces the same ruling text for multiple different cases,
    only the first occurrence keeps its text.  Subsequent duplicates get
    ``ruling_text = None`` so they are not stored as cross-contaminated.

    Short texts (< ``_DEDUP_MIN_LENGTH`` chars) are exempt because legitimate
    identical rulings (e.g. "GRANTED." or "Motion is denied.") can appear
    on multiple cases.

    Entries with ``cross_reference_source`` set are exempt because they
    intentionally share ruling text via cross-reference resolution (#2317).
    """
    import hashlib

    seen_hashes: dict[str, int] = {}  # hash -> index of first occurrence
    for i, ruling in enumerate(rulings):
        text = ruling.ruling_text
        if not text or len(text) < _DEDUP_MIN_LENGTH:
            continue
        # Skip cross-reference entries — they share text by design (#2317).
        if ruling.cross_reference_source is not None:
            continue
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if text_hash in seen_hashes:
            # Duplicate — null out the text on this ruling.
            # Use model_copy to preserve all fields (extracted_parties,
            # motion_type, outcome, case_type, confidence, etc.).
            rulings[i] = ruling.model_copy(update={"ruling_text": None})
            logger.warning(
                "llm_extractor.dedup_ruling_text",
                case_number=ruling.extracted_case_number,
                duplicate_of_index=seen_hashes[text_hash],
                text_length=len(text),
            )
        else:
            seen_hashes[text_hash] = i

    return rulings


# ---------------------------------------------------------------------------
# Post-processing: concatenated case-title truncation (#2562)
# ---------------------------------------------------------------------------
#
# Santa Clara (and occasionally other multi-case-PDF counties) sometimes
# produces an ``extracted_case_title`` that concatenates adjacent calendar
# lines — e.g.
#
#   "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando "
#   "Contreras et al. Jin Yin, et al vs Xiaoxiao Lu, et al."
#
# This produces one ``ExtractedRuling`` whose ``extracted_case_title``
# contains 2+ adversarial separators (``v.`` / ``vs.`` / bare ``vs``).  The
# deterministic rule ``check_no_multiple_adversarial_patterns`` (#2398)
# already flags this, but flagged rulings are still written to the DB.  To
# prevent contaminated titles from reaching ``derived.cases``, this
# post-processor rewrites the title in-place to keep only the first
# ``A v. B`` caption.  It runs alongside ``_deduplicate_ruling_texts`` in
# every code path that emits rulings: the main extractor path and both
# cache-hit filter helpers.
#
# Design notes:
#
# * We only truncate when there are 2+ adversarial separators.  A single
#   ``v.`` or ``vs.`` is a clean caption and is passed through.
# * We cut at the boundary BEFORE the second separator, not at the first
#   separator — keeping the "A v. B" caption intact (including any trailing
#   ``, et al.`` on the defendant side).  We then trim stray boundary
#   punctuation so the result reads as a complete caption.
# * The truncation is deterministic (pure regex + string ops) — no LLM
#   round-trip, safe to apply on every extraction path including cache hits.

# Case-insensitive separator pattern used by the truncator.  Matches the
# same three forms as ``_MULTI_VS_PATTERN`` in ``validation/deterministic.py``
# so the deterministic flag rule and this truncator agree on what counts as
# an adversarial separator:
#   - ``v.`` + whitespace          (e.g. "Smith v. Jones")
#   - ``vs.`` + whitespace         (e.g. "TAYLOR VS. AMAZON")
#   - whitespace + ``vs`` + whitespace  (bare ``vs``, e.g. "Yin vs Lu")
_TITLE_SEPARATOR_RE = re.compile(
    r"(?:\bv\.\s|\bvs\.\s|\s+vs\s+)",
    re.IGNORECASE,
)


def _truncate_concatenated_title(title: str | None) -> str | None:
    """Return *title* truncated to its first ``A v. B`` caption (#2562).

    If *title* contains fewer than 2 adversarial separators, it is returned
    unchanged.  Otherwise, the string is split at the boundary BEFORE the
    second caption's plaintiff, and everything from that boundary onward
    is dropped.

    The heuristic:

    1. Find all separator match spans using ``_TITLE_SEPARATOR_RE``.  If
       there are 0 or 1 matches, return *title* unchanged — the caller
       already has a clean (or obviously-missing) caption.
    2. The first match anchors the first caption.  Between the end of the
       first separator and the start of the second separator sits the
       first caption's defendant + the second caption's plaintiff (fused
       by the LLM).  Walk this middle region to find the natural
       boundary, in priority order:
       - a trailing ``et al.`` / ``et al`` / ``et. al.`` token — cut
         immediately after it
       - a sentence-terminating ``.``, ``!``, or ``?`` followed by
         whitespace — cut after the terminator
       - otherwise, walk LEFT from the second separator over whitespace
         and one non-whitespace token (the second caption's plaintiff
         name, e.g. "Doe" in ``"Jones Doe v. Roe"``), and cut at the
         resulting position.  This keeps the first caption's defendant
         intact.
    3. Strip trailing whitespace and any stray boundary punctuation
       (``,``/``;``/``:``) so the result reads as a single caption.

    ``None`` and empty/whitespace-only strings are returned unchanged.
    """
    if title is None:
        return None
    if not title.strip():
        return title

    matches = list(_TITLE_SEPARATOR_RE.finditer(title))
    if len(matches) < 2:
        return title

    first_end = matches[0].end()
    second_start = matches[1].start()
    # The fused region between the end of the first separator and the
    # start of the second separator.  This region contains the first
    # caption's defendant name followed by the second caption's plaintiff
    # name, with no delimiter.
    middle = title[first_end:second_start]

    # Priority 1: ``et al`` anchor inside the fused region.  Cut after
    # the last ``et al`` occurrence — this keeps the full first-caption
    # defendant (including ``, et al.``) and strips the fused plaintiff
    # that follows.
    boundary_re = re.compile(r"\bet\.?\s*al\.?", re.IGNORECASE)
    anchor_match: re.Match[str] | None = None
    for anchor in boundary_re.finditer(middle):
        anchor_match = anchor  # keep the last occurrence within `middle`
    if anchor_match is not None:
        cut = first_end + anchor_match.end()
        result = title[:cut]
    else:
        # Priority 2: sentence terminator followed by whitespace inside
        # the fused region.  Cut after the terminator.
        terminator_re = re.compile(r"[.!?]\s+")
        terminator_match: re.Match[str] | None = None
        for t in terminator_re.finditer(middle):
            terminator_match = t
        if terminator_match is not None:
            cut = first_end + terminator_match.start() + 1
            result = title[:cut]
        else:
            # Priority 3: strip the trailing token in `middle`.  That
            # token is the second caption's plaintiff name that was
            # fused onto the end of the first caption's defendant.
            stripped_middle = middle.rstrip()
            # Find the last whitespace boundary in the stripped middle.
            # Everything AFTER that boundary is the second caption's
            # plaintiff name — strip it off.
            ws_iter = list(re.finditer(r"\s+", stripped_middle))
            if ws_iter:
                last_ws = ws_iter[-1]
                # Cut at the start of the final whitespace run.
                cut = first_end + last_ws.start()
                result = title[:cut]
            else:
                # The fused region is a single token (e.g. "B Doe"
                # would split; "BDoe" would not).  Fall back to keeping
                # only the first-separator + no defendant — this is the
                # degenerate case and is rare in real data.
                result = title[:first_end].rstrip()
                if re.search(r"\bv[s]?\.?\s*$", result, re.IGNORECASE):
                    return title  # degenerate fallback would lose defendant; keep fused title

    # Final cleanup: strip trailing whitespace and stray connector
    # punctuation that can be left behind at the boundary.
    result = result.rstrip()
    result = re.sub(r"[,;:]+$", "", result).rstrip()
    return result


def _truncate_concatenated_case_titles(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Truncate ``extracted_case_title`` to first caption when concatenated (#2562).

    Runs ``_truncate_concatenated_title`` on each ruling's title and rewrites
    the ruling via ``model_copy`` when the title changes.  Preserves every
    other field.  Logs one ``llm_extractor.truncate_concatenated_title``
    record per rewritten title so production can observe the guard's
    activity.

    This post-processor is idempotent — re-applying it to an already-clean
    output is a no-op.
    """
    for i, ruling in enumerate(rulings):
        original = ruling.extracted_case_title
        truncated = _truncate_concatenated_title(original)
        if truncated != original:
            rulings[i] = ruling.model_copy(update={"extracted_case_title": truncated})
            logger.warning(
                "llm_extractor.truncate_concatenated_title",
                case_number=ruling.extracted_case_number,
                original_length=len(original) if original else 0,
                truncated_length=len(truncated) if truncated else 0,
                original_title=original,
                truncated_title=truncated,
            )
    return rulings


# ---------------------------------------------------------------------------
# Post-processing: repeated-name tail sanitizer (#3684)
# ---------------------------------------------------------------------------
#
# Some LLM extractions produce an ``extracted_case_title`` that duplicates the
# party-name tail verbatim — for example::
#
#   "Hearns vs. FCA US, LLC. Tenaya Hearns Tenaya Hearns Tenaya Hearns Tenaya Hearns"
#
# should be sanitized to::
#
#   "Hearns vs. FCA US, LLC."
#
# The guard detects a tail-run of capitalized N-gram repetitions (N in 1–3),
# requires at least 2 repetitions, and strips the repeated tail, keeping only
# the first occurrence.  The capitalization gate prevents accidental truncation
# of lowercase connector phrases like "Department of Maintenance Maintenance".


def _truncate_repeated_name_tail(title: str | None) -> str | None:
    """Return *title* with a repeated capitalized N-gram tail stripped (#3684).

    The heuristic:

    1. Return ``None`` / empty / whitespace-only inputs unchanged.
    2. Tokenize on whitespace.  For N in ``(3, 2, 1)`` (longest-first to
       avoid a 1-gram match shadowing a legitimate 3-gram match):
       a. Require at least ``2 * N`` tokens.
       b. Require the candidate N-gram (last N tokens) to be all capitalized
          (first char uppercase).  This guards against truncating lowercase-
          connector phrases such as ``"Department of Maintenance Maintenance"``.
       c. Walk backwards to count the number of consecutive repetitions of
          the N-gram at the tail.  Require at least 2 repetitions.
       d. If exactly 2 repetitions are found, keep the first occurrence:
          cut at ``first_rep_start + N`` (i.e. after the first occurrence).
          This covers ``"...Maintenance Corporation Maintenance Corporation"``
          → ``"...Maintenance Corporation"``.
       e. If 3 or more repetitions are found, strip the entire repeated run
          (including the first occurrence): cut at ``first_rep_start``.
          This covers the Hearns pattern where 4 identical 2-grams appear and
          ALL of them are clearly artifact (``"LLC. Tenaya Hearns × 4"``).
       f. Strip trailing whitespace and stray connector punctuation (``,;:``).
    3. If no repetition is found, return *title* unchanged.

    Idempotent: applying this function twice produces the same result as
    applying it once.
    """
    if title is None:
        return None
    if not title.strip():
        return title

    tokens = title.split()
    total = len(tokens)

    for n in (3, 2, 1):
        if total < 2 * n:
            continue
        # Candidate N-gram is the last N tokens.
        candidate = tokens[-n:]
        # Capitalization gate: every token in the candidate N-gram must start
        # with an uppercase character.
        if not all(tok and tok[0].isupper() for tok in candidate):
            continue
        # Walk backwards counting consecutive repetitions.
        rep_count = 0
        pos = total
        while pos >= n and tokens[pos - n : pos] == candidate:
            rep_count += 1
            pos -= n
        if rep_count < 2:
            continue
        # first_rep_start is the index where the FIRST occurrence of the
        # repeated N-gram starts.
        first_rep_start = pos
        if rep_count == 2:
            # Keep the first occurrence; strip only the duplicate tail copy.
            cut = first_rep_start + n
        else:
            # 3+ repetitions — strip the entire run, including first copy.
            cut = first_rep_start
        result = " ".join(tokens[:cut])
        result = result.rstrip()
        result = re.sub(r"[,;:]+$", "", result).rstrip()
        return result

    return title


def _truncate_repeated_name_tails(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Strip repeated capitalized name tails from ``extracted_case_title`` (#3684).

    Runs ``_truncate_repeated_name_tail`` on each ruling's title and rewrites
    the ruling via ``model_copy`` when the title changes.  Preserves every
    other field.  Logs one ``llm_extractor.truncate_repeated_name_tail``
    record per rewritten title so production can observe the guard's activity.

    This post-processor is idempotent — re-applying it to already-clean output
    is a no-op.
    """
    for i, ruling in enumerate(rulings):
        original = ruling.extracted_case_title
        truncated = _truncate_repeated_name_tail(original)
        if truncated != original:
            rulings[i] = ruling.model_copy(update={"extracted_case_title": truncated})
            logger.warning(
                "llm_extractor.truncate_repeated_name_tail",
                case_number=ruling.extracted_case_number,
                original_title=original,
                truncated_title=truncated,
            )
    return rulings


# ---------------------------------------------------------------------------
# Post-processing: Riverside title motion-tail sanitizer (#2564)
# ---------------------------------------------------------------------------
#
# Riverside LLM extractions sometimes produce an ``extracted_case_title`` that
# absorbs the motion heading following the party-name caption.  The LLM reads
# the PDF caption line (e.g. ``WILLARD VS HYUNDAI MOTOR AMERICA``) together
# with the motion description that immediately follows (e.g.
# ``MOTION FOR ATTORNEY'S FEES BY JAMES WILLARD``), and joins them with a
# second ``vs.`` separator to produce a contaminated title like:
#
#   ``"WILLARD VS HYUNDAI MOTOR AMERICA vs. MOTION FOR ATTORNEY'S FEES BY JAMES WILLARD"``
#
# The sanitizer detects this pattern by looking for a ``v.`` / ``vs.`` token
# followed by a recognized motion keyword, and strips everything from that
# separator onward.  The result is the clean party-names caption.

# Matches a second adversarial separator (``v.`` / ``vs.``) followed
# immediately by a motion keyword — the telltale sign of title contamination.
# Applied case-insensitively; anchored at the separator so the preceding
# party-name text is preserved intact.
_MOTION_HEADING_TAIL_RE = re.compile(
    r"\s+v[s]?\.\s+(?:Motion|Msc|Petition|Demurrer|MSJ|Hearing\s+on|"
    r"To\s+(?:Be|Set|Compel)|For\s+(?:Attorney|Summary))"
    r".*$",
    re.IGNORECASE | re.DOTALL,
)


def _sanitize_title_motion_tail(title: str) -> str:
    """Strip motion-heading tail from a contaminated ``case_title`` (#2564).

    When the Riverside LLM absorbs a motion heading into the case title,
    the result looks like::

        "WILLARD VS HYUNDAI MOTOR AMERICA vs. MOTION FOR ATTORNEY'S FEES BY JAMES WILLARD"

    This function strips everything from the second adversarial separator
    (``v.`` / ``vs.``) onward when it is immediately followed by a motion
    keyword.  Clean titles (single ``v.`` / ``vs.`` separator with no
    following motion keyword) are returned unchanged.

    This is a pure, side-effect-free helper.  Callers are responsible for
    deciding when to apply it (e.g. only for Riverside-origin rulings).
    """
    result = _MOTION_HEADING_TAIL_RE.sub("", title)
    return result.rstrip()


# ---------------------------------------------------------------------------
# Post-processing: Riverside title cost-itemization tail sanitizer (#3555)
# ---------------------------------------------------------------------------
#
# Riverside LLM extractions for Motion to Tax Costs entries sometimes produce
# an ``extracted_case_title`` that absorbs the cost-itemization line items
# following the party-name caption.  For example::
#
#   ``"VELASQUEZ vs MONTENEGRO Court Reporter Fees Interpreter Fees
#      Models, Enlargements and Photocopies of Exhibits"``
#
# should be sanitized to::
#
#   ``"VELASQUEZ vs MONTENEGRO"``
#
# Design constraint: the regex MUST NOT fire on party names that happen to
# contain cost-related words (e.g. ``"ACME COURT REPORTING SERVICES INC"`` as
# plaintiff).  We achieve this by anchoring the match to occur AFTER a
# complete ``WORD vs[.] WORD`` or ``WORD v. WORD`` caption pattern — i.e.
# after at least two party-name tokens separated by a ``v.``/``vs.`` separator.
# The cost-item keywords must appear AFTER the second party token ends.

# Matches cost-itemization suffixes that follow a complete party-name caption.
# The leading lookahead `(?<=\S)` ensures there is at least one preceding
# non-whitespace character (the end of the second party name).  We then
# require a word boundary followed by a cost-item keyword.  The match
# extends to end-of-string so the entire tail is stripped.
#
# Applied after ``_MOTION_HEADING_TAIL_RE``, so the title at this point is
# either already clean or still has a raw cost-item suffix (not a motion
# heading).
#
# NOTE: bare single-word tokens ``Costs`` and ``Fees`` are intentionally
# excluded from this regex even though they appear in the prompt stop-token
# list (rule 4a).  The LLM has full context to disambiguate them; the
# deterministic sanitizer does not.  A bare ``Fees`` or ``Costs`` token could
# legitimately be part of a defendant company name (e.g. "JONES vs FEES INC"),
# so including them would cause false positives.  In the real contamination
# fixture (VELASQUEZ vs MONTENEGRO), both words appear only AFTER the
# multi-word anchor ``Court Reporter``, which triggers ``.*$`` to consume them.
_COST_ITEMIZATION_TAIL_RE = re.compile(
    r"(?<=\S)"  # must follow a non-whitespace char (end of party name)
    r"\s+"  # one or more spaces separating party name from cost item
    r"\b(?:Court\s+Reporter|Interpreter\s+Fees?|Models,?\s+Enlargements|"
    r"Photocopies(?:\s+of(?:\s+Exhibits)?)?)\b"
    r".*$",
    re.IGNORECASE | re.DOTALL,
)

# Guard pattern: ensures the title contains at least one adversarial separator
# (``v.`` or ``vs.``) before we apply cost-item stripping.  Without this check
# a single-token title like ``"Fees"`` would be incorrectly reduced to ``""``.
_HAS_ADVERSARIAL_SEPARATOR_RE = re.compile(r"\bvs?\.?\s", re.IGNORECASE)


def _sanitize_title_cost_itemization_tail(title: str) -> str:
    """Strip cost-itemization tail from a contaminated ``case_title`` (#3555).

    When the Riverside LLM absorbs a Motion-to-Tax-Costs cost itemization list
    into the case title, the result looks like::

        "VELASQUEZ vs MONTENEGRO Court Reporter Fees Interpreter Fees Models, ..."

    This function strips everything from the first cost-item keyword onward,
    but ONLY when:

    1. The title contains at least one ``v.``/``vs.`` adversarial separator
       (ensuring it is a multi-party caption, not a single party name).
    2. The cost-item keyword appears AFTER the second party token (enforced
       via the ``(?<=\\S)\\s+`` prefix in the regex).

    This prevents stripping legitimate party names such as
    ``"ACME COURT REPORTING SERVICES INC vs JONES"`` where the cost-item
    words appear inside the plaintiff name before the separator.

    This is a pure, side-effect-free helper.  Callers are responsible for
    deciding when to apply it (e.g. only for Riverside-origin rulings).
    """
    # Only apply if the title has at least one adversarial separator.
    if not _HAS_ADVERSARIAL_SEPARATOR_RE.search(title):
        return title
    result = _COST_ITEMIZATION_TAIL_RE.sub("", title)
    return result.rstrip()


# ---------------------------------------------------------------------------
# Post-processing: Riverside cross-case ruling_text truncation (#2564)
# ---------------------------------------------------------------------------
#
# Riverside LLM extractions sometimes produce a ``ruling_text`` that bleeds
# across case boundaries — the LLM fails to stop at the next numbered-entry
# line (e.g. ``2.``) and continues transcribing the next case's content.
# The result is a single ``ruling_text`` that contains the full ruling for
# case N followed immediately by the header and ruling for case N+1.
#
# This is detectable because the text for the next case begins with a foreign
# Riverside case number (a case number different from the ruling's own
# ``extracted_case_number``).  The sanitizer truncates at the first
# occurrence of such a foreign case number.
#
# Design notes:
#
# * ``own_case_number`` is normalised before comparison so ``CVRI2500796``
#   matches regardless of whether the text has ``CVRI 2500796`` or
#   ``cvri2500796``.
# * The function is generic over any ``case_number_re`` pattern so it can
#   be unit-tested without a live Riverside fixture and potentially reused
#   for other counties in the future.


def _truncate_cross_case_ruling_text(
    text: str,
    *,
    own_case_number: str | None,
    case_number_re: re.Pattern,
) -> str:
    """Truncate *text* at the first foreign Riverside case number (#2564).

    Scans *text* for all case-number matches using *case_number_re*.  For
    each match, if the normalised match value differs from
    *own_case_number* (normalised), truncate *text* at the start of that
    match and return the prefix.  If no foreign case number is found,
    *text* is returned unchanged.

    Parameters
    ----------
    text:
        The ``ruling_text`` value to sanitize.
    own_case_number:
        The ruling's own case number (e.g. ``"CVRI2500796"``).  When
        ``None``, any matching case number is considered foreign and
        triggers truncation.
    case_number_re:
        Compiled regex pattern that matches candidate Riverside case
        numbers.  Callers should pass the Riverside-specific pattern
        (``CV[A-Z]{2,4}`` + ``RIC/MCC/...`` prefixes) to avoid false
        positives from other county formats.

    Returns
    -------
    str
        The sanitized ruling text (possibly unchanged).
    """
    own_norm = own_case_number.upper().strip() if own_case_number else None

    for m in case_number_re.finditer(text):
        candidate = m.group(0).upper().strip()
        if own_norm is None or candidate != own_norm:
            # Found a foreign case number — truncate here.
            return text[: m.start()]

    return text


def _sanitize_riverside_rulings(
    rulings: list[ExtractedRuling],
    *,
    case_number_re: re.Pattern,
) -> list[ExtractedRuling]:
    """Apply Riverside-specific title and ruling_text sanitizers (#2564, #3555).

    Applies three deterministic post-processors to every ruling in *rulings*:

    1. :func:`_sanitize_title_motion_tail` — strips motion-heading tails
       from ``extracted_case_title`` when the LLM absorbed a motion header
       into the title field.
    2. :func:`_sanitize_title_cost_itemization_tail` — strips cost-itemization
       line items (e.g. "Court Reporter Fees Interpreter Fees Models, ...") from
       ``extracted_case_title`` when the LLM absorbed cost-item suffixes from a
       Motion to Tax Costs entry into the title field (#3555).
    3. :func:`_truncate_cross_case_ruling_text` — truncates ``ruling_text``
       at the first occurrence of a foreign Riverside case number, preventing
       bleed-across of ruling text from the next case in the PDF.

    All sanitizers are pure helpers; this function wires them over a list
    and emits structured log entries for each change so production can
    observe the guard's activity.

    This post-processor is idempotent — re-applying it to already-clean
    rulings is a no-op.
    """
    for i, ruling in enumerate(rulings):
        updates: dict = {}

        # --- Title sanitization ---
        original_title = ruling.extracted_case_title
        if original_title:
            clean_title = _sanitize_title_motion_tail(original_title)
            if clean_title != original_title:
                updates["extracted_case_title"] = clean_title
                logger.info(
                    "llm_extractor.title_motion_tail_stripped",
                    case_number=ruling.extracted_case_number,
                    before=original_title,
                    after=clean_title,
                )

            # Apply cost-itemization tail stripping on the (possibly already
            # cleaned) title so both guards compose correctly.
            title_after_motion_strip = updates.get("extracted_case_title", original_title)
            clean_title_ci = _sanitize_title_cost_itemization_tail(title_after_motion_strip)
            if clean_title_ci != title_after_motion_strip:
                updates["extracted_case_title"] = clean_title_ci
                logger.info(
                    "llm_extractor.title_cost_itemization_tail_stripped",
                    case_number=ruling.extracted_case_number,
                    before=title_after_motion_strip,
                    after=clean_title_ci,
                )

        # --- Cross-case ruling_text truncation ---
        original_text = ruling.ruling_text
        if original_text:
            clean_text = _truncate_cross_case_ruling_text(
                original_text,
                own_case_number=ruling.extracted_case_number,
                case_number_re=case_number_re,
            )
            if clean_text != original_text:
                # Identify the foreign case number that triggered truncation.
                foreign_match = case_number_re.search(original_text[len(clean_text) :])
                foreign_cn = foreign_match.group(0) if foreign_match else "unknown"
                updates["ruling_text"] = clean_text
                logger.warning(
                    "llm_extractor.ruling_text_truncated_at_foreign_case_number",
                    case_number=ruling.extracted_case_number,
                    foreign_case_number=foreign_cn,
                    before_len=len(original_text),
                    after_len=len(clean_text),
                )

        if updates:
            rulings[i] = ruling.model_copy(update=updates)

    return rulings


# ---------------------------------------------------------------------------
# Post-processing: Riverside "No tentative ruling" stub filter (#3715)
# ---------------------------------------------------------------------------
#
# Riverside PDFs sometimes include per-entry bodies that are ONLY a bare
# "No tentative ruling." statement — these are not substantive rulings and
# should not appear in ``derived.rulings``.  They are typically very short
# (< 200 chars) and match a recognisable pattern.
#
# The pattern tolerates:
#   - An optional motion-header preamble on one or more lines
#   - An optional "Tentative Ruling:" label prefix
#   - "No tentative ruling" or "No further tentative ruling" variants
#   - Up to 200 chars of trailing boilerplate (", appearances required.", etc.)
#
# Substantive carve-out: if the ruling_text starts with "No tentative ruling"
# but is >= 200 chars overall, it stays with outcome='other' — the judge may
# have added a substantial narrative.
#
# Cross-reference entries are exempt: their ruling_text is intentionally
# sparse because they share text with the referent entry.

_RIVERSIDE_NO_TENTATIVE_RULING_STUB_RE = re.compile(
    r"^\s*(?:[A-Z][\w \-,'\\.]{0,150}\n+)?"
    r"(?:Tentative Ruling:\s*)?No (?:further )?tentative ruling[\s\S]{0,200}$",
    re.IGNORECASE,
)


def _drop_riverside_no_tentative_ruling_stubs(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Drop Riverside 'No tentative ruling.' stub entries from the rulings list (#3715).

    Entries whose ``ruling_text`` consists entirely of a bare
    "No tentative ruling." variant (≤ 200 chars total) are dropped so that
    they do not pollute ``derived.rulings``.

    Rules:
    - Entries with ``cross_reference_source`` set are exempt — they share
      text intentionally via cross-reference resolution and are the
      referent's responsibility.
    - Entries with ``ruling_text`` of ``None`` or empty are preserved.
    - Drop when ``len(ruling_text.strip()) < 200`` AND the stub regex matches
      the entire stripped text.

    This filter is idempotent — applying it to already-filtered rulings is
    a no-op.
    """
    kept: list[ExtractedRuling] = []
    for ruling in rulings:
        if ruling.cross_reference_source is not None:
            kept.append(ruling)
            continue
        if not ruling.ruling_text:
            kept.append(ruling)
            continue
        stripped = ruling.ruling_text.strip()
        if len(stripped) < 200 and _RIVERSIDE_NO_TENTATIVE_RULING_STUB_RE.match(stripped):
            logger.warning(
                "llm_extractor.riverside_no_tentative_stub_dropped",
                case_number=ruling.extracted_case_number,
                length=len(stripped),
                preview=stripped[:80],
            )
            continue
        kept.append(ruling)
    return kept


# ---------------------------------------------------------------------------
# Post-processing: San Bernardino title and ruling_text sanitizers (#2565)
# ---------------------------------------------------------------------------
#
# San Bernardino LLM extractions can produce two contamination patterns:
#
# 1. **Concatenated captions**: the LLM fuses adjacent case captions from a
#    multi-case PDF into one ``extracted_case_title``, e.g.
#    ``"Smith v. Jones Doe v. Roe"``.  Fixed by the generic
#    ``_truncate_concatenated_case_titles`` (which already runs on every
#    county).
#
# 2. **Cross-case ruling_text bleed**: the LLM fails to stop at the
#    horizontal-rule / repeated-header boundary and copies the next case's
#    content into the current ruling's ``ruling_text``.  Fixed by
#    ``_truncate_cross_case_ruling_text`` parametrised with the SB regex.
#
# 3. **Role-literal titles**: the LLM emits ``"Plaintiff v. Defendant"``
#    instead of the real party names.  Fixed by
#    ``_rebuild_title_from_parties`` when ``extracted_parties`` is populated.
#
# This is an exact structural parallel of ``_sanitize_riverside_rulings``
# from #2564.  The two sanitisers are intentionally kept as separate
# county-scoped wrappers (not merged into a generic helper) to make per-county
# log markers precise and to keep each county's configuration self-contained.


def _disjoint_plaintiff_names(parties_a: list, parties_b: list) -> bool:
    """Return True when the plaintiff name sets of two party lists are disjoint (#3791).

    Used to detect the inherited-case-number misattribution shape in San Bernardino
    multi-case PDFs: when two rulings share a CIVSB/CIVRS case number but have
    completely different plaintiff names, the LLM likely inherited the case number
    from the prior section rather than finding a distinct case number in the text.

    Conservative behaviour: returns ``False`` (don't fire) when either side has
    no plaintiffs, so the guard never fires on sparse data.

    Parameters
    ----------
    parties_a:
        Plaintiff party list for the anchor ruling.
    parties_b:
        Plaintiff party list for the subsequent ruling.

    Both lists may contain ``ExtractedParty`` pydantic instances **or** plain
    ``dict`` objects with ``name`` and ``role`` keys (the LA path).

    Returns
    -------
    bool
        ``True`` only when both sides have at least one plaintiff and all
        plaintiff names from *parties_a* are absent from *parties_b*
        (case-insensitively).
    """

    def _plaintiff_names(parties: list) -> set[str]:
        names: set[str] = set()
        for party in parties:
            if isinstance(party, dict):
                role = (party.get("role") or "").lower()
                name = (party.get("name") or "").strip()
            else:
                role = (getattr(party, "role", None) or "").lower()
                name = (getattr(party, "name", None) or "").strip()
            if role in {"plaintiff", "petitioner"} and name:
                names.add(name.lower())
        return names

    names_a = _plaintiff_names(parties_a)
    names_b = _plaintiff_names(parties_b)

    # Conservative: if either set is empty we cannot make a reliable comparison
    if not names_a or not names_b:
        return False

    return names_a.isdisjoint(names_b)


def _rebuild_title_from_parties(
    title: str | None,
    parties: list,
) -> str | None:
    """Rebuild a role-literal ``case_title`` from ``extracted_parties`` (#2565).

    If *title* matches ``_ROLE_LITERAL_TITLE_RE`` (e.g. ``"Plaintiff v. Defendant"``),
    attempt to reconstruct ``"<first-plaintiff> v. <first-defendant>"`` from
    *parties*.  Returns ``None`` when the title does not match the pattern or
    when the required parties are not available (so the caller can leave the
    field unchanged or emit a warning).

    Parameters
    ----------
    title:
        The raw ``extracted_case_title`` from the LLM.
    parties:
        The ``extracted_parties`` list from the same ruling.  Elements are
        ``ExtractedParty`` instances (or duck-typed equivalents) with ``name``
        and ``role`` attributes.

    Returns
    -------
    str | None
        Rebuilt title string, or ``None`` if the pattern does not match or
        parties are insufficient to rebuild.
    """
    if not title or not (
        _ROLE_LITERAL_TITLE_RE.match(title) or _BRACKETED_PLACEHOLDER_TITLE_RE.search(title)
    ):
        return None

    # Determine which role pair to look for.  Petitions use petitioner/respondent;
    # everything else uses plaintiff/defendant.
    title_lower = title.lower().strip()
    if title_lower.startswith("petitioner"):
        plaintiff_roles = {"petitioner"}
        defendant_roles = {"respondent"}
    else:
        plaintiff_roles = {"plaintiff"}
        defendant_roles = {"defendant"}

    plaintiff_name: str | None = None
    defendant_name: str | None = None
    for party in parties:
        if isinstance(party, dict):
            role = party.get("role") or ""
            name = party.get("name") or ""
        else:
            role = getattr(party, "role", None) or ""
            name = getattr(party, "name", None) or ""
        if not name:
            continue
        if role.lower() in plaintiff_roles and plaintiff_name is None:
            plaintiff_name = name
        elif role.lower() in defendant_roles and defendant_name is None:
            defendant_name = name

    if plaintiff_name and defendant_name:
        return f"{plaintiff_name} v. {defendant_name}"
    return None


def _sanitize_san_bernardino_rulings(
    rulings: list[ExtractedRuling],
    *,
    case_number_re: re.Pattern,
) -> list[ExtractedRuling]:
    """Apply San Bernardino-specific title and ruling_text sanitizers (#2565).

    Applies two deterministic post-processors to every ruling in *rulings*
    whose ``extracted_case_number`` matches *case_number_re*, making this
    function a **no-op on non-SB documents**:

    1. :func:`_truncate_cross_case_ruling_text` — truncates ``ruling_text``
       at the first occurrence of a foreign SB case number.
    2. :func:`_rebuild_title_from_parties` — when ``extracted_case_title``
       is a role-literal placeholder (e.g. ``"Plaintiff v. Defendant"``),
       replaces it with the real party names from ``extracted_parties``.

    Concatenated-caption titles are handled upstream by the generic
    ``_truncate_concatenated_case_titles`` which already runs on all counties.

    Both sanitizers are pure helpers; this function wires them over the list
    and emits structured log entries for each change so production can observe
    the guard's activity.

    This post-processor is idempotent — re-applying it to already-clean
    rulings is a no-op.
    """
    for i, ruling in enumerate(rulings):
        # Scope to SB case numbers only — skip non-SB rulings entirely.
        if not ruling.extracted_case_number or not case_number_re.search(
            ruling.extracted_case_number
        ):
            continue

        updates: dict = {}

        # --- Cross-case ruling_text truncation ---
        original_text = ruling.ruling_text
        if original_text:
            clean_text = _truncate_cross_case_ruling_text(
                original_text,
                own_case_number=ruling.extracted_case_number,
                case_number_re=case_number_re,
            )
            if clean_text != original_text:
                foreign_match = case_number_re.search(original_text[len(clean_text) :])
                foreign_cn = foreign_match.group(0) if foreign_match else "unknown"
                updates["ruling_text"] = clean_text
                logger.warning(
                    "llm_extractor.sb_ruling_text_truncated_at_foreign_case_number",
                    case_number=ruling.extracted_case_number,
                    foreign_case_number=foreign_cn,
                    before_len=len(original_text),
                    after_len=len(clean_text),
                )

        # --- Role-literal title rebuild ---
        original_title = ruling.extracted_case_title
        if original_title:
            rebuilt = _rebuild_title_from_parties(original_title, ruling.extracted_parties)
            if rebuilt is not None:
                updates["extracted_case_title"] = rebuilt
                logger.info(
                    "llm_extractor.sb_title_role_literal_rebuilt",
                    case_number=ruling.extracted_case_number,
                    before=original_title,
                    after=rebuilt,
                )

        if updates:
            rulings[i] = ruling.model_copy(update=updates)

    # --- Second pass: inherited-case-number guard (#3791) ---
    # Group rulings by extracted_case_number (SB-scoped only).
    # For any group of size >= 2, compare each subsequent ruling's plaintiffs
    # to the anchor (first) ruling's plaintiffs.  When the sets are disjoint,
    # the LLM likely inherited the case number — null it out and rebuild the
    # title from the subsequent ruling's own parties.
    # Build an index: case_number -> [(position, ruling)]
    cn_groups: dict[str, list[tuple[int, ExtractedRuling]]] = defaultdict(list)
    for i, ruling in enumerate(rulings):
        if ruling.extracted_case_number and case_number_re.search(ruling.extracted_case_number):
            cn_groups[ruling.extracted_case_number].append((i, ruling))

    for shared_cn, group in cn_groups.items():
        if len(group) < 2:
            continue

        _anchor_idx, anchor_ruling = group[0]
        anchor_parties = anchor_ruling.extracted_parties or []

        for pos_idx, (i, ruling) in enumerate(group):
            if pos_idx == 0:
                # Anchor is left unchanged
                continue

            subsequent_parties = ruling.extracted_parties or []
            if not _disjoint_plaintiff_names(anchor_parties, subsequent_parties):
                # Conservative: same plaintiffs (multi-motion shape) or
                # empty anchor — do nothing.
                continue

            # Plaintiff sets are disjoint: this ruling likely inherited the
            # case number from the anchor.  Null out the case number.
            # The title is already extracted from this ruling's own caption
            # and is kept as-is; only extracted_case_number is cleared.
            logger.warning(
                "llm_extractor.sb_inherited_case_number_nullified",
                shared_case_number=shared_cn,
                anchor_plaintiffs=[
                    (p.get("name") if isinstance(p, dict) else getattr(p, "name", None))
                    for p in anchor_parties
                    if (
                        p.get("role") if isinstance(p, dict) else getattr(p, "role", None) or ""
                    ).lower()
                    in {"plaintiff", "petitioner"}
                ],
                subsequent_plaintiffs=[
                    (p.get("name") if isinstance(p, dict) else getattr(p, "name", None))
                    for p in subsequent_parties
                    if (
                        p.get("role") if isinstance(p, dict) else getattr(p, "role", None) or ""
                    ).lower()
                    in {"plaintiff", "petitioner"}
                ],
            )
            rulings[i] = ruling.model_copy(update={"extracted_case_number": None})

    return rulings


# ---------------------------------------------------------------------------
# Post-processing: calendar-listing-only detection (#2446)
# ---------------------------------------------------------------------------

# Some Orange County department PDFs (W8, C10, N14, etc.) are *calendar
# listings* rather than published tentative rulings.  They are titled
# "TENTATIVE RULINGS" but the per-case table cells contain only the motion
# type heading (e.g. "Motion to Strike") or an "OFF-CALENDAR" marker — the
# actual ruling body is not published for that date.  Upstream, the
# multimodal LLM still emits one row per listed case, producing "rulings"
# whose ruling_text is a motion-type label or "OFF-CALENDAR".  These rows
# are not substantive rulings and should be dropped so they do not appear in
# search results or corrupt motion-type analytics.
#
# The filter is a *deterministic* post-processor: it only drops a row when
# the text is unambiguously a non-ruling (OFF-CALENDAR marker, or
# motion-type-only text with no disposition verb), so real short rulings
# (e.g. "Motion to strike is GRANTED.") are preserved.

# OFF-CALENDAR markers — very common on OC department calendars for cases
# where no tentative was actually posted.  Covers variations in hyphenation,
# spacing, casing, and trailing punctuation.
_OFF_CALENDAR_RE = re.compile(
    r"\bOFF[\s\-\u2013\u2014]*CALENDAR\b",
    re.IGNORECASE,
)

# ``O/C`` shorthand for Off Calendar — used in OC department calendars as a
# bare listing marker (#2489).  Matches the entire stripped text so that
# embedded substrings like "GROUP O/C PROCEEDING …" are NOT misclassified.
# The optional leading/trailing whitespace and trailing punctuation allow
# ``O/C``, ``O / C``, ``o/c``, and ``O/C.`` to match.
_OC_ABBREV_RE = re.compile(
    r"\A\s*O\s*/\s*C\s*[.!]?\s*\Z",
    re.IGNORECASE,
)

# "NO TENTATIVE" markers — OC calendar listings for cases where the court
# did not post a tentative ruling.  Like OFF-CALENDAR, these are not real
# rulings and should be dropped.  Covers "NO TENTATIVE", "NO TENTATIVE
# POSTED", and trivial whitespace variations.
_NO_TENTATIVE_RE = re.compile(
    r"\bNO\s+TENTATIVE\b",
    re.IGNORECASE,
)

# Bare parenthetical disposition markers (#2489) — e.g. ``(Moot)``,
# ``(Continued)``, ``(Withdrawn)``, ``(Vacated)``, ``(Off Calendar)``.
# Matches ONLY when the entire stripped text is such a parenthetical (with
# optional trailing period), so that parentheticals embedded in a real
# ruling ("The motion is GRANTED (Continued to 4/20)") are not misclassified.
_BARE_DISPOSITION_RE = re.compile(
    r"\A\s*\(\s*("
    r"Moot|"
    r"Continued|"
    r"Withdrawn|"
    r"Vacated|"
    r"Settled|"
    r"Dropped|"
    r"Dismissed|"
    r"Off[\s\-]*Calendar|"
    r"Taken[\s\-]+Off[\s\-]*Calendar"
    r")\s*\)\s*[.!]?\s*\Z",
    re.IGNORECASE,
)

# Bare continuance-only lines (#2489) — e.g. ``Cont. To 4/20``,
# ``CONTINUED TO 10/6/26``, ``Continued to April 20, 2026``.  These are
# calendar-listing cells that communicate only "this matter is continued"
# with no actual ruling body.  Must be evaluated BEFORE the disposition-verb
# short-circuit, since ``CONTINUED`` matches ``_RULING_VERB_RE`` and would
# otherwise cause bare continuances to be classified as real rulings.
#
# The regex anchors to the full stripped text to avoid matching
# "Motion CONTINUED to April 1 for briefing." (a real ruling that happens
# to contain a continuance clause).  The date portion accepts either a
# numeric date (``4/20``, ``4/20/26``, ``10/6/2026``) or a month-name date
# (``April 20``, ``April 20, 2026``), with optional trailing time/detail
# segment (``at 9:00 a.m.``).
_BARE_CONTINUANCE_RE = re.compile(
    r"\A\s*"
    r"(?:Cont\.?|Continued|CONT)\s+"
    r"(?:to|TO)\s+"
    r"(?:"
    # Numeric date: M/D, M/D/YY, M/D/YYYY
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r"|"
    # Month-name date: "April 20" or "April 20, 2026"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+\d{1,2}(?:,?\s*\d{2,4})?"
    r")"
    # Optional trailing detail (time, courtroom, dept, etc.) — short only.
    r"(?:\s+(?:at|@|in|for|dept\.?|department|courtroom|ctrm\.?)\b.*)?"
    r"\s*[.!]?\s*\Z",
    re.IGNORECASE,
)

# Placeholder text (#2489) — ``Tentative pending``, ``ADR Review``,
# ``ADR Review Hearing``.  These OC calendar cells signal that no tentative
# was posted without using the literal "OFF-CALENDAR" / "NO TENTATIVE"
# phrasing.  Like the other listing markers they are not real rulings.
_PLACEHOLDER_RE = re.compile(
    r"\A\s*("
    r"Tentative\s+pending|"
    r"ADR\s+Review(?:\s+Hearing)?"
    r")\s*[.!]?\s*\Z",
    re.IGNORECASE,
)

# Contra Costa probate calendar-pointer signals (#3609).  These match
# calendar-listing rows from CC dept 30 "alternate sheet" PDFs where every
# numbered entry is a pointer to the full calendar, not a ruling body.
# They are identified by EITHER:
#   (a) a time-prefixed "HEARING IN RE" phrase (``9:00 AM HEARING IN RE:``)
#   (b) the literal pointer phrase ``see also alternate sheet`` / ``see alt sheet``
# The bare "HEARING IN RE" phrase (without a time prefix or alt-sheet pointer)
# is intentionally excluded — non-CC courts also use this phrase in real rulings
# with verbs like ``ruled``, ``ordered``, ``found``, ``held`` that are absent
# from ``_RULING_VERB_RE`` and would cause false-positive drops (#3699).
# These rows may be 110-180 chars — well above _CALENDAR_LISTING_MAX_LENGTH —
# so they MUST be matched by an explicit pointer check that bypasses the
# length gate.
_PROBATE_CALENDAR_LISTING_RE = re.compile(
    r"(?:"
    r"\d{1,2}:\d{2}\s*(?:AM|PM)\s+HEARING\s+IN\s+RE\b"
    r"|"
    r"see\s+also\s+alt(?:ernate)?\s*sheet"
    r")",
    re.IGNORECASE,
)

# Disposition verbs — if any of these appear, the text is a real ruling,
# not a bare calendar listing.  Limited to *past-participle* forms so that
# motion labels like "Motion to Continue" or "Petition to Approve" (which
# use infinitive/present-tense verbs) are NOT misclassified as rulings.
# Matches whole words, case-insensitive.
_RULING_VERB_RE = re.compile(
    r"\b("
    r"GRANTED|"
    r"DENIED|"
    r"SUSTAINED|"
    r"OVERRULED|"
    r"CONTINUED|"
    r"TRAILED|"
    r"MOOTED|"
    r"WITHDRAWN|"
    r"DROPPED|"
    r"SETTLED|"
    r"DISMISSED|"
    r"VACATED|"
    r"STAYED|"
    r"APPROVED|"
    r"TAKEN\s+OFF[\s\-]*CALENDAR"
    r")\b",
    re.IGNORECASE,
)

# Optional party-prefix for motion-type labels (#2489) — OC calendar cells
# sometimes attribute the motion to a party ("Plaintiff's Motion for …",
# "Defendant's Motion to …", "Cross-Complainant's Motion to …").  Accepts
# both straight and curly apostrophes (``'`` and ``\u2019``) and singular/
# plural party forms.  Used as a non-capturing optional prefix inside
# ``_MOTION_TYPE_LINE_RE``.
_PARTY_PREFIX = (
    r"(?:"
    r"Plaintiffs?|"
    r"Defendants?|"
    r"Cross[\s\-]Complainants?|"
    r"Cross[\s\-]Defendants?|"
    r"Petitioners?|"
    r"Respondents?|"
    r"Applicants?|"
    r"Movants?|"
    r"Claimants?"
    r")['\u2019]s?\s+"
)

# Lines that look like motion-type labels — used in OC calendar cells to
# show which motions are scheduled, without providing a tentative body.
# Plural forms (Motions, Demurrers, Hearings, Applications, Petitions) are
# handled via the ``s?`` suffix so a cell like "Demurrers" does not bypass
# the filter.  The trailing ``.*`` allows periods in motion-type labels
# (e.g. trailing punctuation, acronyms like "N.O.V.") — safe because
# ``_is_calendar_listing_only`` short-circuits on any disposition verb
# before reaching this regex, so real rulings are never misclassified.
# Optional ``_PARTY_PREFIX`` allows party-attributed labels like
# ``Plaintiff's Motion for Approval`` (#2489).
_MOTION_TYPE_LINE_RE = re.compile(
    r"^\s*(?:" + _PARTY_PREFIX + r")?"
    r"("
    r"Motions?\s+(?:to|for|in)\b.*|"
    r"Demurrers?(?:\s+(?:to\b|\().*)?|"
    r"Petitions?\s+(?:to|for)\b.*|"
    r"Hearings?(?:\s+on\b.*)?|"
    r"Ex\s+Parte\b.*|"
    r"Applications?\s+(?:to|for)\b.*|"
    r"Case\s+Management\s+Conference\b.*|"
    r"Status\s+Conference\b.*|"
    r"Trial\s+Setting\s+Conference\b.*|"
    r"Order\s+to\s+Show\s+Cause\b.*|"
    r"OSC\b.*"
    r")\s*$",
    re.IGNORECASE,
)

# Upper bound on length for "calendar-listing-only" classification.  Beyond
# this, even motion-type-only text is likely to contain real content we
# should not drop.  Calendar listings are generally very short — a handful
# of words per cell.
_CALENDAR_LISTING_MAX_LENGTH = 100


def _is_calendar_listing_only(text: str | None) -> bool:
    """Return True if ``text`` looks like a calendar listing, not a ruling.

    A "calendar listing" is a per-case cell from an Orange County department
    calendar PDF that contains only the motion-type heading or an
    "OFF-CALENDAR" / "NO TENTATIVE" / "O/C" / bare-continuance /
    parenthetical-disposition marker — no actual tentative ruling body.
    Examples::

        "OFF-CALENDAR"
        "O/C"
        "(Moot)"
        "Cont. To 4/20"
        "CONTINUED TO 10/6/26"
        "Tentative pending."
        "ADR Review Hearing"
        "Motion to Strike"
        "Plaintiff's Motion for Approval"
        "Demurrer\nMotion to Strike"
        "Motion for Attorneys' Fees"

    Also covers Contra Costa probate calendar-pointer rows (#3609) that are
    110-180 chars (above ``_CALENDAR_LISTING_MAX_LENGTH``) but are
    unambiguously listings because they contain explicit pointer signals::

        "9:00 AM HEARING IN RE: PETITION FOR ... --see also alternate sheet"
        "HEARING IN RE: ESTATE OF ROBERT A. HARRIS --see also alternate sheet"

    These are distinguishable from real short rulings because real rulings
    contain a disposition verb (GRANTED/DENIED/SUSTAINED/etc.) used as a
    substantive verb rather than as a bare listing marker.  The filter is
    deliberately conservative — if any disposition verb is present in a
    *non-bare* context, the text is treated as a real ruling and not
    dropped.

    The evaluation order is:

    0. **Pointer exemption** — if the text matches ``_PROBATE_CALENDAR_LISTING_RE``
       (explicit CC probate calendar-pointer signal) AND does NOT contain a
       disposition verb, accept as a listing regardless of length.  This must
       run BEFORE the length gate so that 110-180 char pointer rows are caught.
    1. Reject text longer than ``_CALENDAR_LISTING_MAX_LENGTH``.
    2. Accept bare continuance lines (``Cont. to 4/20``, ``CONTINUED TO
       10/6/26``) — these contain the ``CONTINUED`` disposition verb but
       are listings, so they must be checked *before* the disposition-verb
       short-circuit.
    3. Accept bare parenthetical dispositions (``(Moot)``, ``(Withdrawn)``)
       for the same reason as (2) — some parenthetical markers contain
       disposition verbs.
    4. Short-circuit to False on any *substantive* disposition verb in
       the remaining text.
    5. Accept O/C, OFF-CALENDAR, NO TENTATIVE, and placeholder markers.
    6. Accept text where every line is a motion-type label (possibly with
       optional party prefix).

    Returns ``False`` for ``None`` or empty text so callers preserve those
    entries (they may be metadata-only rows handled by other filters).
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    # Step 0: Explicit CC probate calendar-pointer signals bypass the length
    # gate.  Only time-prefixed HEARING IN RE and alt-sheet pointer phrases
    # match; the bare phrase is excluded to avoid false-positive drops on
    # non-CC rulings that use verbs absent from _RULING_VERB_RE (#3699).
    if _PROBATE_CALENDAR_LISTING_RE.search(stripped) and not _RULING_VERB_RE.search(stripped):
        return True
    # Too long to be a calendar listing.
    if len(stripped) > _CALENDAR_LISTING_MAX_LENGTH:
        return False
    # Bare continuance and bare parenthetical dispositions must be checked
    # BEFORE the disposition-verb short-circuit: their markers ("CONTINUED
    # TO", "(Withdrawn)", "(Vacated)") contain disposition verbs but the
    # full stripped text is still a listing rather than a real ruling.
    if _BARE_CONTINUANCE_RE.match(stripped):
        return True
    if _BARE_DISPOSITION_RE.match(stripped):
        return True
    # Any disposition verb means this is a real ruling — never drop.
    if _RULING_VERB_RE.search(stripped):
        return False
    # OFF-CALENDAR, O/C, "NO TENTATIVE", and placeholder markers are
    # calendar listings, not real rulings (OC department calendars use
    # these for cases where no tentative was posted).
    if (
        _OFF_CALENDAR_RE.search(stripped)
        or _OC_ABBREV_RE.match(stripped)
        or _NO_TENTATIVE_RE.search(stripped)
        or _PLACEHOLDER_RE.match(stripped)
    ):
        return True
    # Otherwise, the text must consist entirely of motion-type-style lines.
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return all(_MOTION_TYPE_LINE_RE.match(ln) for ln in lines)


def _drop_calendar_listing_rulings(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Filter out rulings whose ruling_text is a bare calendar listing (#2446).

    Orange County department calendar PDFs sometimes list cases with only
    the motion type heading or "OFF-CALENDAR" in place of an actual tentative
    ruling body.  Those rows are emitted by the multimodal LLM as "rulings"
    with very short, non-substantive ``ruling_text``.  Drop them entirely so
    they do not pollute search results or motion-type analytics.

    Rules:

    - Entries with ``cross_reference_source`` set are exempt — they share
      text intentionally via cross-reference resolution (#2317) and are the
      referent's responsibility.
    - Entries with ``ruling_text`` of ``None`` or empty are preserved.
      They carry metadata (case_number, case_title) but no text; dropping
      them is the job of other filters, not this one.
    - Otherwise, ``_is_calendar_listing_only(ruling_text)`` decides.
    """
    kept: list[ExtractedRuling] = []
    for ruling in rulings:
        if ruling.cross_reference_source is not None:
            kept.append(ruling)
            continue
        if not ruling.ruling_text:
            kept.append(ruling)
            continue
        if _is_calendar_listing_only(ruling.ruling_text):
            logger.warning(
                "llm_extractor.calendar_listing_dropped",
                case_number=ruling.extracted_case_number,
                case_title=ruling.extracted_case_title,
                text_preview=ruling.ruling_text[:100],
                text_length=len(ruling.ruling_text),
            )
            continue
        kept.append(ruling)
    return kept


# ---------------------------------------------------------------------------
# Post-processing: short-unsubstantive ruling filter (#2645)
# ---------------------------------------------------------------------------
#
# Some OC calendar PDFs include rows on the tentative-rulings table whose
# body cell is empty — the court lists the case on the calendar but did not
# post a tentative ruling for that row.  The multimodal LLM still emits one
# object per calendar row, filling ``ruling_text`` with whatever happened to
# be in that cell: the case caption, the motion-name header, a stray
# punctuation mark, a fragment of nearby text.  These slip through the
# pattern-based ``_drop_calendar_listing_rulings`` filter because the noise
# text doesn't match any of its known markers (OFF-CALENDAR, NO TENTATIVE,
# motion-type labels, etc.).
#
# The symptom is diagnosable by three shared characteristics of every
# observed case:
#
#   * ``len(ruling_text) < 100`` — real tentative rulings run multi-paragraph
#   * ``motion_type is None``    — LLM found no recognizable motion
#   * ``outcome is None``        — LLM found no disposition verb
#
# Any one of these three signals alone is weak (real short "GRANTED."
# rulings lack only length; a long citation artifact lacks only outcome),
# but ALL THREE together virtually guarantees the row is calendar-listing
# noise that the pattern filter missed.  "Missing > wrong" — when in doubt,
# skip the row rather than emit something bogus.

# Upper bound on ruling_text length for "short-unsubstantive" classification.
# Real tentative rulings almost always exceed this length when complete; if
# a row has <100 chars AND is missing both motion_type and outcome, the LLM
# had no substantive content to summarize.
_SHORT_UNSUBSTANTIVE_MAX_LENGTH = 100


def _is_short_unsubstantive_ruling(
    ruling: ExtractedRuling,
    *,
    length_threshold: int = _SHORT_UNSUBSTANTIVE_MAX_LENGTH,
) -> bool:
    """Return True if ``ruling`` is short and has no motion/outcome signal.

    A "short-unsubstantive" ruling is one whose body text is below the
    minimum-content threshold AND whose LLM extraction produced neither a
    ``motion_type`` nor an ``outcome``.  These three signals together
    indicate the row is almost certainly an empty-cell calendar listing
    that the pattern-based filter missed — the LLM saw a calendar row with
    no ruling body and filled ``ruling_text`` with whatever happened to be
    in that cell (case caption fragment, motion-name header, stray text).

    Additional qualifying condition: the ruling must have a case identifier
    (``extracted_case_number`` or ``extracted_case_title``).  Rows without
    either field are cross-page continuations that ``_join_page_rows``
    could not merge into a previous case (no previous page context); those
    promote to standalone ruling rows and must not be dropped even when
    they happen to be short, because their content is real body text
    belonging to a real case whose header was on a prior page.

    The filter also short-circuits to False if the text contains a
    disposition verb (GRANTED, DENIED, SUSTAINED, etc.).  Real one-liner
    rulings like ``"Motion GRANTED."`` occasionally make it through with
    null ``motion_type``/``outcome`` (LLM extraction failures are not free
    of bugs), and those are real rulings we should not drop.

    Parameters
    ----------
    ruling : ExtractedRuling
        The ruling to evaluate.
    length_threshold : int, optional
        Maximum ``ruling_text`` length for short classification.  Defaults to
        100 — chosen to match the matching OC spotcheck criterion in #2645.

    Returns
    -------
    bool
        True if ``ruling`` should be dropped as short-unsubstantive.
    """
    text = ruling.ruling_text
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) >= length_threshold:
        return False
    # A disposition verb means this is a real ruling, even if motion_type
    # and outcome happen to be None (LLM extraction isn't bug-free either).
    if _RULING_VERB_RE.search(stripped):
        return False
    # Cross-page continuations lack both case_number and case_title — those
    # rows carry real body text whose header lives on a prior page.  Never
    # drop them even when short and signal-free; that's the splitter's job
    # to recover the full context on a later pass, not this filter's.
    if not ruling.extracted_case_number and not ruling.extracted_case_title:
        return False
    # Cross-reference stubs (e.g. "Ctrl Click on Line 10 for tentative
    # ruling.") are valid calendar-pointer rows awaiting cross-ref
    # resolution (#3663).  Even when they are short and lack motion_type /
    # outcome, they must not be classified as unsubstantive noise — they
    # carry a real case_number and will be resolved (or survive as stubs) by
    # subsequent pipeline steps.
    if _XREF_LINE_RE.search(stripped):
        return False
    # The three-signal test: missing motion_type AND outcome AND short.
    return ruling.motion_type is None and ruling.outcome is None


def _drop_short_unsubstantive_rulings(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Filter out rulings below the minimum-content threshold (#2645).

    Orange County calendar PDFs sometimes list cases with empty
    tentative-ruling cells (the court listed the case but did not post a
    tentative for that row).  The multimodal LLM still emits one row per
    listed case, filling ``ruling_text`` with whatever happened to be in
    that cell — e.g. a fragment of the case caption or motion label.
    These rows slip through ``_drop_calendar_listing_rulings`` because the
    noise text doesn't match any of its known placeholder patterns.

    This filter drops rulings that are **simultaneously**:

    1. Short — ``len(ruling_text) < 100``
    2. Missing ``motion_type`` (LLM found no motion label)
    3. Missing ``outcome`` (LLM found no disposition)
    4. Do not contain any disposition verb in ``ruling_text``

    All four conditions must hold.  A disposition verb (``GRANTED``,
    ``SUSTAINED``, etc.) is a strong enough signal that this is a real
    short ruling — short-but-real rulings like ``"Motion GRANTED."`` are
    preserved even if the LLM happened to leave ``motion_type``/``outcome``
    null.

    Rules:

    - Entries with ``cross_reference_source`` set are exempt (same pattern
      as ``_drop_calendar_listing_rulings``) — the shared text is the
      referent's responsibility.
    - Entries with ``ruling_text`` of ``None`` or empty are preserved —
      they carry metadata only; dropping them is the job of other filters.

    "Missing > wrong" (CLAUDE.md §Scraper Development Rules): when in doubt
    between emitting a bogus short ruling and dropping the row, drop it.
    """
    kept: list[ExtractedRuling] = []
    for ruling in rulings:
        if ruling.cross_reference_source is not None:
            kept.append(ruling)
            continue
        if not ruling.ruling_text:
            kept.append(ruling)
            continue
        if _is_short_unsubstantive_ruling(ruling):
            logger.warning(
                "llm_extractor.short_unsubstantive_dropped",
                case_number=ruling.extracted_case_number,
                case_title=ruling.extracted_case_title,
                text_preview=ruling.ruling_text[:100],
                text_length=len(ruling.ruling_text),
            )
            continue
        kept.append(ruling)
    return kept


# ---------------------------------------------------------------------------
# Post-processing: role-literal orphan filter (#3663)
# ---------------------------------------------------------------------------
#
# SC (Santa Clara) multi-page PDFs produce two LLM rows per case:
#
#   1. **Calendar row** (pages 2–3): correct extracted_case_number,
#      correct extracted_case_title, and a short cross-reference stub like
#      "Ctrl Click on Line 10 for tentative ruling."  This row carries the
#      ``entry_number`` from the PDF's calendar index.
#
#   2. **Body-section orphan** (later pages): empty extracted_case_number,
#      a role-literal extracted_case_title ("Plaintiff v. FCA"), real
#      analysis text, and no entry_number.  The LLM emits this row because
#      the ruling body starts a fresh page without a proper case header.
#
# The calendar row is the authoritative record — it has the real case
# number and is the target of any cross-references from other calendar
# rows.  The body orphan must be dropped; keeping it causes the downstream
# pipeline to create a derived ruling with an ``UNKNOWN-<sha>`` placeholder
# case_number instead of the real one.
#
# Drop condition (all three must hold):
#   * ``_ROLE_LITERAL_TITLE_RE`` matches ``extracted_case_title``, OR
#     ``_BRACKETED_PLACEHOLDER_TITLE_RE`` matches anywhere in the title
#   * ``extracted_case_number`` is None or empty string
#   * ``entry_number`` is None
#
# The entry_number guard is critical: real calendar rows (the rows we want
# to keep) almost always carry an entry_number from the PDF index.  Requiring
# its absence prevents accidental drops of calendar rows whose court used a
# generic "Plaintiff" as the actual party pseudonym while still providing a
# calendar line number.


def _drop_role_literal_orphan_rulings(
    rulings: list[ExtractedRuling],
) -> list[ExtractedRuling]:
    """Drop body-section orphans whose title is a role literal and have no case_number (#3663).

    SC multi-page PDFs emit two LLM rows per case:

    1. A **calendar row** with the real ``extracted_case_number``,
       ``extracted_case_title``, and ``entry_number`` (a stub
       cross-reference text like "Ctrl Click on Line N …").
    2. A **body-section orphan** with empty ``extracted_case_number``,
       a role-literal title like ``"Plaintiff v. FCA"`` (matching
       ``_ROLE_LITERAL_TITLE_RE``), and no ``entry_number``.

    The orphan is an artefact of multi-page PDF parsing — the ruling body
    starts on a fresh page without a proper case header, so the LLM
    fabricates a role-literal title.  Keeping the orphan causes the
    pipeline to create a derived ruling with an ``UNKNOWN-<sha>``
    placeholder case_number.

    Rules:

    - A ruling is dropped when ALL THREE conditions hold:
      1. ``_ROLE_LITERAL_TITLE_RE`` matches ``extracted_case_title``, OR
         ``_BRACKETED_PLACEHOLDER_TITLE_RE`` matches anywhere in the title
      2. ``extracted_case_number`` is None or empty
      3. ``entry_number`` is None
    - Rulings with a valid ``extracted_case_number`` are ALWAYS preserved,
      even when the title happens to be role-literal or a bracketed
      placeholder (genuine pseudonym cases, e.g. Doe v. Smith).
    - Rulings with a valid ``entry_number`` are preserved — they are real
      calendar rows, not orphans.
    """
    kept: list[ExtractedRuling] = []
    for ruling in rulings:
        title = ruling.extracted_case_title or ""
        has_case_number = bool(ruling.extracted_case_number)
        has_entry_number = ruling.entry_number is not None
        if (
            (_ROLE_LITERAL_TITLE_RE.match(title) or _BRACKETED_PLACEHOLDER_TITLE_RE.search(title))
            and not has_case_number
            and not has_entry_number
        ):
            logger.warning(
                "llm_extractor.role_literal_orphan_dropped",
                case_title=ruling.extracted_case_title,
                text_length=len(ruling.ruling_text) if ruling.ruling_text else 0,
            )
            continue
        kept.append(ruling)
    return kept


# ---------------------------------------------------------------------------
# Post-processing: cache-hit filter re-application (#2513)
# ---------------------------------------------------------------------------
#
# The LLM cache stores post-filter rulings keyed by content hash.  Returning
# cached rulings directly means any filter changes made AFTER the cache entry
# was written are not applied — leaving stale rows in the database.  See
# #2489 for the Orange County calendar-listing filter widening that motivated
# this fix.
#
# Both helpers re-apply the subset of post-processing filters that operate
# purely on the final ``ExtractedRuling[]`` list.  They are idempotent: if
# the cached rulings already passed the current filters, re-applying them is
# a no-op.  If the filters have been updated since the cache entry was
# written, the new logic takes effect on cache read — avoiding an expensive
# cache-busting reingest.
#
# ``_propagate_document_fields`` is NOT re-applied here — it operates on the
# text-extraction ``ExtractionResult``, not ``ExtractedRuling[]``, so it is
# structurally not portable to the PDF cache-hit path.


def _apply_pdf_cache_hit_filters(
    rulings: list[ExtractedRuling],
    *,
    content_key: str,
) -> list[ExtractedRuling]:
    """Re-apply post-processing filters on the PDF cache-hit path (#2513).

    Mirrors the subset of filters in :func:`_join_page_rows` that operate
    purely on the final ``ExtractedRuling[]`` list.  Order matches
    ``_join_page_rows`` so the cache-hit output is equivalent to what a
    fresh extraction would produce.

    Cross-reference resolution (#3608) runs first so stub rulings that carry
    ``entry_number`` are resolved before the drop/dedup filters discard them,
    mirroring the filter ordering in the fresh-extract path.

    Logs ``llm_extractor.cache_hit_filters_dropped`` at info level if the
    filters dropped rows — useful for observing the effect of filter
    widening in production without re-running LLM calls.
    """
    original_count = len(rulings)
    rulings = _resolve_cross_references(rulings)
    rulings = _drop_role_literal_orphan_rulings(rulings)
    rulings = _drop_calendar_listing_rulings(rulings)
    rulings = _drop_short_unsubstantive_rulings(rulings)
    rulings = _truncate_concatenated_case_titles(rulings)
    rulings = _truncate_repeated_name_tails(rulings)
    rulings = _deduplicate_ruling_texts(rulings)
    rulings = _filter_citation_artifacts(rulings)
    if len(rulings) != original_count:
        logger.info(
            "llm_extractor.cache_hit_filters_dropped",
            content_key=content_key[:12],
            path="pdf",
            original_count=original_count,
            kept_count=len(rulings),
        )
    return rulings


def _apply_text_cache_hit_filters(
    rulings: list[ExtractedRuling],
    *,
    content_key: str,
) -> list[ExtractedRuling]:
    """Re-apply post-processing filters on the text cache-hit path (#2513).

    Applies ``_filter_citation_artifacts`` (matching the fresh text path in
    :meth:`LlmExtractor.extract`) plus ``_deduplicate_ruling_texts`` so
    the cache-hit path catches filter updates made after the cache entry
    was written.  Calendar-listing filtering is not applied here because
    the text path is used primarily for HTML-originated content where
    those patterns do not appear.

    Logs ``llm_extractor.cache_hit_filters_dropped`` at info level if the
    filters dropped rows.
    """
    original_count = len(rulings)
    rulings = _filter_citation_artifacts(rulings)
    rulings = _truncate_concatenated_case_titles(rulings)
    rulings = _truncate_repeated_name_tails(rulings)
    rulings = _deduplicate_ruling_texts(rulings)
    rulings = _sanitize_riverside_rulings(rulings, case_number_re=_RIVERSIDE_CASE_NUMBER_RE)
    rulings = _drop_riverside_no_tentative_ruling_stubs(rulings)
    rulings = _sanitize_san_bernardino_rulings(rulings, case_number_re=_SB_CASE_NUMBER_RE)
    if len(rulings) != original_count:
        logger.info(
            "llm_extractor.cache_hit_filters_dropped",
            content_key=content_key[:12],
            path="text",
            original_count=original_count,
            kept_count=len(rulings),
        )
    return rulings


# ---------------------------------------------------------------------------
# Post-processing: cross-reference resolution (#2317)
# ---------------------------------------------------------------------------

# Santa Clara PDFs use calendar line numbers.  When multiple motions share
# one ruling, the PDF lists the full ruling under one line number and uses
# cross-references for the others.  These patterns match the stub text.

# Patterns that contain an explicit line number reference.
_XREF_LINE_RE = re.compile(
    r"(?:See\s+Line\s+(\d+))"
    r"|(?:[Ss]croll\s+down\s+to\s+Lines?\s+(\d+))"
    r"|(?:Ctrl\s+Click\b.*?\bon\s+Line\s+(\d+))"
    r"|(?:Click\s+on\s+Line\s+(\d+))"
    r"|(?:\[The\s+Court\s+addressed\b.*?\bat\s+Line\s+(\d+)\s+above\])",
    re.IGNORECASE,
)

# Pattern for implicit "previous entry" references (no explicit line number).
_XREF_ORDER_ABOVE_RE = re.compile(
    r"\[Order\s+above\]",
    re.IGNORECASE,
)

# Riverside PDFs reference rulings by entry number: "See Ruling for #N Above".
_XREF_RIVERSIDE_RE = re.compile(
    r"See\s+Ruling\s+for\s+#(\d+)\s+Above",
    re.IGNORECASE,
)

# Orange County PDFs reference rulings by case number:
# "See the tentative ruling [set forth] above for <Party>, case no. XXXXXX".
_XREF_OC_CASE_NUMBER_RE = re.compile(
    r"See\s+the\s+tentative\s+ruling\s+(?:set\s+forth\s+)?above\s+for\s+[^,]+,?\s*case\s+(?:no|number)\.?\s*([A-Z0-9-]+)",
    re.IGNORECASE,
)

# Minimum ruling_text length on the *referenced* entry for it to be considered
# substantive enough to copy.  Short referenced text (< 100 chars) usually means
# the reference target is itself a stub and copying would propagate the stub.
_XREF_REF_MIN_TEXT_LENGTH = 100

# Maximum ruling_text length on the *stub* entry for us to still consider it a
# stub.  Santa Clara stubs commonly run 300-500 chars because the LLM prefixes
# the caption header (e.g. ``**Order on Defendants' Demurrer...**``) before the
# cross-reference phrase.  Texts beyond 2000 chars almost certainly contain a
# real ruling and should not be overwritten even if they happen to mention
# another line number.  See #2416.
_XREF_STUB_MAX_TEXT_LENGTH = 2000


def _build_case_number_to_index(rulings: list[ExtractedRuling]) -> dict[str, int]:
    """Build a case-number → ruling-index map that prefers the substantive entry (#4000).

    When multiple OC rulings share a case number (a stub and the substantive
    ruling it references), a plain dict comprehension with last-write-wins
    semantics may store the stub index instead of the substantive one.  The
    stub's xref then resolves to itself (``ref_idx == i``) and stays unresolved.

    This helper breaks the tie by keeping the index of the entry with the
    *longest* ``ruling_text`` for each case number.  Length is a reliable
    proxy for substantiveness: OC stubs are by construction short cross-
    reference phrases, while substantive rulings contain the full court text.
    """
    result: dict[str, int] = {}
    for i, r in enumerate(rulings):
        if not r.extracted_case_number:
            continue
        key = r.extracted_case_number
        current_text = r.ruling_text or ""
        if key not in result:
            result[key] = i
        else:
            existing_text = rulings[result[key]].ruling_text or ""
            if len(current_text) > len(existing_text):
                result[key] = i
    return result


def _resolve_cross_references(
    rulings: list[ExtractedRuling],
    entry_number_to_index: dict[int, int] | None = None,
    case_number_to_index: dict[str, int] | None = None,
) -> list[ExtractedRuling]:
    """Resolve cross-reference entries by copying ruling_text from the referenced entry (#2317).

    Santa Clara tentative ruling PDFs use calendar line numbers.  When multiple
    cases share one ruling, the PDF lists the full ruling under one line number
    and uses cross-reference text for the others (e.g., "See Line 4 for
    tentative ruling").  These entries end up with only the referral text as
    their ``ruling_text``, resulting in null outcome after enrichment.

    Also handles Riverside PDFs that reference rulings by entry number
    ("See Ruling for #N Above") and Orange County PDFs that reference rulings
    by case number ("See the tentative ruling above for <Party>, case no. XXXXXX").

    This function:

    1. Detects cross-reference patterns in ``ruling_text`` regardless of the
       stub text's length (up to ``_XREF_STUB_MAX_TEXT_LENGTH``).  Santa Clara
       stubs frequently include a caption-header prefix that pushes the stub
       text past a few hundred chars — an aggressive length gate was filtering
       those out previously (#2416).
    2. Extracts the referenced line number (or uses the previous entry for
       ``[Order above]``).
    3. Copies ``ruling_text`` from the referenced entry when (a) the referenced
       entry has substantial text (>= ``_XREF_REF_MIN_TEXT_LENGTH`` chars), and
       (b) the referenced text is longer than the current stub text so we never
       replace a longer, presumably-more-complete ruling with a shorter one.
    4. Sets ``cross_reference_source`` to the referenced entry_number.

    Because ``_join_page_rows`` is invoked on the rows from *all* pages joined
    together (see :func:`extract_from_pdf`), cross-references that span pages
    (e.g. stub on page 2, target on page 1) resolve naturally — the joined
    ``entry_number_to_index`` contains every line number from every page.

    Parameters
    ----------
    rulings:
        List of ``ExtractedRuling`` objects from ``_join_page_rows``.
    entry_number_to_index:
        Mapping from calendar line entry_number to index in ``rulings``.  When
        ``None`` (cache-hit path), the map is built from each ruling's
        ``entry_number`` field so resolution works without pre-join row state.
    case_number_to_index:
        Mapping from extracted_case_number to index in ``rulings``.  Used to
        resolve Orange County case-number cross-references.  When ``None``
        (cache-hit path or ``_join_page_rows`` for non-OC PDFs), the map is
        built from each ruling's ``extracted_case_number`` field.
    """
    # When no map is provided (cache-hit path), build it from per-ruling entry_number.
    if entry_number_to_index is None:
        entry_number_to_index = {
            r.entry_number: i for i, r in enumerate(rulings) if r.entry_number is not None
        }

    # When no case-number map is provided, build it from per-ruling extracted_case_number.
    # Use the helper so that duplicate case numbers keep the substantive (longest) entry.
    if case_number_to_index is None:
        case_number_to_index = _build_case_number_to_index(rulings)

    # Build reverse map: index -> entry_number (for "order above" lookups).
    index_to_entry: dict[int, int] = {v: k for k, v in entry_number_to_index.items()}

    for i, ruling in enumerate(rulings):
        text = ruling.ruling_text
        if not text:
            continue

        # Cap the stub length.  Texts much longer than this almost certainly
        # contain a real ruling — even if they happen to mention another line
        # number in passing.  Do not overwrite them.
        if len(text) > _XREF_STUB_MAX_TEXT_LENGTH:
            continue

        # Try explicit line number patterns.
        m = _XREF_LINE_RE.search(text)
        if m:
            # Extract the first non-None group (the line number).
            ref_line = next((int(g) for g in m.groups() if g is not None), None)
            if ref_line is not None:
                ref_idx = entry_number_to_index.get(ref_line)
                if ref_idx is not None and ref_idx != i:
                    ref_ruling = rulings[ref_idx]
                    ref_text = ref_ruling.ruling_text
                    if (
                        ref_text
                        and len(ref_text) >= _XREF_REF_MIN_TEXT_LENGTH
                        and len(ref_text) > len(text)
                    ):
                        rulings[i] = ruling.model_copy(
                            update={
                                "ruling_text": ref_text,
                                "cross_reference_source": ref_line,
                            }
                        )
                        logger.info(
                            "llm_extractor.xref_resolved",
                            case_number=ruling.extracted_case_number,
                            ref_line=ref_line,
                            stub_length=len(text),
                            ref_length=len(ref_text),
                        )
                continue

        # Try implicit "order above" pattern.
        if _XREF_ORDER_ABOVE_RE.search(text):
            # Find the immediately preceding entry in the calendar.
            prev_idx: int | None = None
            if i > 0:
                prev_idx = i - 1
            if prev_idx is not None:
                ref_ruling = rulings[prev_idx]
                ref_entry = index_to_entry.get(prev_idx)
                ref_text = ref_ruling.ruling_text
                if (
                    ref_text
                    and len(ref_text) >= _XREF_REF_MIN_TEXT_LENGTH
                    and len(ref_text) > len(text)
                ):
                    rulings[i] = ruling.model_copy(
                        update={
                            "ruling_text": ref_text,
                            "cross_reference_source": ref_entry,
                        }
                    )
                    logger.info(
                        "llm_extractor.xref_resolved_order_above",
                        case_number=ruling.extracted_case_number,
                        prev_entry=ref_entry,
                        stub_length=len(text),
                        ref_length=len(ref_text),
                    )
            continue

        # Try Riverside explicit entry-number pattern: "See Ruling for #N Above".
        m_rv = _XREF_RIVERSIDE_RE.search(text)
        if m_rv:
            ref_entry_num = int(m_rv.group(1))
            ref_idx = entry_number_to_index.get(ref_entry_num)
            if ref_idx is not None and ref_idx != i:
                ref_ruling = rulings[ref_idx]
                ref_text = ref_ruling.ruling_text
                if (
                    ref_text
                    and len(ref_text) >= _XREF_REF_MIN_TEXT_LENGTH
                    and len(ref_text) > len(text)
                ):
                    rulings[i] = ruling.model_copy(
                        update={
                            "ruling_text": ref_text,
                            "cross_reference_source": ref_entry_num,
                        }
                    )
                    logger.info(
                        "llm_extractor.xref_resolved_riverside",
                        case_number=ruling.extracted_case_number,
                        ref_entry_num=ref_entry_num,
                        stub_length=len(text),
                        ref_length=len(ref_text),
                    )
            continue

        # Try Orange County case-number pattern:
        # "See the tentative ruling [set forth] above for <Party>, case no. XXXXXX".
        m_oc = _XREF_OC_CASE_NUMBER_RE.search(text)
        if m_oc:
            ref_case_number = m_oc.group(1)
            ref_idx = case_number_to_index.get(ref_case_number)
            if ref_idx is not None and ref_idx != i:
                ref_ruling = rulings[ref_idx]
                ref_text = ref_ruling.ruling_text
                if (
                    ref_text
                    and len(ref_text) >= _XREF_REF_MIN_TEXT_LENGTH
                    and len(ref_text) > len(text)
                ):
                    rulings[i] = ruling.model_copy(
                        update={
                            "ruling_text": ref_text,
                            # Use entry_number when available; fall back to the
                            # list index so cross_reference_source is always set
                            # (the only consumer is ``is not None`` guard logic).
                            "cross_reference_source": ref_ruling.entry_number
                            if ref_ruling.entry_number is not None
                            else ref_idx,
                        }
                    )
                    logger.info(
                        "llm_extractor.xref_resolved_oc_case_number",
                        case_number=ruling.extracted_case_number,
                        ref_case_number=ref_case_number,
                        stub_length=len(text),
                        ref_length=len(ref_text),
                    )

    return rulings


# ---------------------------------------------------------------------------
# LlmExtractor
# ---------------------------------------------------------------------------


class LlmExtractor:
    """Stateless extractor that converts raw court calendar text to structured data.

    Supports both text-based extraction via ``extract()`` and multimodal
    PDF image extraction via ``extract_from_pdf()``.  The provider can be
    ``"anthropic"`` (default) or ``"google"``.

    Args:
        provider: LLM provider — ``"anthropic"`` (default) or ``"google"``.
        model: Model ID.  Defaults to Claude Haiku 4.5 for Anthropic or
            ``"gemini-2.5-flash-lite"`` for Google.
        api_key: Provider API key.  For Anthropic, if ``None`` the
            ``ANTHROPIC_API_KEY`` env var is used.  For Google, the
            ``GOOGLE_API_KEY`` env var is used.
        max_retries: Number of retries on transient API failures (429, 500, 529).
        base_delay: Initial backoff delay in seconds.
        max_delay: Maximum backoff delay in seconds.
        max_output_tokens: Maximum tokens in the model response.
        max_chars_per_chunk: Per-chunk character limit for large documents.

    Example::

        extractor = LlmExtractor()
        rulings = extractor.extract("Case No. 22SMCV01940 ...")

        # Multimodal extraction from PDF images
        extractor = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")
        rulings = extractor.extract_from_pdf(pdf_bytes)
    """

    # Default models per provider.
    _PROVIDER_DEFAULT_MODELS: dict[str, str] = {
        "anthropic": DEFAULT_HAIKU_MODEL,
        "google": "gemini-2.5-flash-lite",
    }

    def __init__(
        self,
        *,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        max_delay: float = _DEFAULT_MAX_DELAY,
        max_output_tokens: int = 4096,
        max_chars_per_chunk: int = _DEFAULT_MAX_CHARS,
        bust_cache: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model or self._PROVIDER_DEFAULT_MODELS.get(provider, DEFAULT_HAIKU_MODEL)
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_output_tokens = max_output_tokens
        self._max_chars_per_chunk = max_chars_per_chunk
        # Instance-level default for cache-bust mode (#2424).  When True,
        # all ``extract()`` / ``extract_from_pdf()`` calls on this
        # instance skip cache reads unless explicitly overridden.  Cache
        # writes still happen so subsequent runs without bust_cache
        # benefit from the fresh extraction.
        self._bust_cache = bust_cache

        # Create provider-specific client.
        if provider == "google":
            self._client = _create_google_client(api_key=api_key)
        else:
            # Default to Anthropic.
            client_kwargs: dict[str, str] = {}
            if api_key is not None:
                client_kwargs["api_key"] = api_key
            self._client = anthropic.Anthropic(**client_kwargs)

        # LLM result cache — stored in S3, served locally via CachedS3Client.
        cache_bucket = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")
        self._cache: _LlmCache | None = (
            _LlmCache(
                s3_client=self._get_cache_s3_client(),
                bucket=cache_bucket,
                provider=self._provider,
                model=self._model,
            )
            if cache_bucket
            else None
        )

    @staticmethod
    def _get_cache_s3_client() -> object:
        """Return an S3 client for the LLM cache.

        Uses make_s3_client() which returns a CachedS3Client when
        S3_CACHE_DIR is set (local dev), or plain boto3 (ECS).
        """
        from .s3_cache import make_s3_client

        return make_s3_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        system_prompt: str | None = None,
        bust_cache: bool = False,
    ) -> list[ExtractedRuling]:
        """Extract structured rulings from raw calendar page text.

        This is the main entry point.  It handles chunking for large
        documents, calls the LLM API with the extraction prompt,
        parses the JSON response into ``ExtractedRuling`` models, and
        deduplicates rulings across chunks.

        Args:
            text: Raw text content from a court calendar page (PDF or HTML,
                already converted to plain text).
            metadata: Optional dict with authoritative scraper-provided
                context.  Supported keys: ``judge_name``, ``department``,
                ``hearing_date``.
            system_prompt: Custom system prompt to use instead of the
                default ``EXTRACTION_SYSTEM_PROMPT``.  Used for
                county-specific prompts (e.g. Riverside).
            bust_cache: When ``True``, skip the cache read on this call.
                Cache writes still happen so subsequent calls benefit
                from the fresh result.  See ``self._bust_cache`` for a
                persistent instance-level default.  Used by the
                ``--bust-llm-cache`` reingest flag (#2424).

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the text is empty or the API call fails after retries.
        """
        if not text or not text.strip():
            return []

        effective_prompt = system_prompt or EXTRACTION_SYSTEM_PROMPT
        content_key = _content_hash_for_cache(text, metadata)
        effective_bust = bust_cache or self._bust_cache

        # Check cache
        if self._cache is not None and not effective_bust:
            cached = self._cache.get(effective_prompt, content_key)
            if cached is not None:
                logger.debug("llm_cache.hit", content_key=content_key[:12])
                rulings = [ExtractedRuling(**r) for r in cached]
                return _apply_text_cache_hit_filters(rulings, content_key=content_key)

        chunks = self._split_into_chunks(text)
        usage = TokenUsage()

        if len(chunks) == 1:
            results = self._extract_chunk_with_retry(
                chunks[0], metadata=metadata, usage=usage, system_prompt=system_prompt
            )
            self._log_usage(usage)
            if not results:
                return []
            merged = self._merge_results(results)
            rulings = self._propagate_document_fields(merged)
        else:
            # Multiple chunks: extract each and merge.
            logger.info(
                "llm_extractor.chunked",
                num_chunks=len(chunks),
                chunk_sizes=[len(c) for c in chunks],
            )
            all_results: list[ExtractionResult] = []
            for i, chunk in enumerate(chunks):
                results = self._extract_chunk_with_retry(
                    chunk,
                    metadata=metadata,
                    usage=usage,
                    chunk_index=i,
                    system_prompt=system_prompt,
                )
                all_results.extend(results)

            self._log_usage(usage)

            if not all_results:
                return []

            merged = self._merge_results(all_results)
            rulings = self._propagate_document_fields(merged)

        # Post-processing: remove citation artifacts produced by LLM
        # misinterpreting inline citations (Requests for Judicial Notice)
        # as separate rulings.  See #2448.
        rulings = _filter_citation_artifacts(rulings)

        # Post-processing: Riverside title and ruling_text sanitizers (#2564).
        # Strip motion-heading tails from case_title and truncate ruling_text
        # at the first foreign Riverside case number.  Both sanitizers are
        # no-ops on clean rulings and on non-Riverside documents (the
        # cross-case truncator only triggers on CVRI/CVSW/RIC/… tokens).
        rulings = _sanitize_riverside_rulings(rulings, case_number_re=_RIVERSIDE_CASE_NUMBER_RE)

        # Post-processing: Riverside "No tentative ruling" stub filter (#3715).
        # Drop per-entry stubs whose only content is a bare "No tentative ruling."
        # variant (≤ 200 chars). No-op on clean rulings and non-Riverside docs.
        rulings = _drop_riverside_no_tentative_ruling_stubs(rulings)

        # Post-processing: San Bernardino title and ruling_text sanitizers (#2565).
        # Truncate ruling_text at foreign CIVSB/CIVRS case numbers and rebuild
        # role-literal titles (e.g. "Plaintiff v. Defendant") from extracted_parties.
        # No-op on non-SB documents (scoped to CIVSB/CIVRS case numbers).
        rulings = _sanitize_san_bernardino_rulings(rulings, case_number_re=_SB_CASE_NUMBER_RE)

        # Write to cache
        if self._cache is not None and rulings:
            self._cache.put(
                effective_prompt,
                content_key,
                [r.model_dump(mode="json") for r in rulings],
            )

        return rulings

    def extract_from_pdf(
        self,
        pdf_bytes: bytes,
        *,
        metadata: dict[str, str] | None = None,
        max_pages: int = 50,
        bust_cache: bool = False,
    ) -> list[ExtractedRuling]:
        """Extract structured rulings from PDF page images (multimodal).

        Renders each PDF page to a PNG image, sends **one page per LLM
        call** with a per-page prompt, collects all table rows, and joins
        cross-page results into ``ExtractedRuling`` models using
        entry-number + case-info heuristics.

        This per-page approach (a) stays within the Flash Lite 8192 output
        token limit, (b) produces more reliable row extraction than
        multi-page calls, and (c) enables accurate cross-page continuation
        detection via the entry_number field.

        Args:
            pdf_bytes: Raw PDF file content.
            metadata: Optional dict with authoritative scraper-provided
                context.  Supported keys: ``judge_name``, ``department``,
                ``hearing_date``.
            max_pages: Maximum number of PDF pages to render.  Pages beyond
                this limit are silently skipped.
            bust_cache: When ``True``, skip the cache read on this call.
                Cache writes still happen so subsequent calls benefit
                from the fresh result.  See ``self._bust_cache`` for a
                persistent instance-level default.  Used by the
                ``--bust-llm-cache`` reingest flag (#2424).

        Returns:
            A list of ``ExtractedRuling`` instances.  Returns an empty list
            if the PDF is empty, rendering fails, or the API call fails
            after retries.
        """
        if not pdf_bytes:
            return []

        content_key = _content_hash_for_cache(pdf_bytes, metadata)
        effective_bust = bust_cache or self._bust_cache

        # Check cache
        if self._cache is not None and not effective_bust:
            cached = self._cache.get(PDF_PER_PAGE_PROMPT, content_key)
            if cached is not None:
                logger.debug("llm_cache.hit_pdf", content_key=content_key[:12])
                rulings = [ExtractedRuling(**r) for r in cached]
                return _apply_pdf_cache_hit_filters(rulings, content_key=content_key)

        page_images = _render_pdf_pages(pdf_bytes, max_pages)
        if not page_images:
            logger.warning("llm_extractor.no_pages_rendered")
            return []

        usage = TokenUsage()

        # Per-page extraction: one LLM call per page.
        all_rows: list[dict] = []
        any_page_failed = False
        for page_idx, (img_bytes, media_type) in enumerate(page_images):
            page_rows = self._extract_single_page(
                img_bytes, media_type, metadata=metadata, usage=usage, page_index=page_idx
            )
            if page_rows:
                all_rows.extend(page_rows)
            else:
                # A page returned no rows — either the page is genuinely empty
                # or the LLM call failed after retries.  Track this so we skip
                # the cache write below: caching a partial result would poison
                # subsequent reads with an incomplete ruling set (#3517).
                any_page_failed = True
                logger.warning(
                    "llm_extractor.page_partial_failure",
                    page_index=page_idx,
                    total_pages=len(page_images),
                )

        self._log_usage(usage)

        if not all_rows:
            logger.warning("llm_extractor.no_rows_extracted", page_count=len(page_images))
            return []

        # Join rows into cases and convert to ExtractedRuling objects.
        rulings = _join_page_rows(all_rows, metadata=metadata)

        # Write to cache ONLY if all pages succeeded.  If any page returned []
        # (throttling, timeout, or API failure after retries), the result is
        # partial and must NOT be cached — caching a partial result causes
        # subsequent reads to serve the incomplete ruling set permanently,
        # producing UNKNOWN-prefixed rulings for cases that do have a case
        # number on the skipped page (#3517).
        if self._cache is not None and rulings and not any_page_failed:
            self._cache.put(
                PDF_PER_PAGE_PROMPT,
                content_key,
                [r.model_dump(mode="json") for r in rulings],
            )

        return rulings

    # ------------------------------------------------------------------
    # Internal: API call with retries
    # ------------------------------------------------------------------

    def _extract_chunk(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        usage: TokenUsage,
        chunk_index: int = 0,
        system_prompt: str | None = None,
    ) -> ExtractionResult | None:
        """Call the LLM API for a single text chunk and parse the result.

        Supports both Anthropic and Google providers.  When the provider
        is ``"google"``, delegates to ``call_llm`` from
        ``ingestion.llm_providers`` which handles the Google GenAI SDK.

        Retries on transient errors (429, 500, 529) with exponential backoff.
        """
        effective_prompt = system_prompt or EXTRACTION_SYSTEM_PROMPT
        user_message = self._build_user_message(text, metadata)
        delay = self._base_delay

        # Google provider: delegate to the provider-agnostic call_llm helper
        # which handles the Google GenAI SDK and retry logic.
        if self._provider == "google":
            return self._extract_chunk_google(
                user_message,
                effective_prompt,
                usage=usage,
                chunk_index=chunk_index,
                metadata=metadata,
            )

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_output_tokens,
                    temperature=0,
                    system=effective_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )

                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens
                usage.api_calls += 1

                raw_text = response.content[0].text.strip()
                return self._parse_response(raw_text, metadata)

            except anthropic.RateLimitError:
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.rate_limit",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.rate_limit_exhausted",
                    chunk_index=chunk_index,
                )
                return None

            except anthropic.InternalServerError:
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.server_error",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.server_error_exhausted",
                    chunk_index=chunk_index,
                )
                return None

            except anthropic.APIStatusError as exc:
                # 529 is Anthropic's "overloaded" status code.
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.retryable_error",
                        status_code=exc.status_code,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.api_error",
                    status_code=exc.status_code,
                    error=str(exc),
                    chunk_index=chunk_index,
                )
                return None

            except anthropic.APIConnectionError as exc:
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.connection_error",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        chunk_index=chunk_index,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.connection_error_exhausted",
                    error=str(exc),
                    chunk_index=chunk_index,
                )
                return None

        return None  # pragma: no cover — defensive

    def _extract_chunk_google(
        self,
        user_message: str,
        system_prompt: str,
        *,
        usage: TokenUsage,
        chunk_index: int = 0,
        metadata: dict[str, str] | None = None,
    ) -> ExtractionResult | None:
        """Call the Google GenAI API for a single text chunk.

        Delegates to ``call_llm`` from ``ingestion.llm_providers`` which
        handles the Google GenAI SDK, retry logic, and timeout.
        """
        from ingestion.llm_providers import call_llm

        response = call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            provider="google",
            model=self._model,
            max_tokens=self._max_output_tokens,
            timeout=60.0,
        )

        if response is None:
            logger.warning(
                "llm_extractor.google_api_failure",
                chunk_index=chunk_index,
            )
            return None

        usage.input_tokens += response.input_tokens
        usage.output_tokens += response.output_tokens
        usage.api_calls += 1

        return self._parse_response(response.text, metadata)

    def _extract_chunk_with_retry(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
        usage: TokenUsage,
        chunk_index: int = 0,
        system_prompt: str | None = None,
        _depth: int = 0,
    ) -> list[ExtractionResult]:
        """Extract a chunk, retrying with smaller sub-chunks on parse failure.

        When the LLM response is truncated (e.g. output exceeds
        ``max_output_tokens``), ``_extract_chunk`` returns ``None``
        because the truncated JSON cannot be parsed.  This method
        catches that failure and retries by splitting the chunk in
        half at a natural boundary, extracting each sub-chunk
        independently.

        Recursion is limited to one level (``_depth <= 1``).  At depth 0,
        a failed chunk is split into two halves and each half is retried
        (depth 1).  At depth 1, failures are final — no further splitting.

        Returns a list of ``ExtractionResult`` (one per successful
        sub-chunk, or one for the original chunk if it succeeds).  The
        caller merges all results.
        """
        result = self._extract_chunk(
            text,
            metadata=metadata,
            usage=usage,
            chunk_index=chunk_index,
            system_prompt=system_prompt,
        )
        if result is not None:
            return [result]

        # Extraction failed (likely truncated JSON).  If we have room to
        # retry and the chunk is large enough to split meaningfully
        # (> 2000 chars), split it in half and try each sub-chunk.
        if _depth >= _MAX_RETRY_DEPTH or len(text) < _MIN_RETRY_CHUNK_CHARS:
            logger.warning(
                "llm_extractor.chunk_failed_no_retry",
                chunk_index=chunk_index,
                chunk_len=len(text),
                depth=_depth,
            )
            return []

        logger.info(
            "llm_extractor.retry_with_smaller_chunks",
            chunk_index=chunk_index,
            chunk_len=len(text),
            depth=_depth,
        )

        sub_chunks = self._split_chunk_in_half(text)
        sub_results: list[ExtractionResult] = []
        for i, sub_chunk in enumerate(sub_chunks):
            sub_results.extend(
                self._extract_chunk_with_retry(
                    sub_chunk,
                    metadata=metadata,
                    usage=usage,
                    chunk_index=chunk_index * 10 + i,
                    system_prompt=system_prompt,
                    _depth=_depth + 1,
                )
            )
        return sub_results

    @staticmethod
    def _split_chunk_in_half(text: str) -> list[str]:
        """Split a text chunk roughly in half at a natural boundary.

        Prefers case boundaries (``SUPERIOR COURT``, ``Case Number``),
        then page breaks, then paragraph breaks.  Falls back to the
        midpoint if no natural boundary is found.
        """
        midpoint = len(text) // 2
        search_start = midpoint - len(text) // 4
        search_end = midpoint + len(text) // 4

        # Search for natural boundaries near the midpoint.
        best_split: int | None = None
        best_distance = len(text)

        # Case boundaries (SF family law: "SUPERIOR COURT" header).
        for m in _CASE_BOUNDARY_RE.finditer(text, search_start, search_end):
            dist = abs(m.start() - midpoint)
            if dist < best_distance:
                best_distance = dist
                best_split = m.start()

        # Page breaks.
        if best_split is None:
            for m in _PAGE_BREAK_RE.finditer(text, search_start, search_end):
                dist = abs(m.start() - midpoint)
                if dist < best_distance:
                    best_distance = dist
                    best_split = m.start()

        # Paragraph breaks.
        if best_split is None:
            for m in re.finditer(r"\n\n", text[search_start:search_end]):
                pos = search_start + m.start()
                dist = abs(pos - midpoint)
                if dist < best_distance:
                    best_distance = dist
                    best_split = pos

        if best_split is None:
            best_split = midpoint

        first_half = text[:best_split]
        second_half = text[max(best_split - _CHUNK_OVERLAP, 0) :]

        return [first_half, second_half]

    def _extract_single_page(
        self,
        img_bytes: bytes,
        media_type: str,
        *,
        metadata: dict[str, str] | None = None,
        usage: TokenUsage,
        page_index: int = 0,
    ) -> list[dict]:
        """Send a single page image to the LLM and return extracted table rows.

        Uses the ``PDF_PER_PAGE_PROMPT`` to extract rows from OC-style
        three-column table PDFs.  Each row is a dict with keys
        ``entry_number``, ``case_info``, and ``ruling_text``.

        Returns an empty list if the API call fails or the page has no rows.
        """
        from ingestion.llm_providers import call_llm_with_images

        text_message = self._build_user_message_for_page(metadata)
        delay = self._base_delay

        for attempt in range(1, self._max_retries + 1):
            try:
                response = call_llm_with_images(
                    system_prompt=PDF_PER_PAGE_PROMPT,
                    text_message=text_message,
                    images=[(img_bytes, media_type)],
                    provider=self._provider,
                    model=self._model,
                    client=self._client,
                    max_retries=0,  # We handle retries ourselves.
                    max_tokens=self._max_output_tokens,
                    timeout=20.0,
                )

                if response is None:
                    if attempt < self._max_retries:
                        wait = min(delay, self._max_delay)
                        logger.warning(
                            "llm_extractor.page_api_failure",
                            attempt=attempt,
                            max_retries=self._max_retries,
                            retry_in=wait,
                            page_index=page_index,
                        )
                        time.sleep(wait)
                        delay *= 2
                        continue
                    logger.error(
                        "llm_extractor.page_api_exhausted",
                        page_index=page_index,
                    )
                    return []

                usage.input_tokens += response.input_tokens
                usage.output_tokens += response.output_tokens
                usage.api_calls += 1

                return _parse_page_rows(response.text, page_index)

            except Exception:  # noqa: BLE001
                if attempt < self._max_retries:
                    wait = min(delay, self._max_delay)
                    logger.warning(
                        "llm_extractor.page_extract_error",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_in=wait,
                        page_index=page_index,
                        exc_info=True,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                logger.error(
                    "llm_extractor.page_extract_exhausted",
                    page_index=page_index,
                    exc_info=True,
                )
                return []

        return []  # pragma: no cover — defensive

    # ------------------------------------------------------------------
    # Internal: message building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(
        text: str,
        metadata: dict[str, str] | None,
    ) -> str:
        """Build the user message for the extraction prompt."""
        parts: list[str] = []
        if metadata:
            meta_lines: list[str] = []
            if metadata.get("judge_name"):
                meta_lines.append(f"Judge name (authoritative): {metadata['judge_name']}")
            if metadata.get("department"):
                meta_lines.append(f"Department (authoritative): {metadata['department']}")
            if metadata.get("hearing_date"):
                meta_lines.append(f"Hearing date (authoritative): {metadata['hearing_date']}")
            if meta_lines:
                parts.append("Metadata from scraper:\n" + "\n".join(meta_lines))

        parts.append(f"Document:\n\n{text}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_user_message_for_page(
        metadata: dict[str, str] | None,
    ) -> str:
        """Build the text portion of the user message for per-page extraction."""
        parts: list[str] = []
        if metadata:
            meta_lines: list[str] = []
            if metadata.get("judge_name"):
                meta_lines.append(f"Judge name: {metadata['judge_name']}")
            if metadata.get("department"):
                meta_lines.append(f"Department: {metadata['department']}")
            if metadata.get("hearing_date"):
                meta_lines.append(f"Hearing date: {metadata['hearing_date']}")
            if meta_lines:
                parts.append("Context:\n" + "\n".join(meta_lines))

        parts.append(
            "Extract all tentative rulings from this page. "
            "One entry per case. Skip page headers and footers."
        )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Internal: response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(
        raw_text: str,
        metadata: dict[str, str] | None,
    ) -> ExtractionResult | None:
        """Parse a JSON response from the model into an ``ExtractionResult``.

        Handles markdown code fences, validates enum values, normalizes
        case numbers, and applies authoritative metadata overrides.
        """
        try:
            parsed = parse_llm_json(raw_text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "llm_extractor.json_parse_error",
                error=str(exc),
                raw_preview=raw_text[:200],
            )
            return None

        # Top-level fields — support both extracted_ prefix and legacy names.
        judge_name = parsed.get("extracted_judge_name") or parsed.get("judge_name")
        hearing_date = parsed.get("hearing_date")
        department = parsed.get("department")

        # Apply metadata overrides.
        if metadata:
            if metadata.get("judge_name"):
                judge_name = metadata["judge_name"]
            if metadata.get("department"):
                department = metadata["department"]
            if metadata.get("hearing_date"):
                hearing_date = metadata["hearing_date"]

        # Parse rulings array.
        raw_rulings = parsed.get("rulings", [])
        if not isinstance(raw_rulings, list):
            raw_rulings = []

        rulings: list[ExtractedRuling] = []
        for r in raw_rulings:
            if not isinstance(r, dict):
                continue
            rulings.append(_parse_single_ruling(r))

        return ExtractionResult(
            extracted_judge_name=judge_name,
            hearing_date=hearing_date,
            department=str(department) if department else None,
            rulings=rulings,
        )

    # ------------------------------------------------------------------
    # Internal: chunking
    # ------------------------------------------------------------------

    def _split_into_chunks(self, text: str) -> list[str]:
        """Split text into chunks of at most ``_max_chars_per_chunk`` characters.

        Uses natural boundaries (page breaks, case-number headers) when
        available; falls back to double-newline paragraph breaks.
        """
        max_chars = self._max_chars_per_chunk
        if len(text) <= max_chars:
            return [text]

        # Try page breaks first, then case boundaries, then paragraphs.
        boundaries = [m.start() for m in _PAGE_BREAK_RE.finditer(text)]
        if not boundaries:
            boundaries = [m.start() for m in _CASE_BOUNDARY_RE.finditer(text)]
        if not boundaries:
            boundaries = [m.start() for m in re.finditer(r"\n\n", text)]
        if not boundaries:
            return _force_split(text, max_chars)

        chunks: list[str] = []
        chunk_start = 0
        last_boundary = 0

        for boundary in boundaries:
            span = boundary - chunk_start
            if span > max_chars:
                split_at = last_boundary if last_boundary > chunk_start else boundary
                chunks.append(text[chunk_start:split_at])
                chunk_start = max(split_at - _CHUNK_OVERLAP, 0)
                if len(chunks) >= _MAX_CHUNKS:
                    break
            last_boundary = boundary

        if len(chunks) < _MAX_CHUNKS and chunk_start < len(text):
            remaining = text[chunk_start:]
            if len(remaining) > max_chars:
                for sc in _force_split(remaining, max_chars):
                    if len(chunks) >= _MAX_CHUNKS:
                        break
                    chunks.append(sc)
            else:
                chunks.append(remaining)

        if not chunks:
            chunks = [text[:max_chars]]

        return chunks

    # ------------------------------------------------------------------
    # Internal: merge multi-chunk results
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _propagate_document_fields(result: ExtractionResult) -> list[ExtractedRuling]:
        """Propagate document-level judge/department to rulings missing them.

        The LLM extracts judge_name and department at the document level
        (from the PDF header). Per-ruling fields are often null because the
        judge isn't repeated in each ruling's text. This fills in the gaps.
        """
        doc_judge = result.extracted_judge_name
        doc_dept = result.department
        for ruling in result.rulings:
            if not ruling.extracted_judge_name and doc_judge:
                ruling.extracted_judge_name = doc_judge
            if not ruling.department and doc_dept:
                ruling.department = doc_dept
        return result.rulings

    @staticmethod
    def _merge_results(results: list[ExtractionResult]) -> ExtractionResult:
        """Merge extraction results from multiple chunks.

        Document-level fields come from the first chunk (headers are at the
        top).  Rulings are deduplicated by ``extracted_case_number``.
        """
        if not results:
            return ExtractionResult()

        first = results[0]
        seen: set[str] = set()
        unique_rulings: list[ExtractedRuling] = []

        for result in results:
            for ruling in result.rulings:
                key = ruling.extracted_case_number
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                unique_rulings.append(ruling)

        return ExtractionResult(
            extracted_judge_name=first.extracted_judge_name,
            hearing_date=first.hearing_date,
            department=first.department,
            rulings=unique_rulings,
        )

    # ------------------------------------------------------------------
    # Internal: logging
    # ------------------------------------------------------------------

    @staticmethod
    def _log_usage(usage: TokenUsage) -> None:
        """Log accumulated token usage for cost monitoring."""
        logger.info(
            "llm_extractor.token_usage",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            api_calls=usage.api_calls,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _normalize_case_number(raw: str) -> str:
    """Strip county prefix from case numbers (e.g. OC format)."""
    m = _COUNTY_PREFIX_RE.match(raw.strip())
    return m.group(1) if m else raw.strip()


def _parse_single_ruling(r: dict) -> ExtractedRuling:  # noqa: ANN001
    """Parse a single ruling dict from the LLM JSON response into an ``ExtractedRuling``."""
    # Case number — support both old and new field names.
    case_number = r.get("extracted_case_number") or r.get("case_number")
    if case_number:
        case_number = _normalize_case_number(str(case_number))

    # Case title.
    case_title = r.get("extracted_case_title") or r.get("case_title")

    # Case type — validate against enum.
    case_type: ExtractionCaseType | None = None
    raw_case_type = r.get("case_type")
    if raw_case_type:
        try:
            case_type = ExtractionCaseType(raw_case_type)
        except ValueError:
            case_type = ExtractionCaseType.OTHER

    # Outcome — validate against enum.
    outcome: ExtractionOutcome | None = None
    raw_outcome = r.get("outcome")
    if raw_outcome:
        try:
            outcome = ExtractionOutcome(raw_outcome)
        except ValueError:
            outcome = ExtractionOutcome.OTHER

    # Judge name.
    judge_name = r.get("extracted_judge_name") or r.get("judge_name")

    # Parties.
    raw_parties = r.get("extracted_parties") or r.get("parties", [])
    parties: list[ExtractedParty] = []
    if isinstance(raw_parties, list):
        for p in raw_parties:
            if isinstance(p, dict) and p.get("name") and p.get("role"):
                name = str(p["name"]).strip()
                # Validate party name is plausible.
                if len(name) > 200 or "\n" in name or "\r" in name:
                    logger.warning(
                        "llm_extractor.invalid_party_name",
                        length=len(name),
                        preview=name[:80],
                    )
                    continue
                raw_conf = p.get("confidence", "high")
                try:
                    conf = ConfidenceLevel(raw_conf)
                except ValueError:
                    conf = ConfidenceLevel.HIGH
                parties.append(
                    ExtractedParty(
                        name=name,
                        role=str(p["role"]),
                        confidence=conf,
                    )
                )

    # Confidence scores.
    raw_confidence = r.get("confidence", {})
    confidence = _parse_confidence(raw_confidence)

    return ExtractedRuling(
        extracted_case_number=case_number,
        extracted_case_title=case_title,
        extracted_parties=parties,
        extracted_judge_name=judge_name,
        department=r.get("department"),
        motion_type=r.get("motion_type"),
        outcome=outcome,
        ruling_text=r.get("ruling_text"),
        hearing_date=r.get("hearing_date"),
        case_type=case_type,
        confidence=confidence,
    )


_VALID_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})


def _parse_confidence(raw: dict | None) -> FieldConfidence:  # noqa: ANN001
    """Parse a confidence dict from the LLM response."""
    if not raw or not isinstance(raw, dict):
        return FieldConfidence()

    def _level(key: str) -> ConfidenceLevel:
        val = raw.get(key, "high")
        if val in _VALID_CONFIDENCE_LEVELS:
            return ConfidenceLevel(val)
        return ConfidenceLevel.HIGH

    return FieldConfidence(
        case_number=_level("case_number"),
        case_title=_level("case_title"),
        parties=_level("parties"),
        judge=_level("judge"),
        ruling_text=_level("ruling_text"),
        outcome=_level("outcome"),
    )


def _force_split(text: str, max_chars: int) -> list[str]:
    """Split text into fixed-size chunks with overlap."""
    chunks: list[str] = []
    pos = 0
    while pos < len(text) and len(chunks) < _MAX_CHUNKS:
        end = min(pos + max_chars, len(text))
        chunks.append(text[pos:end])
        pos = end - _CHUNK_OVERLAP if end < len(text) else end
    return chunks


# ---------------------------------------------------------------------------
# Per-page row parsing and join logic (#1590)
# ---------------------------------------------------------------------------


def _parse_page_rows(raw_text: str, page_index: int) -> list[dict]:
    """Parse LLM response for a single page into a list of row dicts.

    Each row has ``entry_number`` (int or None), ``case_info`` (str),
    and ``ruling_text`` (str).  Supports both the legacy 3-field format
    (entry_number/case_info/ruling_text) and the newer 4-field format
    (entry_number/case_number/case_title/ruling_text).

    When the newer format is detected (``case_number`` or ``case_title``
    present), these are combined into ``case_info`` for downstream
    compatibility with ``_join_page_rows``.

    Also extracts ``page_header`` metadata if present.
    """
    cleaned = strip_llm_json_fences(raw_text)

    # ``strict=False`` tolerates unescaped control characters inside JSON
    # string values (see #2518 and :func:`parse_llm_json`).
    try:
        parsed = json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        # Try to find a JSON object or array in the response.
        obj_start = cleaned.find("{")
        arr_start = cleaned.find("[")
        if obj_start >= 0 and (arr_start < 0 or obj_start < arr_start):
            obj_end = cleaned.rfind("}") + 1
            if obj_end > obj_start:
                try:
                    parsed = json.loads(cleaned[obj_start:obj_end], strict=False)
                except json.JSONDecodeError:
                    parsed = None
            else:
                parsed = None
        elif arr_start >= 0:
            arr_end = cleaned.rfind("]") + 1
            if arr_end > arr_start:
                try:
                    parsed = json.loads(cleaned[arr_start:arr_end], strict=False)
                except json.JSONDecodeError:
                    parsed = None
            else:
                parsed = None
        else:
            parsed = None

        if parsed is None:
            logger.warning(
                "llm_extractor.page_parse_error",
                page_index=page_index,
                raw_preview=raw_text[:200],
            )
            return []

    # Handle both list and dict responses.
    page_header: dict | None = None
    if isinstance(parsed, dict):
        # Extract page_header if present.
        page_header = parsed.get("page_header")
        # Get the rulings array.
        rows_raw = parsed.get("rulings", parsed.get("rows", parsed.get("entries", [parsed])))
    elif isinstance(parsed, list):
        rows_raw = parsed
    else:
        return []

    rows: list[dict] = []

    # If page_header contains metadata, emit a synthetic header row so
    # _join_page_rows can extract judge/department/hearing_date.
    if page_header and isinstance(page_header, dict):
        header_parts: list[str] = []
        if page_header.get("department"):
            header_parts.append(f"Department {page_header['department']}")
        if page_header.get("judge_name"):
            header_parts.append(f"JUDGE {page_header['judge_name']}")
        if page_header.get("hearing_date"):
            header_parts.append(f"Hearing Date: {page_header['hearing_date']}")
        if header_parts:
            rows.append(
                {
                    "entry_number": None,
                    "case_info": "\n".join(header_parts),
                    "ruling_text": "",
                }
            )

    for item in rows_raw:
        if not isinstance(item, dict):
            continue
        entry_number = item.get("entry_number")
        if entry_number is not None:
            entry_str = str(entry_number).rstrip(".").strip()
            # Strip parentheses from Fresno-style "(47)" numbers.
            entry_str = entry_str.strip("()")
            try:
                entry_number = int(entry_str)
            except (ValueError, TypeError):
                # Extract numeric portion from "Line 2", "Item 3", etc.
                num_match = re.search(r"\d+", entry_str)
                entry_number = int(num_match.group()) if num_match else None

        # Support both legacy (case_info) and new (case_number + case_title)
        # formats.  Combine new-format fields into case_info for downstream
        # compatibility with _join_page_rows.
        if "case_number" in item or "case_title" in item:
            case_number = str(item.get("case_number") or "").strip()
            case_title = str(item.get("case_title") or "").strip()
            # Sanitize bogus case_number values (#2577).
            # The multimodal LLM sometimes copies the case title into the
            # case_number field when the PDF has no actual case numbers —
            # e.g. returning ``case_number="Ayala v. Castillo"`` on an OC
            # N17/C10 calendar PDF where no case numbers are printed.  If
            # that fused value is passed through unchanged, downstream
            # ``_split_fused_case_info`` sees a duplicated
            # ``"Ayala v. Castillo\nAyala v. Castillo"`` case_info, detects
            # two "v." clauses, splits on the repeating-plaintiff tier, and
            # produces TWO identical rulings (one with the real text, one
            # with ``ruling_text=None``).  Reject values that don't look
            # like a case number: if case_number matches the "v." caption
            # pattern OR is byte-identical to case_title, discard it.
            if case_number:
                looks_like_case_number = bool(_CASE_NUMBER_RE.search(case_number))
                looks_like_caption = bool(_VS_RE.search(case_number))
                duplicates_title = case_title and case_number == case_title
                if not looks_like_case_number and (looks_like_caption or duplicates_title):
                    case_number = ""
            # Post-process: strip trailing artifacts from case_title.
            # The LLM sometimes appends county prefix letters, motion
            # descriptions, or cause of action text to the case title.
            # Strip trailing single-letter county prefixes (C, N, etc.)
            case_title = re.sub(r"\s+[A-Z]$", "", case_title)
            # Strip anything after common artifact patterns.
            # These are specific enough to avoid truncating legitimate
            # party names like "Johnson C Smith".
            for pattern in [
                r"\s+[A-Z]\s+(?:HEARING|PETITION|FURTHER|MOTION)",
                r"\s+[A-Z]\s+(?:Third|Fourth|Fifth|Sixth|Seventh|First|Second)",
                r"\s*\*HEARING",
                r"\s+PETITION OF:",
            ]:
                case_title = re.sub(pattern, "", case_title)
            parts = [p for p in [case_title, case_number] if p]
            case_info = "\n".join(parts)
        else:
            case_info = str(item.get("case_info", "")).strip()

        rows.append(
            {
                "entry_number": entry_number,
                "case_info": case_info,
                "ruling_text": str(item.get("ruling_text", "")).strip(),
            }
        )

    return rows


def _is_new_case(row: dict) -> bool:
    """Determine if a row starts a new case based on entry_number and case_info.

    A new case is detected when:
    1. ``entry_number`` is a valid integer, AND
    2. ``case_info`` contains a case number pattern or "vs"/"v."
    """
    if row["entry_number"] is None:
        return False

    case_info = row["case_info"]
    if not case_info:
        return False

    # Check for case number pattern.
    if _CASE_NUMBER_RE.search(case_info):
        return True

    # Check for "vs" / "v." pattern.
    if _VS_RE.search(case_info):
        return True

    return False


def _extract_case_number_from_info(case_info: str) -> str | None:
    """Extract a case number from the case_info string."""
    m = _CASE_NUMBER_RE.search(case_info)
    if m:
        raw = m.group(0)
        return _normalize_case_number(raw)
    return None


def _extract_case_title_from_info(case_info: str) -> str | None:
    """Extract a case title from the case_info string.

    Looks for patterns like "Smith v. Jones" or "Smith vs Jones" after
    the case number.  Cleans up common artifacts from multimodal
    extraction: embedded newlines, case number fragments, and court
    name fragments.
    """
    # 1. Replace newlines with spaces so multi-line case_info is joined.
    cleaned = case_info.replace("\n", " ")
    # 2. Remove complete case numbers.
    cleaned = _CASE_NUMBER_RE.sub("", cleaned)
    # 3. Remove case number fragments (partial prefixes, type codes, suffixes).
    cleaned = _CASE_NUMBER_FRAGMENT_RE.sub("", cleaned)
    # 4. Remove court / county name fragments.
    cleaned = _COURT_NAME_RE.sub("", cleaned)
    # 5. Remove trailing case-number county prefix letters (e.g. "C" from
    #    "C22-01971" where the numeric part was already stripped in step 2).
    #    Also strip trailing artifacts like "C Fraud" or "C PETITION OF:".
    cleaned = re.sub(r"\s+[CN]\s*$", "", cleaned)
    cleaned = re.sub(
        r"\s+[CN]\s+(?:HEARING|PETITION|MOTION|FURTHER"
        r"|Third|Fourth|Fifth|Sixth|Seventh|First|Second|Fraud).*$",
        "",
        cleaned,
    )
    # 6. Remove other non-title artifacts.
    cleaned = re.sub(r"\s*\*?HEARING ON\b.*$", "", cleaned)
    cleaned = re.sub(r"\s+PETITION OF:.*$", "", cleaned)
    # 7. Collapse multiple whitespace to single space.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # 8. Remove "None" artifacts from null fields being stringified.
    cleaned = re.sub(r"\bNone\b", "", cleaned)
    # 9. Collapse multiple whitespace to single space (again after removals).
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # 10. Remove leading/trailing punctuation and whitespace.
    cleaned = cleaned.strip(" -;,\t")
    # 10b. Dual-"vs." contamination guard (#2369).  When adjacent cases on
    #     a multimodal PDF page bleed into each other, the LLM sometimes
    #     returns a case_info containing TWO "vs."/"v." clauses with the
    #     first plaintiff's name repeating at the start of the second
    #     case, e.g. "Anh-Vu Nguyen vs. Freedom Medical Group, LLC
    #     Anh-Vu Nguyen vs. Seed4Planet, LLC".  When that pattern is
    #     detected, keep only the first plaintiff-vs-defendant pair by
    #     truncating at the whitespace immediately before the repeated
    #     plaintiff name.  Cases where the plaintiff does NOT repeat
    #     (genuinely unusual titles with two "vs." clauses) are left
    #     untouched to avoid mistruncation.
    vs_iter = list(_VS_RE.finditer(cleaned))
    if len(vs_iter) >= 2:
        first_vs_start = vs_iter[0].start()
        first_vs_end = vs_iter[0].end()
        second_vs_start = vs_iter[1].start()
        # First plaintiff text — everything before the first "vs.".
        first_plaintiff = cleaned[:first_vs_start].strip(" ,;-")
        # First word of the first plaintiff.  The heuristic: if this word
        # reappears between the end of the first "vs." clause and the
        # start of the second "vs.", we're seeing the repeated plaintiff
        # contamination pattern.
        first_word = first_plaintiff.split(" ", 1)[0] if first_plaintiff else ""
        if first_word:
            needle = " " + first_word
            candidate = cleaned.find(needle, first_vs_end, second_vs_start)
            if candidate > first_vs_end:
                cleaned = cleaned[:candidate].rstrip(" ,;-")
    # 11. Truncate overly long titles.  Titles over 150 chars typically
    #     contain multiple case names or full court caption text jammed
    #     together.
    if len(cleaned) > 150:
        # For "v./vs." titles, try to truncate after the first party clause.
        vs_match = _VS_RE.search(cleaned)
        if vs_match:
            after_vs = cleaned[vs_match.end() :]
            term_match = re.search(
                r"\b(?:[A-Z]{2,6}\d{5,}|\d{2,4}[A-Z]{1,4}\d{5,}"
                r"|\d{2,4}-\d{5,}|[A-Z]{1,4}-\d{2,4}-\d{5,})\b"
                r"|" + _VS_RE.pattern,
                after_vs,
                re.IGNORECASE,
            )
            if term_match:
                cleaned = cleaned[: vs_match.end() + term_match.start()].strip(" ,;")
        # For probate/estate/conservatorship titles (no "v."), truncate
        # at the start of the next case pattern.
        if len(cleaned) > 150:
            next_case = re.search(
                r"\s(?:CONSERVATORSHIP|ESTATE|GUARDIANSHIP|IN THE MATTER)"
                r"\s+OF[:.]",
                cleaned[30:],  # skip past the first one
            )
            if next_case:
                cleaned = cleaned[: 30 + next_case.start()].strip(" ,;")
        # Hard-truncate at a word boundary if still too long.
        if len(cleaned) > 150:
            space_idx = cleaned.rfind(" ", 0, 150)
            if space_idx > 50:
                cleaned = cleaned[:space_idx].rstrip(" ,;")
    if cleaned:
        return cleaned
    return None


def _split_fused_case_info(case_info: str | None) -> list[str]:
    """Split a possibly-fused ``case_info`` into one or more sub-case strings.

    When the multimodal LLM extracts a single PDF page that visually groups
    several cases together, it sometimes concatenates two adjacent cases
    into one row's ``case_info`` (#2500).  The classic pattern is:

        "Gu v. Family Orthodontics & Oral Surgery "
        "Clarke, Inc. v. Ellis & Son Trucking, Inc."

    There are two "v."/"vs." clauses, the first plaintiff's first word
    ("Gu") does NOT repeat, and there is no explicit separator — so the
    existing repeating-plaintiff guard in ``_extract_case_title_from_info``
    fails to detect it and the whole blob is collapsed into one
    ``ExtractedRuling`` (dropping the second case entirely).

    This helper detects fusion and splits the ``case_info`` into a list of
    sub-cases by looking for a boundary signal BETWEEN the first and
    second "v." clauses, in this order of precedence:

    1. **Case number** — a case-number pattern (e.g. "24-12345") between
       the two "v." clauses almost certainly marks the boundary.  The
       case number belongs to the SECOND sub-case.
    2. **Entry number** — a bare standalone integer (e.g. "2") between
       the clauses is treated as a numbering boundary (the PDF row is
       "Entry 1 ... Entry 2 ...").  The integer is discarded.
    3. **Entity suffix** — a corporate-entity suffix (``Inc.``, ``LLC``,
       ``Corp.`` etc.) between the clauses indicates the first defendant
       ended; split immediately after the suffix and any trailing
       punctuation/whitespace.
    4. **Repeating plaintiff** — if the first plaintiff's first word
       reappears before the second "v.", split at that repeat.  (This
       mirrors the existing dual-"vs." guard in
       ``_extract_case_title_from_info`` but returns two sub-cases
       instead of silently dropping the second.)
    5. **No split** — if none of the above matches, return
       ``[case_info]`` unchanged.  Speculative splitting risks fragmenting
       legitimate single-case titles (e.g. titles with embedded phrases
       resembling "v.").

    Triple+ fusion is handled by recursing on the tail sub-case.

    Args:
        case_info: The raw ``case_info`` string from ``_parse_page_rows``.
            May be ``None`` or empty.

    Returns:
        A list of sub-case strings.  Always non-empty for non-empty
        input; returns ``[case_info]`` when no fusion is detected.  For
        ``None`` / empty input returns ``[case_info]`` to preserve the
        caller's expected shape (callers should handle empty strings
        themselves).
    """
    if not case_info:
        return [case_info or ""]

    # Normalize: replace newlines with spaces so multi-line case_info
    # joins cleanly when we search for boundaries.  We keep a reference
    # back to the original case_info so sub-strings preserve punctuation
    # and whitespace that matter downstream (e.g. _extract_case_number).
    flat = case_info.replace("\n", " ")

    vs_iter = list(_VS_RE.finditer(flat))
    if len(vs_iter) < 2:
        return [case_info]

    first_vs_end = vs_iter[0].end()
    second_vs_start = vs_iter[1].start()

    # Search between the END of the first "v." clause and the START of
    # the second "v." clause for a boundary signal.
    between = flat[first_vs_end:second_vs_start]
    between_offset = first_vs_end  # absolute offset for slicing back

    split_abs_start: int | None = None  # slice [:split_abs_start] = sub1
    split_abs_end: int | None = None  # slice [split_abs_end:] = sub2

    # 1. Case number boundary — case number belongs to the SECOND sub-case.
    case_num_match = _CASE_NUMBER_RE.search(between)
    if case_num_match:
        split_abs_start = between_offset + case_num_match.start()
        split_abs_end = between_offset + case_num_match.start()
    else:
        # 2. Entry number boundary — a standalone bare integer that is
        #    NOT part of a case number (we already ruled those out above).
        #    We look for a short digit run (1-3 digits) that is
        #    surrounded by non-word characters (whitespace OR
        #    punctuation like ``.`` ``,`` ``;``).  ``\b`` is a word
        #    boundary so it matches ``"2 "``, ``"2."``, and ``"2,"``
        #    but not a digit embedded inside a word (``"word2go"``).
        #    Longer digit runs are suspicious and would have matched
        #    ``_CASE_NUMBER_RE`` above if valid.
        entry_match = re.search(r"(?<=\s)\d{1,3}\b", between)
        if not entry_match:
            # The entry number may sit at the very start of `between`
            # (no preceding whitespace inside the slice because
            # ``_VS_RE`` already consumed the separating whitespace).
            # Example: ``"Gu v. 1 Clarke, Inc. v. Ellis..."`` yields
            # ``between == "1 Clarke, Inc. "``, where the leading ``1``
            # has no space before it in the slice but is still a valid
            # entry-number boundary.
            start_match = re.match(r"\d{1,3}\b", between)
            if start_match:
                entry_match = start_match
        if entry_match:
            # First sub-case ends just before the entry number; second
            # sub-case starts just after it.
            split_abs_start = between_offset + entry_match.start()
            split_abs_end = between_offset + entry_match.end()
        else:
            # 3. Entity suffix boundary — find the FIRST entity suffix
            #    in the between-slice and split just after it.  The
            #    first suffix marks the END of the first defendant's
            #    name (which starts immediately after the first
            #    "v. ").  Using the LAST suffix instead would
            #    over-consume when the second plaintiff's name also
            #    contains an entity suffix — e.g. on input
            #    ``"Alpha v. Beta Inc. Gamma LLC v. Delta"`` the
            #    last-suffix variant would split at ``LLC`` and
            #    leave sub2 = ``"v. Delta"`` with no plaintiff.
            suffix_matches = list(_ENTITY_SUFFIX_RE.finditer(between))
            if suffix_matches:
                first_suffix = suffix_matches[0]
                suffix_end = first_suffix.end()
                # Also consume any trailing comma/semicolon/period
                # and whitespace so the second sub-case starts clean.
                tail = between[suffix_end:]
                trim = re.match(r"[\s,;.\-]*", tail)
                extra = trim.end() if trim else 0
                split_abs_start = between_offset + suffix_end
                split_abs_end = between_offset + suffix_end + extra
            else:
                # 4. Repeating-plaintiff boundary — mirrors the existing
                #    dual-"vs." guard, but yields a SPLIT instead of a
                #    truncation.  Compute the first plaintiff (everything
                #    before the first "v.").
                first_plaintiff = flat[: vs_iter[0].start()].strip(" ,;-")
                first_word = first_plaintiff.split(" ", 1)[0] if first_plaintiff else ""
                # Require at least 2 chars to avoid false positives on
                # single-letter initials (which routinely recur in case
                # titles).
                if first_word and len(first_word) >= 2:
                    # Look for the first occurrence of the first word as
                    # a standalone token in the between-slice.
                    repeat = re.search(
                        r"\b" + re.escape(first_word) + r"\b",
                        between,
                    )
                    if repeat:
                        split_abs_start = between_offset + repeat.start()
                        split_abs_end = between_offset + repeat.start()

    if split_abs_start is None or split_abs_end is None:
        # 5. No boundary signal found — do not speculate.
        return [case_info]

    # Slice back into the ORIGINAL case_info (with newlines preserved).
    # The offsets were computed against ``flat`` where each "\n" was
    # replaced by a single " "; offsets into ``flat`` map 1:1 to the
    # original because lengths are preserved.  Verify the invariant.
    assert len(flat) == len(case_info)

    sub1 = case_info[:split_abs_start].strip(" \n\t,;-")
    sub2_raw = case_info[split_abs_end:].strip(" \n\t,;-")

    # If either side is empty after trimming, the boundary was degenerate;
    # fall back to no split to avoid losing content.
    if not sub1 or not sub2_raw:
        return [case_info]

    # If sub1 and sub2 are byte-identical (or differ only in whitespace),
    # the "fusion" is actually a duplicated line — the LLM returned the
    # same caption twice in case_info.  Splitting would produce two
    # identical rulings where the second silently loses its ruling_text
    # in ``_join_page_rows``.  Return the single de-duplicated sub-case
    # instead of a phantom split (#2577).
    norm1 = re.sub(r"\s+", " ", sub1)
    norm2 = re.sub(r"\s+", " ", sub2_raw)
    if norm1 == norm2:
        return [sub1]

    # Recurse on the tail to handle triple+ fusion.
    tail_parts = _split_fused_case_info(sub2_raw)
    return [sub1, *tail_parts]


def _append_ruling_from_case(
    rulings: list[ExtractedRuling],
    case_info: str,
    ruling_text: str | None,
    *,
    metadata: dict[str, str] | None,
    header_judge: str | None,
    header_dept: str | None,
    header_date: str | None,
    header_case_number: str | None = None,
    single_ruling_doc: bool = False,
    entry_number: int | None = None,
) -> None:
    """Append one ``ExtractedRuling`` built from a single case_info string.

    Applies metadata / header fallbacks and the calendar-header guard.
    Extracted from the main conversion loop in ``_join_page_rows`` so the
    fused-row splitter (#2500) can emit one ruling per sub-case while
    reusing the same post-processing.

    When ``_extract_case_number_from_info`` returns None AND
    ``single_ruling_doc=True``, ``header_case_number`` is used as a fallback
    to avoid UNKNOWN-synthetic case numbers on OC pages where the case number
    appears in the document header rather than inline in case_info (#3729).
    The ``single_ruling_doc`` gate prevents mis-attributing one
    document-header case_number to multiple cases on the same page.
    """
    case_number = _extract_case_number_from_info(case_info)
    if case_number is None and single_ruling_doc and header_case_number:
        case_number = header_case_number
    case_title = _extract_case_title_from_info(case_info)
    text = ruling_text.strip() if ruling_text else None
    text = text or None

    # Post-processing: filter calendar header text (#2096).
    if _is_calendar_header(text):
        logger.warning(
            "llm_extractor.calendar_header_filtered",
            case_number=case_number,
            text_preview=(text or "")[:100],
        )
        text = None

    # Metadata precedence: per-row scraper/DB values (metadata kwarg) win over
    # header-extracted values (header_judge/dept/date), which are only used as a
    # fallback.  Both layers default to None when no value is available.
    judge_name: str | None = None
    department: str | None = None
    hearing_date: str | None = None
    if metadata:
        judge_name = metadata.get("judge_name")
        department = metadata.get("department")
        hearing_date = metadata.get("hearing_date")
    if not judge_name:
        judge_name = header_judge
    if not department:
        department = header_dept
    if not hearing_date:
        hearing_date = header_date

    rulings.append(
        ExtractedRuling(
            extracted_case_number=case_number,
            extracted_case_title=case_title,
            extracted_judge_name=judge_name,
            department=department,
            hearing_date=hearing_date,
            ruling_text=text,
            entry_number=entry_number,
        )
    )


def _parse_header_date(info: str) -> str | None:
    """Extract a hearing date from a page-header string, returning ISO YYYY-MM-DD.

    Tries three patterns in priority order so that the existing ISO path
    wins over ambiguous slash or month-name matches (#3559):

    1. ``Hearing Date: YYYY-MM-DD`` — explicit ISO label (existing behaviour).
    2. Month-name: ``March 16, 2026`` — common in OC multimodal PDFs.
    3. Slash: ``3/16/2026`` or ``03/16/2026`` — alternate courts.
    """
    # 1. Existing ISO path — must remain the highest-priority match.
    m = re.search(r"Hearing Date:\s*(\d{4}-\d{2}-\d{2})", info)
    if m:
        return m.group(1)

    # 2. Month-name format, e.g. "March 16, 2026".
    m = re.search(
        r"\b((?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s*\d{4})\b",
        info,
    )
    if m:
        try:
            return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 3. Slash format, e.g. "3/16/2026" or "03/16/2026".
    m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", info)
    if m:
        try:
            return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def _join_page_rows(
    rows: list[dict],
    *,
    metadata: dict[str, str] | None = None,
) -> list[ExtractedRuling]:
    """Join per-page rows into ``ExtractedRuling`` objects.

    Uses the entry_number + case_info heuristic to detect case boundaries.
    Rows without a valid entry_number or without a case identifier in
    case_info are treated as continuations of the previous case.
    """
    if not rows:
        return []

    # Group rows into cases.
    cases: list[dict] = []  # Each: {case_info, ruling_text}

    # Extract judge/department/hearing_date/case_number from header rows before
    # skipping them.  Header rows typically have entry_number=null,
    # empty ruling_text, and case_info containing metadata.  These
    # are emitted by _parse_page_rows when it encounters a page_header.
    header_judge: str | None = None
    header_dept: str | None = None
    header_date: str | None = None
    header_case_number: str | None = None
    for row in rows:
        if row["entry_number"] is None and not row["ruling_text"] and row.get("case_info"):
            info = row["case_info"]
            # Extract judge from "JUDGE WILLIAM D. CLASTER" pattern
            judge_match = re.search(r"JUDGE\s+(.+?)(?:\n|$)", info, re.IGNORECASE)
            if judge_match and not header_judge:
                header_judge = judge_match.group(1).strip()
            # Extract department from "Department CX101" or "Dept. C25" pattern
            dept_match = re.search(r"(?:Department|Dept\.?)\s+([A-Z0-9]+)", info, re.IGNORECASE)
            if dept_match and not header_dept:
                header_dept = dept_match.group(1).strip()
            # Extract hearing date from header (ISO, month-name, or slash formats).
            if not header_date:
                header_date = _parse_header_date(info)
            # Extract case number from header case_info (#3729 — OC single-ruling docs
            # sometimes encode the case number in the document header rather than inline).
            if not header_case_number:
                header_case_number = _extract_case_number_from_info(info)

    # Permissive second pass: when the strict pass did not find a judge or
    # department, scan case_info of ALL rows (regardless of entry_number or
    # ruling_text) for the patterns.  OC multimodal PDFs embed this metadata
    # inside regular ruling rows rather than in a dedicated header row (#3722).
    # First match wins; strict-pass hits are never overwritten.
    if not header_judge or not header_dept or not header_case_number:
        for row in rows:
            info = row.get("case_info") or ""
            if not info:
                continue
            if not header_judge:
                judge_match = re.search(
                    r"(?:Hon\.?\s+|Honorable\s+|JUDGE\s+)([A-Z][A-Za-z .'-]{2,80})",
                    info,
                    re.IGNORECASE,
                )
                if judge_match:
                    header_judge = judge_match.group(1).strip()
            if not header_dept:
                dept_match = re.search(r"(?:Department|Dept\.?)\s+([A-Z0-9]+)", info, re.IGNORECASE)
                if dept_match:
                    header_dept = dept_match.group(1).strip()
            if not header_case_number:
                header_case_number = _extract_case_number_from_info(info)
            if header_judge and header_dept and header_case_number:
                break

    # Track entry_number -> case_index for cross-reference resolution (#2317).
    entry_number_to_index: dict[int, int] = {}

    for row in rows:
        # Skip header rows (e.g., department/judge headers) that the LLM
        # may include with entry_number=null and empty ruling_text.  These
        # would otherwise be merged into adjacent cases, corrupting data.
        if row["entry_number"] is None and not row["ruling_text"]:
            continue

        if _is_new_case(row):
            cases.append(
                {
                    "case_info": row["case_info"],
                    "ruling_text": row["ruling_text"],
                    "entry_number": row["entry_number"],
                }
            )
            # Record entry_number -> case index for cross-reference lookups.
            if row["entry_number"] is not None:
                entry_number_to_index[row["entry_number"]] = len(cases) - 1
        elif cases:
            # Continuation: merge into previous case.
            if row["case_info"]:
                cases[-1]["case_info"] += "\n" + row["case_info"]
            if row["ruling_text"]:
                cases[-1]["ruling_text"] += "\n" + row["ruling_text"]
        else:
            # No previous case to merge into — start a new case if there's
            # meaningful content.  This handles continuations from a previous
            # page's last case or header rows.
            if row["case_info"] or row["ruling_text"]:
                cases.append(
                    {
                        "case_info": row["case_info"],
                        "ruling_text": row["ruling_text"],
                        "entry_number": row["entry_number"],
                    }
                )

    # Convert to ExtractedRuling objects.  Each grouped case may be a
    # FUSED row that the LLM concatenated from two (or more) adjacent
    # cases — split it with _split_fused_case_info (#2500) and emit one
    # ExtractedRuling per sub-case.  The first sub-case keeps the
    # ruling_text (the body almost certainly belongs to the case whose
    # title appears first in the fused string); subsequent sub-cases
    # get ruling_text=None so they do not silently claim the first
    # case's ruling body.
    #
    # When fusion splits a case into N>1 rulings, any entry_number that
    # originally pointed to this case index needs to be remapped to the
    # new index of the FIRST sub-case in ``rulings``.  Build a
    # case_index -> ruling_index map as we go, then rewrite
    # entry_number_to_index after the loop.
    rulings: list[ExtractedRuling] = []
    case_index_to_ruling_index: dict[int, int] = {}
    for case_idx, case in enumerate(cases):
        case_index_to_ruling_index[case_idx] = len(rulings)
        sub_case_infos = _split_fused_case_info(case["case_info"])
        for idx, sub_info in enumerate(sub_case_infos):
            sub_text = case["ruling_text"] if idx == 0 else None
            # Only the first sub-case of a fused split inherits the row's
            # entry_number — subsequent sub-cases stay None because the
            # original entry_number unambiguously refers to the first ruling.
            sub_entry_number = case["entry_number"] if idx == 0 else None
            # Pass header_case_number + single_ruling_doc only when there
            # is exactly one case AND no fused-row split occurred (#3729).
            # Fused-split sub-cases (idx > 0) must never inherit the header
            # case_number — the number belongs to the first sub-case, not
            # the continuation block that was merged by the LLM.
            _effective_header_cn = (
                header_case_number if len(cases) == 1 and len(sub_case_infos) == 1 else None
            )
            _append_ruling_from_case(
                rulings,
                sub_info,
                sub_text,
                metadata=metadata,
                header_judge=header_judge,
                header_dept=header_dept,
                header_date=header_date,
                header_case_number=_effective_header_cn,
                single_ruling_doc=(len(cases) == 1 and len(sub_case_infos) == 1),
                entry_number=sub_entry_number,
            )

    # Remap entry_number_to_index to point at the first sub-case's ruling
    # index so cross-reference resolution in _resolve_cross_references
    # continues to find the right ExtractedRuling after any fusion
    # splits.
    entry_number_to_index = {
        entry_num: case_index_to_ruling_index[case_idx]
        for entry_num, case_idx in entry_number_to_index.items()
        if case_idx in case_index_to_ruling_index
    }

    # Post-processing: resolve cross-reference entries (#2317, #3857).
    # Santa Clara PDFs use cross-references like "See Line 4 for tentative
    # ruling" when multiple motions share one ruling.  Riverside PDFs use
    # "See Ruling for #N Above".  Orange County PDFs use case-number refs.
    # Copy the ruling text from the referenced entry so enrichment can
    # extract an outcome.
    case_number_to_index = _build_case_number_to_index(rulings)
    rulings = _resolve_cross_references(
        rulings, entry_number_to_index or None, case_number_to_index
    )

    # Post-processing: drop SC body-section orphans (#3663).
    # SC multi-page PDFs produce a body-section row with an empty
    # case_number, role-literal title ("Plaintiff v. FCA"), and no
    # entry_number.  Must run AFTER cross-reference resolution so any
    # calendar row that WAS resolved keeps its real ruling_text, and the
    # orphan — which has no entry_number and thus was never a cross-ref
    # target — is discarded.  Run BEFORE the calendar-listing filter so the
    # orphan's long ruling_text doesn't confuse the calendar-only heuristic.
    rulings = _drop_role_literal_orphan_rulings(rulings)

    # Post-processing: drop calendar-listing-only rows (#2446).
    # Orange County department calendar PDFs sometimes list cases with only
    # the motion-type heading or "OFF-CALENDAR" marker in place of an actual
    # tentative ruling body.  These rows are not substantive rulings.  Run
    # this AFTER cross-reference resolution so legitimate shared text is
    # not accidentally classified as calendar-only.
    rulings = _drop_calendar_listing_rulings(rulings)

    # Post-processing: drop short-unsubstantive rulings that slipped
    # through the pattern-based calendar-listing filter (#2645).  Catches
    # empty-cell OC calendar rows where the LLM filled ruling_text with
    # noise (case caption fragment, motion label without disposition)
    # instead of a real tentative ruling body.  Must run AFTER the
    # pattern-based filter so pattern-matched rows log under their specific
    # marker, and AFTER cross-reference resolution so shared text isn't
    # misclassified as unsubstantive.
    rulings = _drop_short_unsubstantive_rulings(rulings)

    # Post-processing: truncate concatenated case titles (#2562).
    # Santa Clara multi-case PDFs sometimes produce an ``extracted_case_title``
    # that fuses adjacent calendar lines ("Smith v. Jones Doe v. Roe").  Run
    # this BEFORE ``_deduplicate_ruling_texts`` because the truncated title
    # is a more accurate signal for downstream dedup heuristics and for the
    # deterministic flag rule ``check_no_multiple_adversarial_patterns`` in
    # the worker's validation step.
    rulings = _truncate_concatenated_case_titles(rulings)
    rulings = _truncate_repeated_name_tails(rulings)

    # Post-processing: deduplicate identical ruling texts (#2096).
    # The LLM sometimes produces the same ruling text for multiple cases
    # in the same PDF.  Keep only the first occurrence; null out duplicates.
    rulings = _deduplicate_ruling_texts(rulings)

    # Post-processing: remove citation artifacts (#2448).
    # When a PDF contains a Request for Judicial Notice citing many other
    # courts' orders, the LLM may return each citation as a separate ruling.
    # Filter those out using title+length heuristics.
    rulings = _filter_citation_artifacts(rulings)

    return rulings


# ---------------------------------------------------------------------------
# PDF page rendering
# ---------------------------------------------------------------------------

# Resolution for rendering PDF pages to images (DPI).
_PDF_RENDER_RESOLUTION = 150


def _render_pdf_pages(
    pdf_bytes: bytes,
    max_pages: int,
) -> list[tuple[bytes, str]]:
    """Render PDF pages to PNG images using pymupdf.

    Uses pymupdf (fitz) for higher quality image rendering compared to
    pdfplumber, which was validated in the OC multimodal eval (PR #1692).

    Returns a list of ``(png_bytes, media_type)`` tuples.
    """
    import pymupdf

    results: list[tuple[bytes, str]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        zoom = _PDF_RENDER_RESOLUTION / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)

        for i, page in enumerate(doc):
            if i >= max_pages:
                logger.info(
                    "llm_extractor.page_limit_reached",
                    max_pages=max_pages,
                    total_pages=len(doc),
                )
                break

            pix = page.get_pixmap(matrix=matrix)
            png_bytes = pix.tobytes("png")
            results.append((png_bytes, "image/png"))

    return results


# ---------------------------------------------------------------------------
# Google client factory
# ---------------------------------------------------------------------------


def _create_google_client(*, api_key: str | None = None) -> object:
    """Create a Google GenAI client.

    If *api_key* is provided, uses it directly.  Otherwise falls back to
    the ``GOOGLE_API_KEY`` environment variable.

    Raises ``ValueError`` if no API key is available.
    """
    import os

    from google import genai

    resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not resolved_key:
        msg = (
            "No Google API key provided — pass api_key= or set "
            "the GOOGLE_API_KEY environment variable."
        )
        raise ValueError(msg)
    return genai.Client(api_key=resolved_key)
