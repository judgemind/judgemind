#!/usr/bin/env python3
"""Eval LLM-based extraction on Contra Costa County PDF fixtures.

Contra Costa PDFs are **text-based** (pdfplumber extracts clean text), so the
LLM receives extracted text rather than page images.  The LLM's job:

  1. Split multi-case department PDFs into individual rulings
  2. Extract structured fields for each case (case_number, case_title, etc.)
  3. Handle two distinct formats:
     - Civil (Dept 14, 16): numbered entries with "CASE NUMBER:" / "CASE NAME:"
     - Probate (Dept 30): entries like "N25-2307 IN THE MATTER OF: ..."

This eval validates:
  - case_count: does the LLM find the right number of cases per document?
  - field completeness: are all required fields non-null?
  - case_number accuracy: do extracted case numbers match ground truth?
  - case_title quality: are titles extracted and reasonably close to expected?
  - outcome accuracy: do extracted outcomes match ground truth?
  - ruling_text: is it non-empty for each case?

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    export GOOGLE_API_KEY="AIza..."

    # Run default model (Gemini Flash Lite)
    python3 scripts/eval/eval_cc_extraction.py

    # Run specific model(s)
    python3 scripts/eval/eval_cc_extraction.py --models gemini-2.5-flash-lite

    # Run all models
    python3 scripts/eval/eval_cc_extraction.py --models claude-haiku-4.5 gemini-2.5-flash-lite gemini-2.5-flash

    # Save results to JSON
    python3 scripts/eval/eval_cc_extraction.py --save

Requirements:
    pip install anthropic google-genai pdfplumber
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
FIXTURES_DIR = REPO_ROOT / "packages" / "scraper-framework" / "tests" / "fixtures"
EXPECTED_DIR = FIXTURES_DIR / "expected"
RESULTS_DIR = SCRIPT_DIR / "results"

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

MODELS: dict[str, dict] = {
    "gemini-2.5-flash-lite": {
        "provider": "google",
        "model_id": "gemini-2.5-flash-lite",
        "pricing_per_m": {"input": 0.075, "output": 0.30},
    },
    "gemini-2.5-flash": {
        "provider": "google",
        "model_id": "gemini-2.5-flash",
        "pricing_per_m": {"input": 0.15, "output": 0.60},
    },
    "claude-haiku-4.5": {
        "provider": "anthropic",
        "model_id": "claude-haiku-4-5-20251001",
        "pricing_per_m": {"input": 0.80, "output": 4.00},
    },
}

# ---------------------------------------------------------------------------
# Contra Costa extraction prompt
# ---------------------------------------------------------------------------

CC_SYSTEM_PROMPT = (
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
    "- `C##-#####` — civil unlimited (e.g., C24-02490, C23-00908)\n"
    "- `L##-#####` — limited civil (e.g., L23-06679, L25-01552)\n"
    "- `N##-####` — name change / probate (e.g., N25-2307)\n"
    "- `P##-#####` — probate (e.g., P23-01484, P26-00022)\n"
    "- `RS##-####` — Rossmoor/Richmond (e.g., RS24-0953)\n"
    "- `A##-#####` — adoption (e.g., A26-00006)\n"
    "- `MS####` — miscellaneous (e.g., MS5031)\n"
    "- `MSP##-#####` — miscellaneous probate (e.g., MSP19-01440)\n\n"
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
    '  "extracted_judge_name": "<full judge name>" or null,\n'
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
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    """Result for a single case within a fixture."""

    line_number: int | None = None
    case_number: str | None = None
    case_title: str | None = None
    case_type: str | None = None
    outcome: str | None = None
    motion_type: str | None = None
    parties: list[dict[str, str]] = field(default_factory=list)
    ruling_text_length: int = 0


@dataclass
class FixtureResult:
    """Result from processing a single fixture."""

    fixture_name: str
    model: str
    expected: dict
    expected_case_count: int = 0
    actual_case_count: int = 0
    cases: list[CaseResult] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class ModelSummary:
    """Aggregated results for a single model."""

    model: str
    total_fixtures: int = 0
    case_count_correct: int = 0
    case_count_wrong: int = 0
    case_number_match: int = 0
    case_number_total: int = 0
    title_extracted: int = 0
    title_total: int = 0
    title_match: int = 0
    outcome_match: int = 0
    outcome_total: int = 0
    ruling_text_present: int = 0
    ruling_text_total: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_per_pdf: float = 0.0
    estimated_monthly_cost: float = 0.0
    fixture_details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def extract_pdf_text(pdf_path: Path) -> str | None:
    """Extract text from a Contra Costa PDF using pdfplumber."""
    import pdfplumber

    pages_text: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)

    if not pages_text:
        return None

    return "\n\n".join(pages_text)


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
CALL_TIMEOUT_S = 120

# Control characters (0x00-0x1F) except tab (\t), newline (\n), carriage return (\r)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_json(text: str) -> str:
    """Strip invalid JSON control characters from LLM output."""
    return _CONTROL_CHAR_RE.sub("", text)


def _call_with_retry(
    fn: object,
    *args: object,
    max_retries: int = MAX_RETRIES,
    timeout_s: float = CALL_TIMEOUT_S,
) -> tuple[dict, int, int, float]:
    """Retry wrapper for LLM API calls."""
    import concurrent.futures

    for attempt in range(max_retries):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fn, *args)
                result = future.result(timeout=timeout_s)
                return result
        except concurrent.futures.TimeoutError:
            if attempt < max_retries - 1:
                wait = 2**attempt
                print(
                    "TIMEOUT ("
                    + str(timeout_s)
                    + "s), retry "
                    + str(attempt + 1)
                    + "...",
                    end=" ",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise TimeoutError("Timed out after " + str(max_retries) + " retries")
        except Exception as e:
            err_str = str(e)
            if ("503" in err_str or "529" in err_str) and attempt < max_retries - 1:
                wait = 2**attempt
                print(
                    "Retryable error, retry " + str(attempt + 1) + "...",
                    end=" ",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise

    msg = "Unreachable"
    raise RuntimeError(msg)


def call_gemini(
    model_id: str,
    text: str,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Gemini API with text input.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        system_instruction=CC_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=16384,
        response_mime_type="application/json",
    )

    start = time.monotonic()
    response = client.models.generate_content(
        model=model_id,
        contents=text,
        config=config,
    )
    latency = (time.monotonic() - start) * 1000

    input_tokens = 0
    output_tokens = 0
    if response.usage_metadata:
        input_tokens = response.usage_metadata.prompt_token_count or 0
        output_tokens = response.usage_metadata.candidates_token_count or 0

    # Access raw text from candidate parts to avoid SDK-level JSON parsing
    # that can fail on control characters in verbatim ruling text.
    response_text = ""
    try:
        response_text = response.text.strip() if response.text else ""
    except Exception:
        # Fallback: extract raw text from candidate parts directly
        if response.candidates:
            parts = response.candidates[0].content.parts
            if parts:
                response_text = parts[0].text.strip()

    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        response_text = "\n".join(lines)

    sanitized = _sanitize_json(response_text)
    try:
        result = json.loads(sanitized)
    except json.JSONDecodeError:
        start_idx = sanitized.find("{")
        end_idx = sanitized.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            try:
                result = json.loads(sanitized[start_idx:end_idx])
            except json.JSONDecodeError:
                result = {
                    "rulings": [],
                    "_parse_error": sanitized[:200],
                }
        else:
            result = {
                "rulings": [],
                "_parse_error": sanitized[:200],
            }

    if isinstance(result, list):
        result = {"rulings": result}

    return result, input_tokens, output_tokens, latency


def call_anthropic(
    model_id: str,
    text: str,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Anthropic API with text input.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    start = time.monotonic()
    response = client.messages.create(
        model=model_id,
        max_tokens=16384,
        temperature=0,
        system=CC_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    latency = (time.monotonic() - start) * 1000

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    response_text = ""
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            response_text = block.text.strip()
            break

    if response_text.startswith("```"):
        response_text = re.sub(r"^```\w*\n?", "", response_text)
        response_text = re.sub(r"\n?```$", "", response_text)

    sanitized = _sanitize_json(response_text)
    try:
        result = json.loads(sanitized)
    except json.JSONDecodeError:
        start_idx = sanitized.find("{")
        end_idx = sanitized.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            result = json.loads(sanitized[start_idx:end_idx])
        else:
            result = {
                "rulings": [],
                "_parse_error": sanitized[:200],
            }

    if isinstance(result, list):
        result = {"rulings": result}

    return result, input_tokens, output_tokens, latency


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def get_cc_fixtures() -> list[tuple[Path, dict]]:
    """Load all CC PDF fixtures that have expected ground truth JSON."""
    fixtures = []
    for expected_file in sorted(EXPECTED_DIR.glob("cc_*.json")):
        expected = json.loads(expected_file.read_text())
        fixture_name = expected.get("_fixture")
        if not fixture_name:
            continue
        fixture_path = FIXTURES_DIR / fixture_name
        if not fixture_path.exists():
            continue
        if fixture_path.suffix != ".pdf":
            continue
        if expected.get("case_count", 0) == 0:
            continue
        if "cases" not in expected:
            continue
        fixtures.append((fixture_path, expected))
    return fixtures


# ---------------------------------------------------------------------------
# Title normalization and matching
# ---------------------------------------------------------------------------


def normalize_title(title: str | None) -> str | None:
    """Normalize case title for fuzzy comparison."""
    if title is None:
        return None
    s = title.strip().lower()
    # Normalize vs variants
    s = re.sub(r"\bvs\.?\s+", "v. ", s)
    s = re.sub(r"\bversus\s+", "v. ", s)
    # Remove "et al." variants
    s = re.sub(r",?\s*\bet\.?\s*al\.?", "", s)
    # Normalize "in the matter of" variants
    s = re.sub(r"in\s+the\s+matter\s+of:?\s*", "", s)
    s = re.sub(r"conservatorship\s+of:?\s*", "", s)
    s = re.sub(r"guardianship\s+of:?\s*", "", s)
    s = re.sub(r"estate\s+of:?\s*", "", s)
    s = re.sub(r"limited\s+conservatorship\s+of:?\s*", "", s)
    s = re.sub(r"cons?\.\s+of:?\s*", "", s)
    s = re.sub(r"est\.?\s+of:?\s*", "", s)
    s = re.sub(r"g'ship\s+of\s+minors:?\s*", "", s)
    s = re.sub(r"petition\s+of:?\s*", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Remove trailing punctuation
    s = s.rstrip(".,;: ")
    return s


def titles_match(extracted: str | None, expected: str | None) -> bool:
    """Check if two case titles are roughly equivalent."""
    e1 = normalize_title(extracted)
    e2 = normalize_title(expected)
    if e1 is None or e2 is None:
        # Both null counts as match
        return e1 == e2

    from difflib import SequenceMatcher

    if e1 in e2 or e2 in e1:
        return True
    return SequenceMatcher(None, e1, e2).ratio() > 0.7


def normalize_outcome(outcome: str | None) -> str | None:
    """Normalize outcome for comparison."""
    if outcome is None:
        return None
    s = outcome.strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_fixture(result: FixtureResult) -> dict:
    """Score a fixture result against expected values."""
    expected_cases = result.expected.get("cases", [])
    expected_count = len(expected_cases)
    actual_count = result.actual_case_count

    count_exact = expected_count == actual_count

    # Track per-case scoring
    case_number_matches = 0
    title_extracted_count = 0
    title_matches = 0
    outcome_matches = 0
    outcome_total = 0
    ruling_text_present = 0
    ruling_text_total = 0

    case_details: list[dict] = []

    # Match expected cases to extracted cases by position (index order)
    # since same case_number can appear multiple times
    extracted_cases = list(result.cases)

    for i, exp_case in enumerate(expected_cases):
        # Try to find a matching extracted case
        matched_case = None

        # First try: match by case number AND position
        if i < len(extracted_cases):
            candidate = extracted_cases[i]
            if candidate.case_number and exp_case.get("case_number"):
                if candidate.case_number.upper() == exp_case["case_number"].upper():
                    matched_case = candidate

        # Second try: search all unmatched extracted cases by case number
        if matched_case is None:
            for c in extracted_cases:
                if c.case_number and exp_case.get("case_number"):
                    if c.case_number.upper() == exp_case["case_number"].upper():
                        matched_case = c
                        break

        detail: dict = {
            "expected_case_number": exp_case.get("case_number"),
            "expected_title": exp_case.get("case_title"),
            "expected_outcome": exp_case.get("outcome"),
        }

        if matched_case is None:
            detail["matched"] = False
            detail["extracted_case_number"] = None
            detail["extracted_title"] = None
            detail["extracted_outcome"] = None
            detail["case_number_match"] = False
            detail["title_match"] = False
            detail["outcome_match"] = False
            detail["has_ruling_text"] = False
            case_details.append(detail)
            if exp_case.get("outcome"):
                outcome_total += 1
            ruling_text_total += 1
            continue

        detail["matched"] = True
        detail["extracted_case_number"] = matched_case.case_number
        detail["extracted_title"] = matched_case.case_title
        detail["extracted_outcome"] = matched_case.outcome

        # Case number match
        if matched_case.case_number and exp_case.get("case_number"):
            if matched_case.case_number.upper() == exp_case["case_number"].upper():
                case_number_matches += 1
                detail["case_number_match"] = True
            else:
                detail["case_number_match"] = False
        else:
            detail["case_number_match"] = False

        # Title scoring
        if matched_case.case_title is not None:
            title_extracted_count += 1
        if titles_match(matched_case.case_title, exp_case.get("case_title")):
            title_matches += 1
            detail["title_match"] = True
        else:
            detail["title_match"] = False

        # Outcome scoring
        if exp_case.get("outcome"):
            outcome_total += 1
            exp_outcome = normalize_outcome(exp_case["outcome"])
            ext_outcome = normalize_outcome(matched_case.outcome)
            if exp_outcome == ext_outcome:
                outcome_matches += 1
                detail["outcome_match"] = True
            else:
                detail["outcome_match"] = False
        else:
            detail["outcome_match"] = True  # No expected outcome

        # Ruling text
        ruling_text_total += 1
        if matched_case.ruling_text_length > 0:
            ruling_text_present += 1
            detail["has_ruling_text"] = True
        else:
            detail["has_ruling_text"] = False

        case_details.append(detail)

    return {
        "fixture_name": result.fixture_name,
        "expected_case_count": expected_count,
        "actual_case_count": actual_count,
        "count_exact": count_exact,
        "case_number_matches": case_number_matches,
        "case_number_total": expected_count,
        "title_extracted": title_extracted_count,
        "title_total": len(expected_cases),
        "title_matches": title_matches,
        "outcome_matches": outcome_matches,
        "outcome_total": outcome_total,
        "ruling_text_present": ruling_text_present,
        "ruling_text_total": ruling_text_total,
        "case_details": case_details,
    }


# ---------------------------------------------------------------------------
# Model analysis
# ---------------------------------------------------------------------------


def analyze_model(
    results: list[FixtureResult],
    model_name: str,
    pricing: dict[str, float],
) -> ModelSummary:
    """Analyze results for a single model."""
    summary = ModelSummary(model=model_name)
    valid = [r for r in results if r.error is None]
    summary.total_fixtures = len(valid)

    if not valid:
        return summary

    for r in valid:
        summary.total_input_tokens += r.input_tokens
        summary.total_output_tokens += r.output_tokens

        scores = score_fixture(r)
        summary.fixture_details.append(scores)

        if scores["count_exact"]:
            summary.case_count_correct += 1
        else:
            summary.case_count_wrong += 1

        summary.case_number_match += scores["case_number_matches"]
        summary.case_number_total += scores["case_number_total"]
        summary.title_extracted += scores["title_extracted"]
        summary.title_total += scores["title_total"]
        summary.title_match += scores["title_matches"]
        summary.outcome_match += scores["outcome_matches"]
        summary.outcome_total += scores["outcome_total"]
        summary.ruling_text_present += scores["ruling_text_present"]
        summary.ruling_text_total += scores["ruling_text_total"]

    for r in results:
        if r.error:
            summary.errors.append(r.fixture_name + ": " + r.error)

    latencies = [r.latency_ms for r in valid]
    summary.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    # Cost calculations
    avg_in = summary.total_input_tokens / len(valid) if valid else 0
    avg_out = summary.total_output_tokens / len(valid) if valid else 0
    summary.cost_per_pdf = (
        avg_in * pricing["input"] + avg_out * pricing["output"]
    ) / 1_000_000

    # Estimate: ~10 department PDFs/day for CC
    # (5 depts x 2 scrapes, or 10 depts x 1 scrape)
    summary.estimated_monthly_cost = summary.cost_per_pdf * 10 * 30

    return summary


def print_model_report(summary: ModelSummary) -> None:
    """Print a detailed report for a single model."""
    print("\n--- " + summary.model + " ---\n")
    print("Fixtures processed: " + str(summary.total_fixtures))
    print(
        "Total tokens: "
        + f"{summary.total_input_tokens:,}"
        + " input + "
        + f"{summary.total_output_tokens:,}"
        + " output"
    )
    print("Avg latency per fixture: " + f"{summary.avg_latency_ms:,.0f}" + "ms")

    if summary.total_fixtures == 0:
        print("\nNo fixtures processed.")
        return

    # Case count accuracy
    total = summary.case_count_correct + summary.case_count_wrong
    if total > 0:
        pct = summary.case_count_correct / total * 100
        print(
            "\nCase count accuracy: "
            + f"{summary.case_count_correct}/{total} "
            + f"({pct:.0f}%)"
        )

    # Case number accuracy
    if summary.case_number_total > 0:
        cn_pct = summary.case_number_match / summary.case_number_total * 100
        print(
            "Case number accuracy: "
            + f"{summary.case_number_match}/{summary.case_number_total} "
            + f"({cn_pct:.0f}%)"
        )

    # Title extraction
    if summary.title_total > 0:
        ext_pct = summary.title_extracted / summary.title_total * 100
        match_pct = summary.title_match / summary.title_total * 100
        print(
            "Case title extraction: "
            + f"{summary.title_extracted}/{summary.title_total} "
            + f"({ext_pct:.0f}%) extracted, "
            + f"{summary.title_match}/{summary.title_total} "
            + f"({match_pct:.0f}%) matching expected"
        )

    # Outcome accuracy
    if summary.outcome_total > 0:
        out_pct = summary.outcome_match / summary.outcome_total * 100
        print(
            "Outcome accuracy: "
            + f"{summary.outcome_match}/{summary.outcome_total} "
            + f"({out_pct:.0f}%)"
        )

    # Ruling text presence
    if summary.ruling_text_total > 0:
        rt_pct = summary.ruling_text_present / summary.ruling_text_total * 100
        print(
            "Ruling text present: "
            + f"{summary.ruling_text_present}/{summary.ruling_text_total} "
            + f"({rt_pct:.0f}%)"
        )

    # Cost
    print("\nCost per PDF: $" + f"{summary.cost_per_pdf:.5f}")
    print(
        "Estimated monthly cost (~10 dept PDFs/day): $"
        + f"{summary.estimated_monthly_cost:.2f}"
    )

    # Fixture details
    print("\nPer-fixture breakdown:")
    for detail in summary.fixture_details:
        status = "PASS" if detail["count_exact"] else "FAIL"
        print(
            f"  {detail['fixture_name']}: "
            f"cases {detail['actual_case_count']}/{detail['expected_case_count']} [{status}]"
            f"  case#s {detail['case_number_matches']}/{detail['case_number_total']}"
            f"  titles {detail['title_matches']}/{detail['title_total']}"
            f"  outcomes {detail['outcome_matches']}/{detail['outcome_total']}"
            f"  ruling_text {detail['ruling_text_present']}/{detail['ruling_text_total']}"
        )
        for case_d in detail.get("case_details", []):
            match_str = "MATCHED" if case_d.get("matched") else "MISSING"
            cn_str = "OK" if case_d.get("case_number_match") else "FAIL"
            title_str = "OK" if case_d.get("title_match") else "FAIL"
            outcome_str = "OK" if case_d.get("outcome_match") else "FAIL"
            rt_str = "OK" if case_d.get("has_ruling_text") else "NONE"
            print(
                f"    {case_d.get('expected_case_number', '?')}: {match_str}"
                f"  case#={cn_str}"
                f"  title={title_str}"
                f" (got: {case_d.get('extracted_title', 'null')!r},"
                f" exp: {case_d.get('expected_title', 'null')!r})"
                f"  outcome={outcome_str}"
                f" (got: {case_d.get('extracted_outcome', 'null')!r},"
                f" exp: {case_d.get('expected_outcome', 'null')!r})"
                f"  text={rt_str}"
            )

    if summary.errors:
        print("\nErrors:")
        for err in summary.errors:
            print("  " + err)


# ---------------------------------------------------------------------------
# Main eval pipeline
# ---------------------------------------------------------------------------


def run_eval(
    model_name: str,
    model_config: dict,
) -> list[FixtureResult]:
    """Run extraction eval for a single model against all CC fixtures."""
    fixtures = get_cc_fixtures()
    if not fixtures:
        print("No CC fixtures found with expected data.")
        return []

    provider = model_config["provider"]
    model_id = model_config["model_id"]

    # Get API key
    if provider == "google":
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            print("GOOGLE_API_KEY not set, skipping " + model_name)
            return []
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("ANTHROPIC_API_KEY not set, skipping " + model_name)
            return []

    print("\nProcessing " + str(len(fixtures)) + " CC fixtures with " + model_name)

    results: list[FixtureResult] = []

    for fixture_path, expected in fixtures:
        fixture_name = fixture_path.name
        print(f"  {fixture_name}... ", end="", flush=True)

        text = extract_pdf_text(fixture_path)
        if text is None:
            print("SKIP (no text extracted)")
            result = FixtureResult(
                fixture_name=fixture_name,
                model=model_name,
                expected=expected,
                error="No text extracted from PDF",
            )
            results.append(result)
            continue

        try:
            call_fn = call_gemini if provider == "google" else call_anthropic
            extraction, in_tok, out_tok, latency = _call_with_retry(
                call_fn, model_id, text, api_key
            )

            rulings = extraction.get("rulings", [])
            cases: list[CaseResult] = []
            for r in rulings:
                parties = r.get("extracted_parties", [])
                ruling_text = r.get("ruling_text") or ""
                cases.append(
                    CaseResult(
                        line_number=r.get("line_number"),
                        case_number=r.get("extracted_case_number"),
                        case_title=r.get("extracted_case_title"),
                        case_type=r.get("case_type"),
                        outcome=r.get("outcome"),
                        motion_type=r.get("motion_type"),
                        parties=parties,
                        ruling_text_length=len(ruling_text),
                    )
                )

            result = FixtureResult(
                fixture_name=fixture_name,
                model=model_name,
                expected=expected,
                expected_case_count=len(expected.get("cases", [])),
                actual_case_count=len(rulings),
                cases=cases,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency,
            )
            results.append(result)
            print(
                f"OK ({len(rulings)} cases, {latency:.0f}ms, {in_tok}+{out_tok} tokens)"
            )

        except Exception as e:
            print("ERROR: " + str(e))
            result = FixtureResult(
                fixture_name=fixture_name,
                model=model_name,
                expected=expected,
                error=str(e),
            )
            results.append(result)

    return results


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Eval LLM extraction on Contra Costa PDF fixtures."
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=None,
        help="Model(s) to evaluate. Default: gemini-2.5-flash-lite.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to JSON in scripts/eval/results/.",
    )
    parser.add_argument(
        "--check-thresholds",
        action="store_true",
        help="Exit non-zero if quality thresholds are not met.",
    )

    args = parser.parse_args()

    models_to_run = args.models or ["gemini-2.5-flash-lite"]
    all_summaries: list[ModelSummary] = []
    threshold_ok = True

    for model_name in models_to_run:
        if model_name not in MODELS:
            print("Unknown model: " + model_name)
            continue

        config = MODELS[model_name]
        results = run_eval(model_name, config)

        if not results:
            continue

        summary = analyze_model(results, model_name, config["pricing_per_m"])
        all_summaries.append(summary)
        print_model_report(summary)

        if args.save:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            save_path = RESULTS_DIR / (
                "cc_" + model_name.replace(".", "_") + "_results.json"
            )
            save_data = {
                "model": model_name,
                "total_fixtures": summary.total_fixtures,
                "case_count_correct": summary.case_count_correct,
                "case_number_match": summary.case_number_match,
                "case_number_total": summary.case_number_total,
                "title_extracted": summary.title_extracted,
                "title_total": summary.title_total,
                "title_match": summary.title_match,
                "outcome_match": summary.outcome_match,
                "outcome_total": summary.outcome_total,
                "ruling_text_present": summary.ruling_text_present,
                "ruling_text_total": summary.ruling_text_total,
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "cost_per_pdf": summary.cost_per_pdf,
                "estimated_monthly_cost": summary.estimated_monthly_cost,
                "fixture_details": summary.fixture_details,
            }
            save_path.write_text(
                json.dumps(save_data, indent=2, default=str),
                encoding="utf-8",
            )
            print("\nResults saved to: " + str(save_path))

        # Threshold checks
        if args.check_thresholds:
            # Case number accuracy: >= 90%
            if summary.case_number_total > 0:
                cn_rate = summary.case_number_match / summary.case_number_total
                if cn_rate < 0.90:
                    print(f"\nTHRESHOLD FAIL: case number accuracy {cn_rate:.0%} < 90%")
                    threshold_ok = False

            # Title extraction: >= 90%
            if summary.title_total > 0:
                title_rate = summary.title_extracted / summary.title_total
                if title_rate < 0.90:
                    print(f"\nTHRESHOLD FAIL: title extraction {title_rate:.0%} < 90%")
                    threshold_ok = False

            # Outcome accuracy: >= 80%
            if summary.outcome_total > 0:
                outcome_rate = summary.outcome_match / summary.outcome_total
                if outcome_rate < 0.80:
                    print(
                        f"\nTHRESHOLD FAIL: outcome accuracy {outcome_rate:.0%} < 80%"
                    )
                    threshold_ok = False

            # Ruling text presence: >= 90%
            if summary.ruling_text_total > 0:
                rt_rate = summary.ruling_text_present / summary.ruling_text_total
                if rt_rate < 0.90:
                    print(f"\nTHRESHOLD FAIL: ruling text presence {rt_rate:.0%} < 90%")
                    threshold_ok = False

    # Print comparison if multiple models
    if len(all_summaries) > 1:
        print("\n\n=== MODEL COMPARISON ===\n")
        header = f"{'Metric':<30}"
        for s in all_summaries:
            header += f" {s.model:<22}"
        print(header)
        print("-" * len(header))

        metrics = [
            (
                "Case count accuracy",
                lambda s: (
                    f"{s.case_count_correct}/"
                    f"{s.case_count_correct + s.case_count_wrong}"
                ),
            ),
            (
                "Case # accuracy",
                lambda s: (
                    f"{s.case_number_match}/{s.case_number_total} "
                    f"({s.case_number_match / s.case_number_total:.0%})"
                    if s.case_number_total > 0
                    else "N/A"
                ),
            ),
            (
                "Title extraction",
                lambda s: (
                    f"{s.title_extracted}/{s.title_total}"
                    if s.title_total > 0
                    else "N/A"
                ),
            ),
            (
                "Title match",
                lambda s: (
                    f"{s.title_match}/{s.title_total} "
                    f"({s.title_match / s.title_total:.0%})"
                    if s.title_total > 0
                    else "N/A"
                ),
            ),
            (
                "Outcome accuracy",
                lambda s: (
                    f"{s.outcome_match}/{s.outcome_total} "
                    f"({s.outcome_match / s.outcome_total:.0%})"
                    if s.outcome_total > 0
                    else "N/A"
                ),
            ),
            (
                "Ruling text present",
                lambda s: (
                    f"{s.ruling_text_present}/{s.ruling_text_total} "
                    f"({s.ruling_text_present / s.ruling_text_total:.0%})"
                    if s.ruling_text_total > 0
                    else "N/A"
                ),
            ),
            (
                "Cost per PDF",
                lambda s: f"${s.cost_per_pdf:.5f}",
            ),
            (
                "Monthly cost est.",
                lambda s: f"${s.estimated_monthly_cost:.2f}",
            ),
        ]

        for label, fn in metrics:
            row = f"{label:<30}"
            for s in all_summaries:
                row += f" {fn(s):<22}"
            print(row)

    if args.check_thresholds and not threshold_ok:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
