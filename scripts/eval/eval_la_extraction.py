#!/usr/bin/env python3
"""Eval LLM-based extraction on LA County HTML fixtures.

LA County tentative rulings are **HTML** (not PDF like OC/Riverside), so the
LLM receives cleaned text extracted from HTML via BeautifulSoup.  The LLM's
job:

  1. Split multi-case department pages into individual rulings
  2. Extract structured fields for each case (case_number, case_title, etc.)
  3. Handle cases where the regex parser currently produces null titles
  4. Extract clean party names (not court headers or ruling text)

This eval validates:
  - case_count: does the LLM find the right number of cases per document?
  - case_title: does the LLM extract titles where regex returns null?
  - parties: are party names clean (no court headers)?
  - outcome: does the LLM correctly classify the ruling outcome?
  - ruling_text: is it non-empty and a reasonable representation?

Known-bad patterns the LLM must fix:
  - 74 null case titles (3.1% of rulings)
  - Party names containing court headers
  - Unsplit multi-case calendar pages (>20k chars)

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    export GOOGLE_API_KEY="AIza..."

    # Run all models (default)
    python3 scripts/eval/eval_la_extraction.py

    # Run specific model(s)
    python3 scripts/eval/eval_la_extraction.py --models claude-haiku-4.5

    # Save results to JSON
    python3 scripts/eval/eval_la_extraction.py --save
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
# LA-specific extraction prompt
# ---------------------------------------------------------------------------

LA_EXTRACTION_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Los Angeles County Superior Court.\n\n"
    "You will receive the cleaned text content from an HTML page "
    "containing tentative rulings for one department.  Your job is to "
    "identify EVERY individual case ruling and extract structured data "
    "for each.\n\n"
    "## LA Document Format\n\n"
    "LA tentative rulings are published as HTML pages with this structure:\n"
    "1. **Department header**: 'DEPARTMENT [X] LAW AND MOTION RULINGS' "
    "followed by boilerplate instructions about submitting on the "
    "tentative.\n"
    "2. **Case sections**: Each case starts with:\n"
    "   - 'Case Number: [number]' (e.g., 24NNCV02551, 23CMCV01566)\n"
    "   - 'Hearing Date: [date]' (e.g., March 2, 2026)\n"
    "   - 'Dept: [dept]' (e.g., 3, P, F46, 205)\n"
    "3. **Party caption block**: A formal caption with:\n"
    "   - Plaintiff/Petitioner name(s)\n"
    "   - 'Plaintiff(s),' or 'Petitioner(s),'\n"
    "   - 'vs.'\n"
    "   - Defendant/Respondent name(s)\n"
    "   - 'Defendant(s).' or 'Respondent(s).'\n"
    "   OR alternatively:\n"
    "   - 'MOVING PARTY: [name]'\n"
    "   - 'RESPONDING PARTY: [name]'\n"
    "4. **Motion description**: The type of motion being ruled on.\n"
    "5. **Ruling text**: The full ruling including legal analysis.\n"
    "6. **Judge signature** (sometimes): '[Name] Judge of the Superior "
    "Court' at the end.\n\n"
    "## Case Number Formats\n\n"
    "LA case numbers follow these patterns:\n"
    "- [YY][courthouse][type][seq]: 24NNCV02551, 23CMCV01566, "
    "25SMCV01132, 21CHCV00539, 22STCV35574\n"
    "- Courthouse codes: NN=Northeast, CM=Compton, SM=Santa Monica, "
    "CH=Chatsworth, ST=Stanley Mosk, NW=Northwest, BH=Beverly Hills\n"
    "- Type codes: CV=Civil, CP=Civil Petition/Probate, LC=Limited Civil\n\n"
    "## Rules\n\n"
    "1. Return one ruling object per UNIQUE case with its own ruling. "
    "If the same case number appears twice with different motions, "
    "return each as a separate ruling.\n"
    "2. Extract the case number EXACTLY as it appears (e.g., "
    "'24NNCV02551').\n"
    "3. For case_title, construct 'Plaintiff v. Defendant' from the "
    "party caption. Use the FIRST plaintiff and FIRST defendant "
    "names. Do NOT include role labels, entity descriptors "
    "(like 'a California Corporation'), or the full caption.\n"
    "   - CORRECT: 'Aasi v. American Honda Motor Co., Inc.'\n"
    "   - WRONG: 'SUMAYYA AASI, et al., Plaintiff(s), vs. AMERICAN "
    "HONDA MOTOR CO., INC., et al., Defendant(s).'\n"
    "4. For parties, extract each named party with their role "
    "(plaintiff/defendant/petitioner/respondent/moving_party/"
    "responding_party). Use proper case (title case), not ALL CAPS.\n"
    '   - CORRECT: {"name": "Sumayya Aasi", "role": "plaintiff"}\n'
    '   - WRONG: {"name": "Department 50 Law And Motion Rulings '
    'Case Number: 20Stcv41848", "role": "plaintiff"}\n'
    "5. NEVER include department headers, case numbers, hearing dates, "
    "or court boilerplate in party names.\n"
    "6. For ruling_text, include the FULL text of the ruling after "
    "the motion description. Preserve it VERBATIM.\n"
    "7. Skip the department header boilerplate and submission "
    "instructions — only extract from the case sections.\n"
    "8. If a page has NO case sections (only department header "
    "boilerplate), return an empty rulings array.\n\n"
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted — motion was fully granted (including 'granted with "
    "conditions')\n"
    "- denied — motion was fully denied (including 'denied without "
    "prejudice')\n"
    "- granted_in_part — partially granted and partially denied\n"
    "- denied_in_part — partially denied\n"
    "- moot — motion is moot\n"
    "- continued — hearing was postponed\n"
    "- off_calendar — hearing removed from calendar\n"
    "- submitted — taken under submission\n"
    "- other — none of the above fit\n\n"
    "For 'overruled' (demurrers), map to 'denied'.\n"
    "For 'sustained' (demurrers), map to 'granted'.\n\n"
    "## Motion type labels\n\n"
    "Use a short descriptive label. Common values:\n"
    "msj, msj_partial, demurrer, motion_to_compel, "
    "motion_to_strike, motion_for_leave_to_amend, "
    "motion_for_sanctions, motion_for_attorney_fees, "
    "motion_to_be_relieved_as_counsel, default_judgment, "
    "petition, ex_parte_application, anti_slapp, "
    "preliminary_injunction, other.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "3" or "P" or "F46" or "205" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "24NNCV02551" or null,\n'
    '      "extracted_case_title": "Aasi v. American Honda Motor Co., '
    'Inc." or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "granted" or null,\n'
    '      "motion_type": "motion_to_compel" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "Sumayya Aasi", "role": "plaintiff", '
    '"confidence": "high"},\n'
    '        {"name": "American Honda Motor Co., Inc.", '
    '"role": "defendant", "confidence": "high"}\n'
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

    case_number: str | None = None
    case_title: str | None = None
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
    title_extracted: int = 0
    title_total: int = 0
    title_match: int = 0
    outcome_match: int = 0
    outcome_total: int = 0
    party_clean: int = 0
    party_total: int = 0
    party_contaminated: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_per_ruling: float = 0.0
    estimated_monthly_cost: float = 0.0
    fixture_details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------


def extract_html_text(html_path: Path) -> str | None:
    """Extract cleaned text from an LA ruling HTML file.

    Strips HTML tags and normalizes whitespace while preserving
    structural elements like case number headers and party blocks.
    """
    from bs4 import BeautifulSoup

    html = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    div = soup.find("div", id="speechSynthesis")
    if div is None:
        return None

    return div.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
CALL_TIMEOUT_S = 60


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
        system_instruction=LA_EXTRACTION_PROMPT,
        temperature=0,
        max_output_tokens=8192,
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
        max_tokens=8192,
        temperature=0,
        system=LA_EXTRACTION_PROMPT,
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


def get_la_fixtures() -> list[tuple[Path, dict]]:
    """Load all LA HTML fixtures that have expected ground truth JSON.

    Returns only fixtures with actual ruling content (case_count > 0)
    and that have the enriched 'cases' field for detailed scoring.
    """
    fixtures = []
    for expected_file in sorted(EXPECTED_DIR.glob("la_*.json")):
        expected = json.loads(expected_file.read_text())
        fixture_name = expected.get("_fixture")
        if not fixture_name:
            continue
        fixture_path = FIXTURES_DIR / fixture_name
        if not fixture_path.exists():
            continue
        if fixture_path.suffix != ".html":
            continue
        # Only include fixtures with actual ruling content
        if expected.get("case_count", 0) == 0:
            continue
        # Only include fixtures that have 'cases' field for detailed scoring
        if "cases" not in expected:
            continue
        fixtures.append((fixture_path, expected))
    return fixtures


# ---------------------------------------------------------------------------
# Party name contamination check
# ---------------------------------------------------------------------------

# Patterns that indicate a party name is contaminated with court headers
_CONTAMINATION_PATTERNS = [
    re.compile(r"DEPARTMENT\s+\S+\s+LAW AND MOTION", re.IGNORECASE),
    re.compile(r"Case Number:", re.IGNORECASE),
    re.compile(r"Hearing Date:", re.IGNORECASE),
    re.compile(r"Dept:", re.IGNORECASE),
    re.compile(r"LAW AND MOTION RULINGS", re.IGNORECASE),
    re.compile(r"SUPERIOR COURT", re.IGNORECASE),
    re.compile(r"COUNTY OF LOS ANGELES", re.IGNORECASE),
]


def is_party_contaminated(name: str) -> bool:
    """Check if a party name contains court header text."""
    for pattern in _CONTAMINATION_PATTERNS:
        if pattern.search(name):
            return True
    return False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def normalize_title(title: str | None) -> str | None:
    """Normalize case title for fuzzy comparison."""
    if title is None:
        return None
    s = title.strip().lower()
    # Normalize vs variants
    s = re.sub(r"\bvs\.?\s+", "v. ", s)
    s = re.sub(r"\bversus\s+", "v. ", s)
    # Remove "et al." variants (careful not to eat surrounding spaces)
    s = re.sub(r",?\s*\bet\.?\s*al\.?", "", s)
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
        return e1 == e2

    from difflib import SequenceMatcher

    # Check if one contains the other or high similarity
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


def score_fixture(result: FixtureResult) -> dict:
    """Score a fixture result against expected values."""
    expected_cases = result.expected.get("cases", [])
    expected_count = len(expected_cases)
    actual_count = result.actual_case_count

    # Case count accuracy
    count_exact = expected_count == actual_count

    # Per-case scoring
    title_matches = 0
    title_extracted_count = 0
    title_total = len(expected_cases)
    outcome_matches = 0
    outcome_total = 0
    party_clean_count = 0
    party_contaminated_count = 0
    party_total = 0

    case_details: list[dict] = []

    for i, exp_case in enumerate(expected_cases):
        # Find matching extracted case by case number
        matched_case = None
        for c in result.cases:
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
            detail["extracted_title"] = None
            detail["extracted_outcome"] = None
            detail["title_match"] = False
            detail["outcome_match"] = False
            case_details.append(detail)
            if exp_case.get("outcome"):
                outcome_total += 1
            continue

        detail["matched"] = True
        detail["extracted_title"] = matched_case.case_title
        detail["extracted_outcome"] = matched_case.outcome

        # Title scoring
        if matched_case.case_title is not None:
            title_extracted_count += 1
            if titles_match(matched_case.case_title, exp_case.get("case_title")):
                title_matches += 1
                detail["title_match"] = True
            else:
                detail["title_match"] = False
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

        # Party contamination check
        for p in matched_case.parties:
            party_total += 1
            if is_party_contaminated(p.get("name", "")):
                party_contaminated_count += 1
            else:
                party_clean_count += 1

        case_details.append(detail)

    return {
        "fixture_name": result.fixture_name,
        "expected_case_count": expected_count,
        "actual_case_count": actual_count,
        "count_exact": count_exact,
        "title_extracted": title_extracted_count,
        "title_total": title_total,
        "title_matches": title_matches,
        "outcome_matches": outcome_matches,
        "outcome_total": outcome_total,
        "party_clean": party_clean_count,
        "party_contaminated": party_contaminated_count,
        "party_total": party_total,
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

        summary.title_extracted += scores["title_extracted"]
        summary.title_total += scores["title_total"]
        summary.title_match += scores["title_matches"]
        summary.outcome_match += scores["outcome_matches"]
        summary.outcome_total += scores["outcome_total"]
        summary.party_clean += scores["party_clean"]
        summary.party_contaminated += scores["party_contaminated"]
        summary.party_total += scores["party_total"]

    for r in results:
        if r.error:
            summary.errors.append(r.fixture_name + ": " + r.error)

    latencies = [r.latency_ms for r in valid]
    summary.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    # Cost calculations
    total_rulings = sum(r.actual_case_count for r in valid)
    avg_in = summary.total_input_tokens / len(valid) if valid else 0
    avg_out = summary.total_output_tokens / len(valid) if valid else 0
    cost_per_fixture = (
        avg_in * pricing["input"] + avg_out * pricing["output"]
    ) / 1_000_000
    summary.cost_per_ruling = (
        cost_per_fixture / (total_rulings / len(valid))
        if total_rulings > 0
        else cost_per_fixture
    )
    # Estimate: ~200 department pages/day (100 depts x 2 scrapes)
    summary.estimated_monthly_cost = cost_per_fixture * 200 * 30

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

    # Party name quality
    if summary.party_total > 0:
        clean_pct = summary.party_clean / summary.party_total * 100
        print(
            "Party name quality: "
            + f"{summary.party_clean}/{summary.party_total} clean "
            + f"({clean_pct:.0f}%), "
            + f"{summary.party_contaminated} contaminated"
        )

    # Cost
    print("\nCost per ruling: $" + f"{summary.cost_per_ruling:.5f}")
    print(
        "Estimated monthly cost (~200 dept pages/day): $"
        + f"{summary.estimated_monthly_cost:.2f}"
    )

    # Fixture details
    print("\nPer-fixture breakdown:")
    for detail in summary.fixture_details:
        status = "PASS" if detail["count_exact"] else "FAIL"
        print(
            f"  {detail['fixture_name']}: "
            f"cases {detail['actual_case_count']}/{detail['expected_case_count']} [{status}]"
            f"  titles {detail['title_matches']}/{detail['title_total']}"
            f"  outcomes {detail['outcome_matches']}/{detail['outcome_total']}"
            f"  parties {detail['party_clean']}/{detail['party_total']} clean"
        )
        for case_d in detail.get("case_details", []):
            match_str = "MATCHED" if case_d.get("matched") else "MISSING"
            title_str = "OK" if case_d.get("title_match") else "FAIL"
            outcome_str = "OK" if case_d.get("outcome_match") else "FAIL"
            print(
                f"    {case_d.get('expected_case_number', '?')}: {match_str}"
                f"  title={title_str}"
                f" (got: {case_d.get('extracted_title', 'null')!r},"
                f" exp: {case_d.get('expected_title', 'null')!r})"
                f"  outcome={outcome_str}"
                f" (got: {case_d.get('extracted_outcome', 'null')!r},"
                f" exp: {case_d.get('expected_outcome', 'null')!r})"
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
    """Run extraction eval for a single model against all LA fixtures."""
    fixtures = get_la_fixtures()
    if not fixtures:
        print("No LA fixtures found with enriched expected data.")
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

    print("\nProcessing " + str(len(fixtures)) + " LA fixtures with " + model_name)

    results: list[FixtureResult] = []

    for fixture_path, expected in fixtures:
        fixture_name = fixture_path.name
        print(f"  {fixture_name}... ", end="", flush=True)

        text = extract_html_text(fixture_path)
        if text is None:
            print("SKIP (no speechSynthesis div)")
            result = FixtureResult(
                fixture_name=fixture_name,
                model=model_name,
                expected=expected,
                error="No speechSynthesis div found",
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
                        case_number=r.get("extracted_case_number"),
                        case_title=r.get("extracted_case_title"),
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
        description="Eval LLM extraction on LA County HTML fixtures."
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=None,
        help="Model(s) to evaluate. Default: all models.",
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

    models_to_run = args.models or list(MODELS.keys())
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
            save_path = RESULTS_DIR / f"la_{model_name.replace('.', '_')}_results.json"
            save_data = {
                "model": model_name,
                "total_fixtures": summary.total_fixtures,
                "case_count_correct": summary.case_count_correct,
                "title_extracted": summary.title_extracted,
                "title_total": summary.title_total,
                "title_match": summary.title_match,
                "outcome_match": summary.outcome_match,
                "outcome_total": summary.outcome_total,
                "party_clean": summary.party_clean,
                "party_contaminated": summary.party_contaminated,
                "party_total": summary.party_total,
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "cost_per_ruling": summary.cost_per_ruling,
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
            # Title extraction: 100% of expected titles must be extracted
            if summary.title_total > 0:
                title_rate = summary.title_extracted / summary.title_total
                if title_rate < 1.0:
                    print(f"\nTHRESHOLD FAIL: title extraction {title_rate:.0%} < 100%")
                    threshold_ok = False

            # No contaminated party names
            if summary.party_contaminated > 0:
                print(
                    f"\nTHRESHOLD FAIL: {summary.party_contaminated} "
                    f"contaminated party names"
                )
                threshold_ok = False

            # Outcome accuracy >= 90%
            if summary.outcome_total > 0:
                outcome_rate = summary.outcome_match / summary.outcome_total
                if outcome_rate < 0.90:
                    print(
                        f"\nTHRESHOLD FAIL: outcome accuracy {outcome_rate:.0%} < 90%"
                    )
                    threshold_ok = False

    # Print comparison if multiple models
    if len(all_summaries) > 1:
        print("\n\n=== MODEL COMPARISON ===\n")
        header = f"{'Metric':<30}"
        for s in all_summaries:
            header += f" {s.model:<20}"
        print(header)
        print("-" * len(header))

        metrics = [
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
                    f"{s.title_match}/{s.title_total} ({s.title_match / s.title_total:.0%})"
                    if s.title_total > 0
                    else "N/A"
                ),
            ),
            (
                "Outcome accuracy",
                lambda s: (
                    f"{s.outcome_match}/{s.outcome_total} ({s.outcome_match / s.outcome_total:.0%})"
                    if s.outcome_total > 0
                    else "N/A"
                ),
            ),
            (
                "Party contaminated",
                lambda s: str(s.party_contaminated),
            ),
            (
                "Cost per ruling",
                lambda s: f"${s.cost_per_ruling:.5f}",
            ),
            (
                "Monthly cost est.",
                lambda s: f"${s.estimated_monthly_cost:.2f}",
            ),
        ]

        for label, fn in metrics:
            row = f"{label:<30}"
            for s in all_summaries:
                row += f" {fn(s):<20}"
            print(row)

    if args.check_thresholds and not threshold_ok:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
