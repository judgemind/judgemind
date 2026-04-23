#!/usr/bin/env python3
"""Eval LLM-based extraction on Ventura County ruling document fixtures.

Ventura ruling documents are **single-case** (not multi-case like Riverside or
Fresno).  Documents can be HTML or PDF format.  The scraper already provides
case_number, motion_type, hearing_date, and department from the search results
table.  The LLM's job is to extract the remaining fields:

  1. outcome (granted, denied, granted_in_part, etc.)
  2. parties (plaintiff/defendant or petitioner/respondent with roles)
  3. ruling_text (trimmed to ruling content, excluding boilerplate)

The LLM receives the document text plus known metadata (case_number, motion_type)
as context to help it focus on the right fields.

This eval validates:
  - outcome accuracy: does the LLM extract the correct outcome?
  - parties extraction: are party names and roles correct?
  - ruling_text quality: is it non-empty and trimmed (shorter than raw document)?
  - field completeness: are all required fields non-null?
  - cost estimate: token counts and cost per document

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    export GOOGLE_API_KEY="AIza..."

    # Run default model (Claude Haiku 4.5)
    python3 scripts/eval/eval_ventura_extraction.py

    # Run specific model(s)
    python3 scripts/eval/eval_ventura_extraction.py --models claude-haiku-4.5

    # Run all models
    python3 scripts/eval/eval_ventura_extraction.py --models claude-haiku-4.5 gemini-2.5-flash-lite gemini-2.5-flash

    # Save results to JSON
    python3 scripts/eval/eval_ventura_extraction.py --save

Requirements:
    pip install anthropic google-genai pdfplumber beautifulsoup4 lxml
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
# Ventura-specific extraction prompt
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
    "- 'et al.' indicates additional parties — extract the named party "
    "and note 'et al.' in the name if present\n"
    "- Use the FULL party name from the document (company names, etc.)\n\n"
    "## Outcome Taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted — motion/petition was fully granted\n"
    "- denied — motion was fully denied (includes 'overruled' for demurrers)\n"
    "- granted_in_part — some parts granted, some denied (includes "
    "'sustained in part' for demurrers, or mixed rulings on multiple "
    "issues)\n"
    "- denied_in_part — partially denied\n"
    "- moot — motion is moot\n"
    "- continued — hearing was postponed/continued to a future date\n"
    "- off_calendar — hearing removed from calendar\n"
    "- submitted — taken under submission\n"
    "- other — none of the above fit\n\n"
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
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FixtureResult:
    """Result from processing a single fixture."""

    fixture_name: str
    model: str
    expected: dict
    raw_text_length: int = 0
    extracted_outcome: str | None = None
    expected_outcome: str | None = None
    outcome_match: bool = False
    extracted_parties: list[dict] = field(default_factory=list)
    expected_parties: list[dict] = field(default_factory=list)
    parties_match: bool = False
    extracted_case_title: str | None = None
    expected_case_title: str | None = None
    ruling_text_length: int = 0
    ruling_text_trimmed: bool = False
    ruling_text_contains_expected: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class ModelSummary:
    """Aggregated results for a single model."""

    model: str
    total_fixtures: int = 0
    outcome_correct: int = 0
    outcome_wrong: int = 0
    outcome_null: int = 0
    parties_correct: int = 0
    parties_partial: int = 0
    parties_wrong: int = 0
    ruling_text_non_empty: int = 0
    ruling_text_trimmed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_per_doc: float = 0.0
    estimated_monthly_cost: float = 0.0
    fixture_details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_html_text(html_path: Path) -> str:
    """Extract text from an HTML ruling document."""
    from bs4 import BeautifulSoup

    html_bytes = html_path.read_bytes()
    try:
        html_str = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        html_str = html_bytes.decode("latin-1")

    soup = BeautifulSoup(html_str, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF ruling document."""
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------


MAX_RETRIES = 3
CALL_TIMEOUT_S = 30


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
                raise TimeoutError(
                    "Timed out after " + str(max_retries) + " retries"
                )
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


def _build_user_message(
    text: str,
    case_number: str,
    motion_type: str,
) -> str:
    """Build the user message with known context and document text."""
    return (
        "Known metadata from the court's search results table:\n"
        "- Case number: " + case_number + "\n"
        "- Motion type: " + motion_type + "\n\n"
        "Document text:\n\n" + text
    )


def call_gemini(
    model_id: str,
    user_message: str,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Gemini API with text input.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        system_instruction=VENTURA_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=8192,
        response_mime_type="application/json",
    )

    start = time.monotonic()
    response = client.models.generate_content(
        model=model_id,
        contents=user_message,
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
        lines = [
            line for line in lines if not line.strip().startswith("```")
        ]
        response_text = "\n".join(lines)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        start_idx = response_text.find("{")
        end_idx = response_text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            result = json.loads(response_text[start_idx:end_idx])
        else:
            result = {"_parse_error": response_text[:200]}

    return result, input_tokens, output_tokens, latency


def call_anthropic(
    model_id: str,
    user_message: str,
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
        max_tokens=8192,
        temperature=0,
        system=VENTURA_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
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

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        start_idx = response_text.find("{")
        end_idx = response_text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            result = json.loads(response_text[start_idx:end_idx])
        else:
            result = {"_parse_error": response_text[:200]}

    return result, input_tokens, output_tokens, latency


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def get_ventura_fixtures() -> list[tuple[Path, dict]]:
    """Load all Ventura ruling document fixtures with expected ground truth."""
    fixtures = []
    for expected_file in sorted(EXPECTED_DIR.glob("ventura_*.json")):
        expected = json.loads(expected_file.read_text())
        fixture_name = expected.get("_fixture")
        if not fixture_name:
            continue
        fixture_path = FIXTURES_DIR / fixture_name
        if not fixture_path.exists():
            continue
        fixtures.append((fixture_path, expected))
    return fixtures


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a party name for comparison."""
    return re.sub(r"\s+", " ", name.strip().lower())


def score_parties(
    extracted: list[dict],
    expected: list[dict],
) -> tuple[str, str]:
    """Score party extraction.

    Returns: (match_level, detail)
      match_level: "exact", "partial", "wrong"
      detail: human-readable explanation
    """
    if not expected:
        if not extracted:
            return "exact", "no parties expected or extracted"
        return "partial", "extracted parties but none expected"

    if not extracted:
        return "wrong", "no parties extracted"

    # Normalize and compare
    expected_names = {_normalize_name(p["name"]) for p in expected}
    extracted_names = {_normalize_name(p["name"]) for p in extracted}

    name_overlap = expected_names & extracted_names
    if len(name_overlap) == len(expected_names):
        # Check roles
        expected_roles = {
            _normalize_name(p["name"]): p.get("role", "")
            for p in expected
        }
        extracted_roles = {
            _normalize_name(p["name"]): p.get("role", "")
            for p in extracted
        }
        role_matches = sum(
            1
            for name in name_overlap
            if expected_roles.get(name) == extracted_roles.get(name)
        )
        if role_matches == len(expected_names):
            return "exact", "all names and roles match"
        return (
            "partial",
            "names match but "
            + str(len(expected_names) - role_matches)
            + " role(s) differ",
        )

    if name_overlap:
        return (
            "partial",
            str(len(name_overlap))
            + "/"
            + str(len(expected_names))
            + " names matched",
        )

    # Try fuzzy: check if expected names are substrings of extracted
    fuzzy_matches = 0
    for exp_name in expected_names:
        for ext_name in extracted_names:
            if exp_name in ext_name or ext_name in exp_name:
                fuzzy_matches += 1
                break

    if fuzzy_matches > 0:
        return (
            "partial",
            str(fuzzy_matches)
            + "/"
            + str(len(expected_names))
            + " fuzzy name matches",
        )

    return (
        "wrong",
        "no name matches (expected: "
        + str(expected_names)
        + ", got: "
        + str(extracted_names)
        + ")",
    )


def score_fixture(result: FixtureResult) -> dict:
    """Score a single fixture result."""
    # Outcome
    outcome_match = False
    if result.extracted_outcome and result.expected_outcome:
        outcome_match = (
            result.extracted_outcome.lower() == result.expected_outcome.lower()
        )
    elif result.extracted_outcome is None and result.expected_outcome is None:
        outcome_match = True

    # Parties
    party_level, party_detail = score_parties(
        result.extracted_parties, result.expected_parties
    )

    # Ruling text
    ruling_text_non_empty = result.ruling_text_length > 0
    ruling_text_trimmed = (
        result.ruling_text_length < result.raw_text_length * 0.95
        if result.raw_text_length > 0
        else False
    )

    return {
        "fixture_name": result.fixture_name,
        "outcome_match": outcome_match,
        "expected_outcome": result.expected_outcome,
        "extracted_outcome": result.extracted_outcome,
        "party_level": party_level,
        "party_detail": party_detail,
        "extracted_case_title": result.extracted_case_title,
        "expected_case_title": result.expected_case_title,
        "ruling_text_length": result.ruling_text_length,
        "raw_text_length": result.raw_text_length,
        "ruling_text_non_empty": ruling_text_non_empty,
        "ruling_text_trimmed": ruling_text_trimmed,
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

        if scores["outcome_match"]:
            summary.outcome_correct += 1
        elif scores["extracted_outcome"] is None:
            summary.outcome_null += 1
        else:
            summary.outcome_wrong += 1

        if scores["party_level"] == "exact":
            summary.parties_correct += 1
        elif scores["party_level"] == "partial":
            summary.parties_partial += 1
        else:
            summary.parties_wrong += 1

        if scores["ruling_text_non_empty"]:
            summary.ruling_text_non_empty += 1
        if scores["ruling_text_trimmed"]:
            summary.ruling_text_trimmed += 1

    for r in results:
        if r.error:
            summary.errors.append(r.fixture_name + ": " + r.error)

    latencies = [r.latency_ms for r in valid]
    summary.avg_latency_ms = (
        sum(latencies) / len(latencies) if latencies else 0.0
    )

    # Cost calculations
    avg_in = summary.total_input_tokens / len(valid) if valid else 0
    avg_out = summary.total_output_tokens / len(valid) if valid else 0
    summary.cost_per_doc = (
        avg_in * pricing["input"] + avg_out * pricing["output"]
    ) / 1_000_000
    # Estimate: ~12 documents/day (6 cases x 2 scrapes/day)
    summary.estimated_monthly_cost = summary.cost_per_doc * 12 * 30

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
    print(
        "Avg latency per doc: "
        + f"{summary.avg_latency_ms:,.0f}"
        + "ms"
    )

    total = summary.total_fixtures
    if total == 0:
        print("\nNo scorable fixtures.")
        return

    print("\n## Outcome Accuracy")
    print(
        "  Correct:  "
        + str(summary.outcome_correct)
        + "/"
        + str(total)
        + " ("
        + f"{summary.outcome_correct / total * 100:.0f}"
        + "%)"
    )
    print(
        "  Wrong:    "
        + str(summary.outcome_wrong)
        + "/"
        + str(total)
    )
    print(
        "  Null:     "
        + str(summary.outcome_null)
        + "/"
        + str(total)
    )

    print("\n## Party Extraction")
    print(
        "  Exact:    "
        + str(summary.parties_correct)
        + "/"
        + str(total)
        + " ("
        + f"{summary.parties_correct / total * 100:.0f}"
        + "%)"
    )
    print(
        "  Partial:  "
        + str(summary.parties_partial)
        + "/"
        + str(total)
    )
    print(
        "  Wrong:    "
        + str(summary.parties_wrong)
        + "/"
        + str(total)
    )

    print("\n## Ruling Text Quality")
    print(
        "  Non-empty: "
        + str(summary.ruling_text_non_empty)
        + "/"
        + str(total)
        + " ("
        + f"{summary.ruling_text_non_empty / total * 100:.0f}"
        + "%)"
    )
    print(
        "  Trimmed:   "
        + str(summary.ruling_text_trimmed)
        + "/"
        + str(total)
        + " ("
        + f"{summary.ruling_text_trimmed / total * 100:.0f}"
        + "%)"
    )

    # Per-fixture detail
    print("\n## Per-Fixture Detail")
    print(
        "  "
        + f"{'Fixture':<35}"
        + f"{'Outcome':>12}"
        + f"{'Match':>7}"
        + f"{'Parties':>10}"
        + f"{'RulingLen':>10}"
    )
    print(
        "  "
        + "-" * 35
        + " "
        + "-" * 12
        + " "
        + "-" * 7
        + " "
        + "-" * 10
        + " "
        + "-" * 10
    )
    for detail in summary.fixture_details:
        fname = detail["fixture_name"]
        if len(fname) > 33:
            fname = fname[:30] + "..."
        outcome_str = str(detail["extracted_outcome"] or "null")
        if len(outcome_str) > 10:
            outcome_str = outcome_str[:10] + ".."
        match_tag = " OK" if detail["outcome_match"] else " MISS"
        party_tag = detail["party_level"]
        rt_len = str(detail["ruling_text_length"])
        print(
            "  "
            + f"{fname:<35}"
            + f"{outcome_str:>12}"
            + f"{match_tag:>7}"
            + f"{party_tag:>10}"
            + f"{rt_len:>10}"
        )

    if summary.errors:
        print("\nErrors (" + str(len(summary.errors)) + "):")
        for err in summary.errors:
            print("  " + err)

    print("\nCost estimate:")
    print("  Per document: $" + f"{summary.cost_per_doc:.6f}")
    print(
        "  Monthly (~12 docs/day): $"
        + f"{summary.estimated_monthly_cost:.2f}"
    )


def print_comparison_table(summaries: dict[str, ModelSummary]) -> None:
    """Print a side-by-side comparison table of all models."""
    model_names = list(summaries.keys())

    print("\n" + "=" * 80)
    print("COMPARISON TABLE: Ventura Single-Case Extraction Eval")
    print("=" * 80 + "\n")

    header = f"{'Metric':<35}"
    for name in model_names:
        header += f"  {name:>20}"
    print(header)
    print(
        "-" * 35
        + "".join("  " + "-" * 20 for _ in model_names)
    )

    # Fixtures
    row = f"{'Fixtures processed':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.total_fixtures:>20}"
    print(row)

    # Outcome accuracy
    row = f"{'Outcome accuracy':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            s.outcome_correct / s.total_fixtures * 100
            if s.total_fixtures > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Party exact match
    row = f"{'Party exact match':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            s.parties_correct / s.total_fixtures * 100
            if s.total_fixtures > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Party partial+exact
    row = f"{'Party partial+ match':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            (s.parties_correct + s.parties_partial)
            / s.total_fixtures
            * 100
            if s.total_fixtures > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Non-empty ruling rate
    row = f"{'Ruling text non-empty':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            s.ruling_text_non_empty / s.total_fixtures * 100
            if s.total_fixtures > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Trimmed ruling rate
    row = f"{'Ruling text trimmed':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            s.ruling_text_trimmed / s.total_fixtures * 100
            if s.total_fixtures > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Avg latency
    row = f"{'Avg latency per doc (ms)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.avg_latency_ms:>20,.0f}"
    print(row)

    # Cost
    row = f"{'Cost per document':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.cost_per_doc:>18.6f}"
    print(row)

    row = f"{'Monthly cost (~12/day)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.estimated_monthly_cost:>18.2f}"
    print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the Ventura LLM extraction evaluation."""
    parser = argparse.ArgumentParser(
        description="Eval LLM extraction on Ventura ruling document fixtures."
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
    google_key = os.environ.get("GOOGLE_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    google_models = [
        m for m in args.models if MODELS[m]["provider"] == "google"
    ]
    anthropic_models = [
        m for m in args.models if MODELS[m]["provider"] == "anthropic"
    ]

    if google_models and not google_key:
        print(
            "ERROR: GOOGLE_API_KEY not set (needed for: "
            + ", ".join(google_models)
            + ")"
        )
        return 1
    if anthropic_models and not anthropic_key:
        print(
            "ERROR: ANTHROPIC_API_KEY not set (needed for: "
            + ", ".join(anthropic_models)
            + ")"
        )
        return 1

    # Load fixtures
    fixtures = get_ventura_fixtures()
    print(
        "Found "
        + str(len(fixtures))
        + " Ventura ruling document fixtures with ground truth\n"
    )
    for fp, exp in fixtures:
        fmt = exp.get("_format", "unknown")
        motion = exp.get("motion_type", "?")
        print("  " + fp.name + " (" + fmt + "): " + motion)
    print()

    if not fixtures:
        print("ERROR: No Ventura fixtures found.")
        print(
            "Expected ventura_*.json files in "
            + str(EXPECTED_DIR)
        )
        return 1

    # Extract text from all fixtures
    print("Extracting text from fixtures...")
    fixture_texts: dict[str, str] = {}
    for fp, exp in fixtures:
        fmt = exp.get("_format", "html")
        if fmt == "pdf":
            text = extract_pdf_text(fp)
        else:
            text = extract_html_text(fp)
        fixture_texts[fp.name] = text
        char_count = len(text)
        line_count = len(text.split("\n"))
        print(
            "  "
            + fp.name
            + ": "
            + str(char_count)
            + " chars, "
            + str(line_count)
            + " lines"
        )
    print()

    # Run evaluation for each model
    all_summaries: dict[str, ModelSummary] = {}

    for model_name in args.models:
        model_config = MODELS[model_name]
        provider = model_config["provider"]
        model_id = model_config["model_id"]
        pricing = model_config["pricing_per_m"]

        if provider == "google":
            api_key = google_key
        else:
            api_key = anthropic_key

        print("\n" + "=" * 70)
        print(
            "MODEL: "
            + model_name
            + " ("
            + model_id
            + ") -- single-case extraction"
        )
        print("=" * 70 + "\n")

        results: list[FixtureResult] = []

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            text = fixture_texts[fname]
            case_number = expected.get("case_number", "")
            motion_type = expected.get("motion_type", "")

            expected_cases = expected.get("expected_cases", [])
            exp_case = expected_cases[0] if expected_cases else {}
            expected_outcome = exp_case.get("outcome")
            expected_parties = exp_case.get("parties", [])
            expected_case_title = exp_case.get("case_title")

            print(
                "  "
                + fname
                + " ("
                + str(len(text))
                + " chars, "
                + motion_type
                + ")...",
                end=" ",
                flush=True,
            )

            fixture_result = FixtureResult(
                fixture_name=fname,
                model=model_name,
                expected=expected,
                raw_text_length=len(text),
                expected_outcome=expected_outcome,
                expected_parties=expected_parties,
                expected_case_title=expected_case_title,
            )

            try:
                user_message = _build_user_message(
                    text, case_number, motion_type
                )

                if provider == "google":
                    call_fn = call_gemini
                else:
                    call_fn = call_anthropic
                extracted, inp_tok, out_tok, lat = _call_with_retry(
                    call_fn, model_id, user_message, api_key
                )

                fixture_result.input_tokens = inp_tok
                fixture_result.output_tokens = out_tok
                fixture_result.latency_ms = lat

                # Extract fields
                fixture_result.extracted_outcome = extracted.get("outcome")
                fixture_result.extracted_parties = extracted.get(
                    "parties", []
                )
                fixture_result.extracted_case_title = extracted.get(
                    "case_title"
                )

                ruling_text = extracted.get("ruling_text") or ""
                fixture_result.ruling_text_length = len(ruling_text)

                # Check outcome
                fixture_result.outcome_match = (
                    fixture_result.extracted_outcome is not None
                    and expected_outcome is not None
                    and fixture_result.extracted_outcome.lower()
                    == expected_outcome.lower()
                )

                # Check ruling text contains expected strings
                if expected_cases and "ruling_text_contains" in exp_case:
                    all_present = all(
                        s.lower() in ruling_text.lower()
                        for s in exp_case["ruling_text_contains"]
                    )
                    fixture_result.ruling_text_contains_expected = all_present

                # Check text trimming
                fixture_result.ruling_text_trimmed = (
                    0 < len(ruling_text) < len(text) * 0.95
                )

                outcome_tag = (
                    "OK"
                    if fixture_result.outcome_match
                    else "MISS"
                )
                party_count = len(fixture_result.extracted_parties)

                print(
                    "outcome="
                    + str(fixture_result.extracted_outcome)
                    + " ("
                    + outcome_tag
                    + "), "
                    + str(party_count)
                    + " parties, "
                    + str(len(ruling_text))
                    + " chars"
                    + " ["
                    + str(inp_tok)
                    + "+"
                    + str(out_tok)
                    + " tok, "
                    + f"{lat:.0f}"
                    + "ms]"
                )

                # Print party details
                for p in fixture_result.extracted_parties:
                    pname = p.get("name", "?")
                    prole = p.get("role", "?")
                    print("    -> " + pname + " (" + prole + ")")

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
        output_path = RESULTS_DIR / "ventura_extraction_results.json"

        save_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "eval_type": "ventura_single_case_extraction",
            "fixtures_count": len(fixtures),
            "models": {},
        }
        for model_name, summary in all_summaries.items():
            save_data["models"][model_name] = {
                "total_fixtures": summary.total_fixtures,
                "outcome_correct": summary.outcome_correct,
                "outcome_wrong": summary.outcome_wrong,
                "outcome_null": summary.outcome_null,
                "outcome_accuracy_pct": (
                    summary.outcome_correct / summary.total_fixtures * 100
                    if summary.total_fixtures > 0
                    else 0
                ),
                "parties_exact": summary.parties_correct,
                "parties_partial": summary.parties_partial,
                "parties_wrong": summary.parties_wrong,
                "parties_exact_pct": (
                    summary.parties_correct / summary.total_fixtures * 100
                    if summary.total_fixtures > 0
                    else 0
                ),
                "ruling_text_non_empty": summary.ruling_text_non_empty,
                "ruling_text_trimmed": summary.ruling_text_trimmed,
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "avg_latency_ms": summary.avg_latency_ms,
                "cost_per_doc": summary.cost_per_doc,
                "monthly_cost_estimate": summary.estimated_monthly_cost,
                "fixture_details": summary.fixture_details,
                "errors": summary.errors,
            }

        output_path.write_text(json.dumps(save_data, indent=2))
        print("\nResults saved to " + str(output_path))

    # Return non-zero if any model had errors
    for summary in all_summaries.values():
        if summary.errors:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
