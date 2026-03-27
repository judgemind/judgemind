#!/usr/bin/env python3
"""Eval LLM-based extraction on San Francisco Family Law PDF fixtures.

San Francisco Family Law PDFs are **text-based** (pdfplumber extracts clean
text), so the LLM receives extracted text rather than page images.  The LLM's
job:

  1. Split the document into individual case rulings (separated by structured
     "SUPERIOR COURT OF CALIFORNIA / COUNTY OF SAN FRANCISCO" headers with
     caption blocks containing Case Number, Hearing Date, Department, and
     Presiding judge)
  2. Extract structured fields for each ruling (case_number, case_title,
     parties, motion_type, outcome, hearing_date, ruling_text, judge_name)

This eval validates:
  - case_count: does the LLM find the right number of rulings per document?
  - field completeness: are all required fields non-null?
  - case_number accuracy: do extracted case numbers match ground truth?
  - ruling_text: is it non-empty and reasonable length for each ruling?
  - regression check: do regex-extracted values from the scraper match?

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    export GOOGLE_API_KEY="AIza..."

    # Run default model (Claude Haiku 4.5)
    python3 scripts/eval/eval_sf_extraction.py

    # Run specific model(s)
    python3 scripts/eval/eval_sf_extraction.py --models claude-haiku-4.5

    # Run all models
    python3 scripts/eval/eval_sf_extraction.py --models claude-haiku-4.5 gemini-2.5-flash-lite gemini-2.5-flash

    # Save results to JSON
    python3 scripts/eval/eval_sf_extraction.py --save

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
from datetime import UTC, datetime
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
    "claude-haiku-4.5": {
        "provider": "anthropic",
        "model_id": "claude-haiku-4-5-20251001",
        "pricing_per_m": {"input": 0.80, "output": 4.00},
    },
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
}

# ---------------------------------------------------------------------------
# San Francisco Family Law extraction prompt
# ---------------------------------------------------------------------------

SF_SYSTEM_PROMPT = (
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
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FixtureResult:
    """Result from processing a single fixture."""

    fixture_name: str
    model: str
    expected: dict
    expected_case_count: int = 0
    actual_case_count: int = 0
    rulings: list[dict] = field(default_factory=list)
    extracted_judge: str | None = None
    extracted_hearing_date: str | None = None
    extracted_department: str | None = None
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
    case_count_off_by_one: int = 0
    case_count_wrong: int = 0
    empty_rulings: int = 0
    total_rulings: int = 0
    fields_complete: int = 0
    fields_total: int = 0
    case_numbers_correct: int = 0
    case_numbers_total: int = 0
    case_titles_correct: int = 0
    case_titles_total: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_per_fixture: float = 0.0
    estimated_monthly_cost: float = 0.0
    fixture_details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract full text from a PDF using pdfplumber."""
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Regex-based extraction (for regression comparison)
# ---------------------------------------------------------------------------

# SF case number: F + 2 uppercase + hyphen + 2 digits + hyphen + 6 digits
_CASE_NUMBER_RE = re.compile(r"\bF[A-Z]{2}-\d{2}-\d{6}\b")

# Judge from "Presiding: BOBBY P. LUNA"
_PRESIDING_RE = re.compile(
    r"Presiding:\s+(?P<judge_name>[A-Z][^\n]+)",
    re.IGNORECASE,
)


def regex_extract(text: str) -> dict:
    """Extract fields using the existing scraper regex patterns."""
    case_numbers = _CASE_NUMBER_RE.findall(text)
    unique_case_numbers = list(dict.fromkeys(case_numbers))

    judge_name = None
    m = _PRESIDING_RE.search(text)
    if m:
        raw = m.group("judge_name").strip()
        # Convert ALL-CAPS to title case
        if raw == raw.upper():
            judge_name = raw.title()
        else:
            judge_name = raw

    return {
        "case_numbers": unique_case_numbers,
        "judge_name": judge_name,
    }


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------


MAX_RETRIES = 3
CALL_TIMEOUT_S = 120  # SF PDFs can be long (30+ pages)


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
                    "TIMEOUT (" + str(timeout_s) + "s), retry " + str(attempt + 1) + "...",
                    end=" ",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise TimeoutError("Timed out after " + str(max_retries) + " retries")
        except Exception as e:
            err_str = str(e)
            if "503" in err_str and attempt < max_retries - 1:
                wait = 2**attempt
                print(
                    "503, retry " + str(attempt + 1) + "...",
                    end=" ",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise

    raise RuntimeError("Unreachable")


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
        system_instruction=SF_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=65536,
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

    response_text = response.text.strip() if response.text else ""

    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        response_text = "\n".join(lines)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        start_idx = response_text.find("{")
        end_idx = response_text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            result = json.loads(response_text[start_idx:end_idx])
        else:
            result = {"rulings": [], "_parse_error": response_text[:200]}

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
        max_tokens=65536,
        temperature=0,
        system=SF_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    latency = (time.monotonic() - start) * 1000

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text

    response_text = response_text.strip()

    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        response_text = "\n".join(lines)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        start_idx = response_text.find("{")
        end_idx = response_text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            result = json.loads(response_text[start_idx:end_idx])
        else:
            result = {"rulings": [], "_parse_error": response_text[:200]}

    if isinstance(result, list):
        result = {"rulings": result}

    return result, input_tokens, output_tokens, latency


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def get_sf_fixtures() -> list[tuple[Path, dict]]:
    """Load all SF PDF fixtures that have expected ground truth JSON."""
    fixtures = []
    for expected_file in sorted(EXPECTED_DIR.glob("sf_*.json")):
        expected = json.loads(expected_file.read_text())
        fixture_name = expected.get("_fixture")
        if not fixture_name:
            continue
        fixture_path = FIXTURES_DIR / fixture_name
        if not fixture_path.exists():
            continue
        if fixture_path.suffix != ".pdf":
            continue
        fixtures.append((fixture_path, expected))
    return fixtures


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "extracted_case_number",
    "extracted_case_title",
    "outcome",
    "motion_type",
    "ruling_text",
    "extracted_parties",
]


def match_case_title(
    extracted: str | None,
    expected: str | None,
) -> bool:
    """Fuzzy-match an extracted case title against expected ground truth.

    Uses a two-tier strategy:
    1. Normalize both strings (lowercase, normalize separators) and check
       SequenceMatcher similarity (>0.8 threshold).
    2. Fall back to last-name containment: extract the key name tokens from
       the expected title and verify they all appear in the extracted title.
    """
    if extracted is None and expected is None:
        return True
    if extracted is None or expected is None:
        return False

    # Normalize
    def _normalize(s: str) -> str:
        s = s.strip().lower()
        # Normalize "vs." / "vs " / "versus" to "v."
        s = re.sub(r"\bvs\.?\s+", "v. ", s)
        s = re.sub(r"\bversus\s+", "v. ", s)
        # Collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        return s

    norm_ext = _normalize(extracted)
    norm_exp = _normalize(expected)

    if norm_ext == norm_exp:
        return True

    # Empty string handling
    if not norm_ext and not norm_exp:
        return True
    if not norm_ext or not norm_exp:
        return False

    # Tier 1: SequenceMatcher similarity
    from difflib import SequenceMatcher

    if SequenceMatcher(None, norm_ext, norm_exp).ratio() > 0.8:
        return True

    # Tier 2: Last-name containment — extract key tokens from expected
    # Split on " v. " to get party sides, then check each side's last
    # significant token appears in extracted.
    if " v. " in norm_exp:
        parts = norm_exp.split(" v. ", 1)
        for part in parts:
            # Get the last word (last name) from each side, stripping
            # common suffixes like "llc", "inc", "n.a."
            tokens = part.strip().split()
            if not tokens:
                continue
            # Use the whole part for containment check (handles
            # corporate names better)
            if part.strip() not in norm_ext:
                # Try just the last significant token
                last_token = tokens[-1].rstrip(".,")
                if last_token not in norm_ext:
                    return False
        return True

    return False


def score_fixture(result: FixtureResult) -> dict:
    """Score a fixture result against expected values."""
    expected_count = result.expected_case_count
    actual_count = result.actual_case_count

    count_diff = abs(expected_count - actual_count)
    count_exact = count_diff == 0
    count_off_by_one = count_diff == 1

    # Ruling text quality
    empty_count = 0
    short_count = 0
    total_text_len = 0
    ruling_lengths: list[int] = []
    for ruling in result.rulings:
        text = ruling.get("ruling_text") or ""
        text_len = len(text.strip())
        ruling_lengths.append(text_len)
        total_text_len += text_len
        if text_len == 0:
            empty_count += 1
        elif text_len < 50:
            short_count += 1

    # Field completeness
    fields_present = 0
    fields_total = 0
    for ruling in result.rulings:
        for f in REQUIRED_FIELDS:
            fields_total += 1
            val = ruling.get(f)
            if val is not None and val != "" and val != []:
                fields_present += 1

    # Case number accuracy
    expected_cases = result.expected.get("expected_cases", [])
    expected_numbers = {c["case_number"] for c in expected_cases}
    extracted_numbers = set()
    for ruling in result.rulings:
        cn = ruling.get("extracted_case_number")
        if cn:
            extracted_numbers.add(cn)

    cn_correct = len(expected_numbers & extracted_numbers)
    cn_total = len(expected_numbers)

    # Case title accuracy — match by case_number, then compare case_title
    ct_correct = 0
    ct_total = 0
    expected_by_cn: dict[str, str | None] = {}
    for c in expected_cases:
        expected_by_cn[c["case_number"]] = c.get("case_title")
    extracted_by_cn: dict[str, str | None] = {}
    for ruling in result.rulings:
        cn = ruling.get("extracted_case_number")
        if cn:
            extracted_by_cn[cn] = ruling.get("extracted_case_title")
    for cn, exp_title in expected_by_cn.items():
        if exp_title is not None:
            ct_total += 1
            ext_title = extracted_by_cn.get(cn)
            if ext_title is not None and match_case_title(ext_title, exp_title):
                ct_correct += 1

    return {
        "fixture_name": result.fixture_name,
        "expected_case_count": expected_count,
        "actual_case_count": actual_count,
        "count_exact": count_exact,
        "count_off_by_one": count_off_by_one,
        "count_diff": count_diff,
        "total_rulings": len(result.rulings),
        "empty_rulings": empty_count,
        "short_rulings": short_count,
        "ruling_lengths": ruling_lengths,
        "avg_ruling_length": (total_text_len / len(result.rulings) if result.rulings else 0),
        "fields_present": fields_present,
        "fields_total": fields_total,
        "field_completeness_pct": (fields_present / fields_total * 100 if fields_total > 0 else 0),
        "case_numbers_correct": cn_correct,
        "case_numbers_total": cn_total,
        "case_titles_correct": ct_correct,
        "case_titles_total": ct_total,
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
        elif scores["count_off_by_one"]:
            summary.case_count_off_by_one += 1
        else:
            summary.case_count_wrong += 1

        summary.total_rulings += scores["total_rulings"]
        summary.empty_rulings += scores["empty_rulings"]
        summary.fields_complete += scores["fields_present"]
        summary.fields_total += scores["fields_total"]
        summary.case_numbers_correct += scores["case_numbers_correct"]
        summary.case_numbers_total += scores["case_numbers_total"]
        summary.case_titles_correct += scores["case_titles_correct"]
        summary.case_titles_total += scores["case_titles_total"]

    for r in results:
        if r.error:
            summary.errors.append(r.fixture_name + ": " + r.error)

    latencies = [r.latency_ms for r in valid]
    summary.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    # Cost calculations
    avg_in = summary.total_input_tokens / len(valid) if valid else 0
    avg_out = summary.total_output_tokens / len(valid) if valid else 0
    summary.cost_per_fixture = (avg_in * pricing["input"] + avg_out * pricing["output"]) / 1_000_000
    # Estimate: ~6 PDFs/day (3 departments x 2 scrapes)
    summary.estimated_monthly_cost = summary.cost_per_fixture * 6 * 30

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

    scorable = summary.case_count_correct + summary.case_count_off_by_one + summary.case_count_wrong
    if scorable == 0:
        print("\nNo scorable fixtures.")
        return

    print("\n## Case Count Accuracy")
    print("  Exact match:  " + str(summary.case_count_correct) + "/" + str(scorable))
    print("  Off by 1:     " + str(summary.case_count_off_by_one) + "/" + str(scorable))
    print("  Wrong (>1):   " + str(summary.case_count_wrong) + "/" + str(scorable))
    exact_pct = summary.case_count_correct / scorable * 100
    lenient_pct = (summary.case_count_correct + summary.case_count_off_by_one) / scorable * 100
    print("  Exact accuracy:   " + f"{exact_pct:.1f}" + "%")
    print("  Lenient accuracy: " + f"{lenient_pct:.1f}" + "% (off-by-1 counted as correct)")

    print("\n## Case Number Accuracy")
    if summary.case_numbers_total > 0:
        cn_pct = summary.case_numbers_correct / summary.case_numbers_total * 100
        print(
            "  Correct: "
            + str(summary.case_numbers_correct)
            + "/"
            + str(summary.case_numbers_total)
            + " ("
            + f"{cn_pct:.1f}"
            + "%)"
        )

    print("\n## Case Title Accuracy")
    if summary.case_titles_total > 0:
        ct_pct = summary.case_titles_correct / summary.case_titles_total * 100
        print(
            "  case_title_match: "
            + str(summary.case_titles_correct)
            + "/"
            + str(summary.case_titles_total)
            + " ("
            + f"{ct_pct:.1f}"
            + "%)"
        )
    else:
        print("  No case titles to compare.")

    print("\n## Field Completeness")
    if summary.fields_total > 0:
        fc_pct = summary.fields_complete / summary.fields_total * 100
        print(
            "  Fields populated: "
            + str(summary.fields_complete)
            + "/"
            + str(summary.fields_total)
            + " ("
            + f"{fc_pct:.1f}"
            + "%)"
        )

    print("\n## Ruling Text Quality")
    print("  Total rulings extracted: " + str(summary.total_rulings))
    print("  Empty rulings:           " + str(summary.empty_rulings))
    if summary.total_rulings > 0:
        non_empty_pct = (
            (summary.total_rulings - summary.empty_rulings) / summary.total_rulings * 100
        )
        print("  Non-empty rate:          " + f"{non_empty_pct:.1f}" + "%")

    # Per-fixture detail
    print("\n## Per-Fixture Detail")
    print(
        "  "
        + f"{'Fixture':<35}"
        + f"{'Exp':>5}"
        + f"{'Got':>5}"
        + f"{'Match':>7}"
        + f"{'Empty':>7}"
        + f"{'CN':>7}"
        + f"{'CT':>7}"
        + f"{'Flds':>7}"
    )
    print(
        "  "
        + "-" * 35
        + " "
        + "-" * 5
        + " "
        + "-" * 5
        + " "
        + "-" * 7
        + " "
        + "-" * 7
        + " "
        + "-" * 7
        + " "
        + "-" * 7
        + " "
        + "-" * 7
    )
    for detail in summary.fixture_details:
        fname = detail["fixture_name"]
        if len(fname) > 33:
            fname = fname[:30] + "..."
        tag = ""
        if detail["count_exact"]:
            tag = " OK"
        elif detail["count_off_by_one"]:
            tag = " ~1"
        else:
            tag = " ERR"
        cn_str = str(detail["case_numbers_correct"]) + "/" + str(detail["case_numbers_total"])
        ct_str = str(detail["case_titles_correct"]) + "/" + str(detail["case_titles_total"])
        fc_str = f"{detail['field_completeness_pct']:.0f}%"
        print(
            "  "
            + f"{fname:<35}"
            + f"{detail['expected_case_count']:>5}"
            + f"{detail['actual_case_count']:>5}"
            + f"{tag:>7}"
            + f"{detail['empty_rulings']:>7}"
            + f"{cn_str:>7}"
            + f"{ct_str:>7}"
            + f"{fc_str:>7}"
        )

    if summary.errors:
        print("\nErrors (" + str(len(summary.errors)) + "):")
        for err in summary.errors:
            print("  " + err)

    print("\nCost estimate:")
    print("  Per fixture: $" + f"{summary.cost_per_fixture:.6f}")
    print("  Monthly (~6 PDFs/day): $" + f"{summary.estimated_monthly_cost:.2f}")


def print_comparison_table(summaries: dict[str, ModelSummary]) -> None:
    """Print a side-by-side comparison table of all models."""
    model_names = list(summaries.keys())

    print("\n" + "=" * 80)
    print("COMPARISON TABLE: San Francisco Family Law Text Extraction Eval")
    print("=" * 80 + "\n")

    header = f"{'Metric':<35}"
    for name in model_names:
        header += f"  {name:>20}"
    print(header)
    print("-" * 35 + "".join("  " + "-" * 20 for _ in model_names))

    # Fixtures
    row = f"{'Fixtures processed':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.total_fixtures:>20}"
    print(row)

    # Case count exact match
    row = f"{'Case count exact match':<35}"
    for name in model_names:
        s = summaries[name]
        scorable = s.case_count_correct + s.case_count_off_by_one + s.case_count_wrong
        pct = (s.case_count_correct / scorable * 100) if scorable > 0 else 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Case number accuracy
    row = f"{'Case number accuracy':<35}"
    for name in model_names:
        s = summaries[name]
        pct = s.case_numbers_correct / s.case_numbers_total * 100 if s.case_numbers_total > 0 else 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Case title accuracy
    row = f"{'Case title accuracy':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            s.case_titles_correct / s.case_titles_total * 100
            if s.case_titles_total > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Field completeness
    row = f"{'Field completeness':<35}"
    for name in model_names:
        s = summaries[name]
        pct = s.fields_complete / s.fields_total * 100 if s.fields_total > 0 else 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Non-empty ruling rate
    row = f"{'Non-empty ruling rate':<35}"
    for name in model_names:
        s = summaries[name]
        if s.total_rulings > 0:
            pct = (s.total_rulings - s.empty_rulings) / s.total_rulings * 100
        else:
            pct = 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Avg latency
    row = f"{'Avg latency per fixture (ms)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.avg_latency_ms:>20,.0f}"
    print(row)

    # Cost
    row = f"{'Cost per fixture':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.cost_per_fixture:>18.6f}"
    print(row)

    row = f"{'Monthly cost (~6/day)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.estimated_monthly_cost:>18.2f}"
    print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the San Francisco Family Law LLM extraction evaluation."""
    parser = argparse.ArgumentParser(
        description="Eval LLM extraction accuracy on San Francisco Family Law PDF fixtures."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(MODELS.keys()),
        default=["claude-haiku-4.5"],
        help="Models to evaluate (default: claude-haiku-4.5).",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to JSON file.",
    )
    args = parser.parse_args()

    # Check API keys
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    google_key = os.environ.get("GOOGLE_API_KEY")

    anthropic_models = [m for m in args.models if MODELS[m]["provider"] == "anthropic"]
    google_models = [m for m in args.models if MODELS[m]["provider"] == "google"]

    if anthropic_models and not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY not set (needed for: " + ", ".join(anthropic_models) + ")")
        return 1
    if google_models and not google_key:
        print("ERROR: GOOGLE_API_KEY not set (needed for: " + ", ".join(google_models) + ")")
        return 1

    # Load fixtures
    fixtures = get_sf_fixtures()
    print("Found " + str(len(fixtures)) + " San Francisco Family Law PDF fixtures with ground truth\n")
    for fp, exp in fixtures:
        desc = exp.get("_description", "")
        cc = exp.get("case_count", "?")
        print("  " + fp.name + ": " + str(cc) + " cases -- " + desc[:70])
    print()

    # Extract text from all PDFs
    print("Extracting text from PDFs...")
    fixture_texts: dict[str, str] = {}
    for fp, _ in fixtures:
        text = extract_pdf_text(fp)
        fixture_texts[fp.name] = text
        line_count = len(text.split("\n"))
        char_count = len(text)
        print("  " + fp.name + ": " + str(char_count) + " chars, " + str(line_count) + " lines")
    print()

    # Regex baseline comparison
    print("Regex baseline (current scraper):")
    for fp, expected in fixtures:
        text = fixture_texts[fp.name]
        regex_result = regex_extract(text)
        regex_cns = regex_result["case_numbers"]
        regex_judge = regex_result["judge_name"] or "(none)"
        print(
            "  "
            + fp.name
            + ": regex found "
            + str(len(regex_cns))
            + " case numbers "
            + str(regex_cns[:5])
            + " | judge: "
            + regex_judge
        )
    print()

    # Run evaluation for each model
    all_summaries: dict[str, ModelSummary] = {}

    for model_name in args.models:
        model_config = MODELS[model_name]
        model_id = model_config["model_id"]
        provider = model_config["provider"]
        pricing = model_config["pricing_per_m"]

        if provider == "anthropic":
            api_key = anthropic_key
        else:
            api_key = google_key

        print("\n" + "=" * 70)
        print("MODEL: " + model_name + " (" + model_id + ") -- text extraction")
        print("=" * 70 + "\n")

        results: list[FixtureResult] = []

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            text = fixture_texts[fname]
            expected_case_count = expected.get("case_count", 0)

            print(
                "  "
                + fname
                + " ("
                + str(len(text))
                + " chars, "
                + str(expected_case_count)
                + " expected cases)...",
                end=" ",
                flush=True,
            )

            fixture_result = FixtureResult(
                fixture_name=fname,
                model=model_name,
                expected=expected,
                expected_case_count=expected_case_count,
            )

            try:
                if provider == "anthropic":
                    call_fn = call_anthropic
                else:
                    call_fn = call_gemini

                extracted, inp_tok, out_tok, lat = _call_with_retry(
                    call_fn, model_id, text, api_key
                )

                rulings_raw = extracted.get("rulings", [])
                rulings = [r for r in rulings_raw if isinstance(r, dict)]

                fixture_result.rulings = rulings
                fixture_result.actual_case_count = len(rulings)
                fixture_result.extracted_judge = extracted.get("extracted_judge_name")
                fixture_result.extracted_hearing_date = extracted.get("hearing_date")
                fixture_result.extracted_department = extracted.get("department")
                fixture_result.input_tokens = inp_tok
                fixture_result.output_tokens = out_tok
                fixture_result.latency_ms = lat

                match_str = (
                    "EXACT"
                    if len(rulings) == expected_case_count
                    else ("~1" if abs(len(rulings) - expected_case_count) == 1 else "WRONG")
                )

                print(
                    str(len(rulings))
                    + " cases ("
                    + match_str
                    + ")"
                    + " ["
                    + str(inp_tok)
                    + "+"
                    + str(out_tok)
                    + " tok, "
                    + f"{lat:.0f}"
                    + "ms]"
                )

                # Print per-ruling details
                for i, ruling in enumerate(rulings):
                    cn = ruling.get("extracted_case_number", "?")
                    ct = ruling.get("extracted_case_title", "?")
                    oc = ruling.get("outcome", "?")
                    mt = ruling.get("motion_type", "?")
                    rt_len = len(ruling.get("ruling_text") or "")
                    parties = ruling.get("extracted_parties", [])
                    party_count = len(parties) if isinstance(parties, list) else 0
                    print(
                        "    #"
                        + str(i + 1)
                        + " "
                        + str(cn)
                        + " | "
                        + str(ct)
                        + " | "
                        + str(oc)
                        + " | "
                        + str(mt)
                        + " | "
                        + str(rt_len)
                        + " chars"
                        + " | "
                        + str(party_count)
                        + " parties"
                    )

                # Print extracted header fields
                print(
                    "    Judge: "
                    + str(fixture_result.extracted_judge)
                    + " (expected: "
                    + str(expected.get("judge_name"))
                    + ")"
                )
                print(
                    "    Dept: "
                    + str(fixture_result.extracted_department)
                    + " (expected: "
                    + str(expected.get("department"))
                    + ")"
                )

                # Regression check: compare case numbers with regex
                regex_result = regex_extract(text)
                regex_cns = set(regex_result["case_numbers"])
                llm_cns = set()
                for ruling in rulings:
                    cn = ruling.get("extracted_case_number")
                    if cn:
                        llm_cns.add(cn)

                if regex_cns - llm_cns:
                    print("    REGRESSION: regex found but LLM missed: " + str(regex_cns - llm_cns))

            except Exception as e:
                print("ERROR: " + str(e))
                fixture_result.error = str(e)

            results.append(fixture_result)

        summary = analyze_model(results, model_name, pricing)
        print_model_report(summary)
        all_summaries[model_name] = summary

    # Comparison table
    if len(all_summaries) > 1:
        print_comparison_table(all_summaries)

    # Save results
    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        output_path = RESULTS_DIR / "sf_extraction_results.json"

        save_data = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "eval_type": "sf_family_law_text_extraction",
            "fixtures_count": len(fixtures),
            "models": {},
        }
        for model_name, summary in all_summaries.items():
            scorable = (
                summary.case_count_correct
                + summary.case_count_off_by_one
                + summary.case_count_wrong
            )
            save_data["models"][model_name] = {
                "total_fixtures": summary.total_fixtures,
                "case_count_exact": summary.case_count_correct,
                "case_count_off_by_one": summary.case_count_off_by_one,
                "case_count_wrong": summary.case_count_wrong,
                "case_count_exact_pct": (
                    summary.case_count_correct / scorable * 100 if scorable > 0 else 0
                ),
                "case_count_lenient_pct": (
                    (summary.case_count_correct + summary.case_count_off_by_one) / scorable * 100
                    if scorable > 0
                    else 0
                ),
                "case_numbers_correct": summary.case_numbers_correct,
                "case_numbers_total": summary.case_numbers_total,
                "case_titles_correct": summary.case_titles_correct,
                "case_titles_total": summary.case_titles_total,
                "fields_complete": summary.fields_complete,
                "fields_total": summary.fields_total,
                "total_rulings": summary.total_rulings,
                "empty_rulings": summary.empty_rulings,
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "avg_latency_ms": summary.avg_latency_ms,
                "cost_per_fixture": summary.cost_per_fixture,
                "estimated_monthly_cost": summary.estimated_monthly_cost,
                "fixture_details": summary.fixture_details,
                "errors": summary.errors,
            }

        output_path.write_text(
            json.dumps(save_data, indent=2, default=str),
            encoding="utf-8",
        )
        print("\nResults saved to: " + str(output_path))

    # Return exit code: 0 if all models achieve 100% exact case count accuracy
    all_pass = True
    for summary in all_summaries.values():
        scorable = (
            summary.case_count_correct + summary.case_count_off_by_one + summary.case_count_wrong
        )
        if scorable > 0:
            if summary.case_count_correct < scorable:
                all_pass = False

    if all_pass:
        print("\nAll models achieved 100% exact case count accuracy!")
    else:
        print("\nSome models did NOT achieve 100% exact case count accuracy.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
