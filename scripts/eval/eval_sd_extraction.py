#!/usr/bin/env python3
"""Eval LLM-based extraction on San Diego ROA HTML fixtures.

San Diego tentative rulings are **HTML** pages from the Odyssey ROA portal.
Each page covers a single case.  The scraper already extracts structured
fields (case_number, case_title, judge_name, department, parties) from the
HTML structure.  The LLM's job is to improve extraction of:

  1. outcome (handles nuanced outcomes like partial grants, mixed rulings)
  2. motion_type (broader coverage than the current regex keyword list)

The LLM receives the ruling text (already extracted from the ROA table by
BeautifulSoup) plus known metadata as context.

This eval validates:
  - outcome accuracy: does the LLM extract the correct outcome?
  - motion_type accuracy: does the LLM extract a more precise motion type?
  - regression check: does the LLM match or beat the current regex parser?
  - cost estimate: token counts and cost per document

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    export GOOGLE_API_KEY="AIza..."

    # Run default model (Claude Haiku 4.5)
    python3 scripts/eval/eval_sd_extraction.py

    # Run specific model(s)
    python3 scripts/eval/eval_sd_extraction.py --models claude-haiku-4.5

    # Run all models
    python3 scripts/eval/eval_sd_extraction.py --models claude-haiku-4.5 gemini-2.5-flash-lite gemini-2.5-flash

    # Save results to JSON
    python3 scripts/eval/eval_sd_extraction.py --save

Requirements:
    pip install anthropic google-genai beautifulsoup4 lxml
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
# San Diego ROA extraction prompt
# ---------------------------------------------------------------------------

SD_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from San Diego County Superior Court.\n\n"
    "You will receive the ruling text extracted from an Odyssey ROA "
    "(Register of Actions) page for a SINGLE case.  The structured "
    "fields (case_number, case_title, judge_name, department, parties) "
    "have already been extracted from the HTML structure.  Your job is "
    "to extract two fields from the ruling text that regex cannot "
    "reliably capture:\n"
    "  1. outcome\n"
    "  2. motion_type\n\n"
    "## San Diego ROA Ruling Text Format\n\n"
    "San Diego tentative rulings appear in the ROA events table as "
    "'Tentative Ruling' entries.  The ruling text typically has:\n"
    "1. **Motion description** (bold heading): e.g., 'Demurrer to "
    "Complaint', 'Motion to Compel Further Responses'\n"
    "2. **Hearing info**: date, department, judge\n"
    "3. **Ruling body**: the substantive ruling with legal analysis\n\n"
    "The ruling may address multiple causes of action or multiple "
    "requests, with different outcomes for each.\n\n"
    "## Motion Type Extraction\n\n"
    "Extract the motion type from the ruling text.  Use the full "
    "description from the motion heading (e.g., 'Motion to Compel "
    "Further Responses to Requests for Production', not just 'Motion "
    "to Compel').\n\n"
    "Common motion types in San Diego:\n"
    "- Demurrer to Complaint / Demurrer to Cross-Complaint\n"
    "- Motion to Compel / Motion to Compel Further Responses\n"
    "- Motion to Strike / Motion to Strike Portions of Complaint\n"
    "- Motion for Summary Judgment / Motion for Summary Adjudication\n"
    "- Motion to Quash Service of Summons\n"
    "- Motion for Sanctions\n"
    "- Motion to Set Aside Default\n"
    "- Petition to Compel Arbitration\n"
    "- Preliminary Injunction\n"
    "- Motion for New Trial\n"
    "- Motion for Judgment on the Pleadings\n\n"
    "## Outcome Taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted -- motion/petition was fully granted\n"
    "- denied -- motion was fully denied\n"
    "- granted_in_part -- some parts granted, some denied (includes "
    "mixed demurrer rulings where some causes of action are sustained "
    "and others overruled)\n"
    "- denied_in_part -- partially denied\n"
    "- sustained -- demurrer sustained (all causes of action)\n"
    "- sustained_with_leave -- demurrer sustained with leave to amend "
    "(all causes of action)\n"
    "- overruled -- demurrer overruled (all causes of action)\n"
    "- moot -- motion is moot\n"
    "- continued -- hearing was postponed/continued to a future date\n"
    "- off_calendar -- hearing removed from calendar\n"
    "- submitted -- taken under submission\n"
    "- other -- none of the above fit\n\n"
    "Mapping rules for mixed rulings:\n"
    "- Demurrer sustained as to some COAs, overruled as to others: "
    "'granted_in_part'\n"
    "- Motion granted as to some requests, denied as to others: "
    "'granted_in_part'\n"
    "- 'GRANTED IN PART AND DENIED IN PART': 'granted_in_part'\n"
    "- If only one outcome is stated for the entire ruling, use that "
    "specific outcome (granted, denied, sustained, overruled, etc.)\n\n"
    "## Output Format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "outcome": "granted_in_part",\n'
    '  "motion_type": "Demurrer to Complaint",\n'
    '  "confidence": {\n'
    '    "outcome": "high",\n'
    '    "motion_type": "high"\n'
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
    ruling_text_length: int = 0
    # Outcome
    extracted_outcome: str | None = None
    expected_outcome: str | None = None
    outcome_match: bool = False
    regex_outcome: str | None = None
    # Motion type
    extracted_motion_type: str | None = None
    expected_motion_type: str | None = None
    motion_type_match: bool = False
    regex_motion_type: str | None = None
    # Tokens and cost
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
    motion_type_correct: int = 0
    motion_type_partial: int = 0
    motion_type_wrong: int = 0
    motion_type_null: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_per_doc: float = 0.0
    estimated_monthly_cost: float = 0.0
    fixture_details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTML text extraction for ruling text
# ---------------------------------------------------------------------------


def extract_ruling_text(html_path: Path) -> str | None:
    """Extract ruling text from an SD ROA HTML fixture using the same
    BeautifulSoup logic as the scraper's parse_tentative_ruling().
    """
    from bs4 import BeautifulSoup

    html_str = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_str, "lxml")

    roa_table = soup.find("table", class_="roa-table")
    if not roa_table:
        return None

    tbody = roa_table.find("tbody")
    if not tbody:
        return None

    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 3:
            continue

        event_type = tds[1].get_text(strip=True)
        if event_type.lower() != "tentative ruling":
            continue

        ruling_div = tds[2].find("div", class_="ruling-content")
        if ruling_div:
            paragraphs = ruling_div.find_all("p")
            text_parts: list[str] = []
            for p in paragraphs:
                text = p.get_text(strip=True)
                if text and text != "\xa0":
                    text_parts.append(text)
            return "\n\n".join(text_parts)

    return None


# ---------------------------------------------------------------------------
# Regex-based extraction (current scraper baseline)
# ---------------------------------------------------------------------------

# These match the regexes in sd_tentatives.py

_OUTCOME_RE = re.compile(
    r"\b(?P<outcome>"
    r"GRANTED(?:\s+IN\s+PART(?:\s+AND\s+DENIED\s+IN\s+PART)?)?"
    r"|DENIED(?:\s+IN\s+PART)?"
    r"|SUSTAINED(?:\s+WITH\s+LEAVE\s+TO\s+AMEND)?"
    r"|OVERRULED"
    r"|MOOT"
    r"|OFF\s+CALENDAR"
    r")\b",
    re.IGNORECASE,
)

_MOTION_TYPE_RE = re.compile(
    r"\b(?P<motion_type>"
    r"Demurrer(?:\s+to\s+\w+)?"
    r"|Motion\s+to\s+(?:Compel(?:\s+Further\s+Responses)?|Dismiss|Strike|Quash"
    r"|Stay|Vacate|Set\s+Aside|Tax\s+Costs)"
    r"|Motion\s+for\s+(?:Summary\s+(?:Judgment|Adjudication)|Sanctions"
    r"|New\s+Trial|Judgment\s+on\s+the\s+Pleadings)"
    r"|Summary\s+(?:Judgment|Adjudication)"
    r"|Petition\s+to\s+Compel\s+Arbitration"
    r"|Writ\s+of\s+Attachment"
    r"|Preliminary\s+Injunction"
    r")\b",
    re.IGNORECASE,
)


def regex_extract_outcome(text: str) -> str | None:
    """Extract outcome using the current scraper regex."""
    m = _OUTCOME_RE.search(text)
    if m:
        raw = m.group("outcome").strip()
        return " ".join(raw.upper().split())
    return None


def regex_extract_motion_type(text: str) -> str | None:
    """Extract motion type using the current scraper regex."""
    m = _MOTION_TYPE_RE.search(text)
    if m:
        raw = m.group("motion_type").strip()
        normalized = " ".join(raw.split())
        if normalized and normalized[0].islower():
            normalized = normalized[0].upper() + normalized[1:]
        return normalized
    return None


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


def _build_user_message(ruling_text: str) -> str:
    """Build the user message with the ruling text."""
    return "Ruling text from San Diego ROA:\n\n" + ruling_text


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
        system_instruction=SD_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=2048,
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
        max_tokens=2048,
        temperature=0,
        system=SD_SYSTEM_PROMPT,
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


def get_sd_fixtures() -> list[tuple[Path, dict]]:
    """Load all SD ROA fixtures that have expected ground truth JSON."""
    fixtures = []
    for expected_file in sorted(EXPECTED_DIR.glob("sd_roa_*.json")):
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
# Outcome normalization for comparison
# ---------------------------------------------------------------------------


def _normalize_outcome(outcome: str | None) -> str | None:
    """Normalize outcome string for comparison.

    Maps regex-style UPPERCASE outcomes and LLM-style lowercase outcomes
    to a common form for comparison.
    """
    if outcome is None:
        return None

    outcome = outcome.strip().lower()
    outcome = re.sub(r"\s+", " ", outcome)

    # Map common variants to taxonomy values
    mapping = {
        "granted": "granted",
        "denied": "denied",
        "granted in part": "granted_in_part",
        "granted in part and denied in part": "granted_in_part",
        "denied in part": "denied_in_part",
        "sustained": "sustained",
        "sustained with leave to amend": "sustained_with_leave",
        "overruled": "overruled",
        "moot": "moot",
        "off calendar": "off_calendar",
    }

    return mapping.get(outcome, outcome.replace(" ", "_"))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _normalize_motion_type(mt: str | None) -> str:
    """Normalize a motion type string for comparison."""
    if mt is None:
        return ""
    return re.sub(r"\s+", " ", mt.strip().lower())


def score_motion_type(
    extracted: str | None,
    expected: str | None,
) -> str:
    """Score motion type extraction.

    Returns: "exact", "partial", or "wrong"
    """
    if not expected:
        return "exact" if not extracted else "partial"
    if not extracted:
        return "wrong"

    norm_ext = _normalize_motion_type(extracted)
    norm_exp = _normalize_motion_type(expected)

    if norm_ext == norm_exp:
        return "exact"

    # Partial: one is a substring of the other
    if norm_exp in norm_ext or norm_ext in norm_exp:
        return "partial"

    # Check for key words overlap
    ext_words = set(norm_ext.split())
    exp_words = set(norm_exp.split())
    overlap = ext_words & exp_words
    # If more than half the expected words match, it's partial
    if len(overlap) > len(exp_words) / 2:
        return "partial"

    return "wrong"


def score_fixture(result: FixtureResult) -> dict:
    """Score a single fixture result."""
    # Outcome
    norm_extracted = _normalize_outcome(result.extracted_outcome)
    norm_expected = _normalize_outcome(result.expected_outcome)
    outcome_match = False
    if norm_extracted is not None and norm_expected is not None:
        outcome_match = norm_extracted == norm_expected
    elif norm_extracted is None and norm_expected is None:
        outcome_match = True

    # Motion type
    mt_level = score_motion_type(
        result.extracted_motion_type, result.expected_motion_type
    )

    # Regex baseline comparison
    norm_regex_outcome = _normalize_outcome(result.regex_outcome)
    regex_outcome_match = False
    if norm_regex_outcome is not None and norm_expected is not None:
        regex_outcome_match = norm_regex_outcome == norm_expected
    elif norm_regex_outcome is None and norm_expected is None:
        regex_outcome_match = True

    regex_mt_level = score_motion_type(
        result.regex_motion_type, result.expected_motion_type
    )

    return {
        "fixture_name": result.fixture_name,
        "outcome_match": outcome_match,
        "expected_outcome": result.expected_outcome,
        "extracted_outcome": result.extracted_outcome,
        "regex_outcome": result.regex_outcome,
        "regex_outcome_match": regex_outcome_match,
        "motion_type_level": mt_level,
        "expected_motion_type": result.expected_motion_type,
        "extracted_motion_type": result.extracted_motion_type,
        "regex_motion_type": result.regex_motion_type,
        "regex_mt_level": regex_mt_level,
        "ruling_text_length": result.ruling_text_length,
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

        if scores["motion_type_level"] == "exact":
            summary.motion_type_correct += 1
        elif scores["motion_type_level"] == "partial":
            summary.motion_type_partial += 1
        elif scores["extracted_motion_type"] is None:
            summary.motion_type_null += 1
        else:
            summary.motion_type_wrong += 1

    for r in results:
        if r.error:
            summary.errors.append(r.fixture_name + ": " + r.error)

    latencies = [r.latency_ms for r in valid]
    summary.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    # Cost calculations
    avg_in = summary.total_input_tokens / len(valid) if valid else 0
    avg_out = summary.total_output_tokens / len(valid) if valid else 0
    summary.cost_per_doc = (
        avg_in * pricing["input"] + avg_out * pricing["output"]
    ) / 1_000_000
    # Estimate: ~10 documents/day (SD scrapes ~10 cases per run)
    summary.estimated_monthly_cost = summary.cost_per_doc * 10 * 30

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
    print("Avg latency per doc: " + f"{summary.avg_latency_ms:,.0f}" + "ms")

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
    print("  Wrong:    " + str(summary.outcome_wrong) + "/" + str(total))
    print("  Null:     " + str(summary.outcome_null) + "/" + str(total))

    print("\n## Motion Type Accuracy")
    print(
        "  Exact:    "
        + str(summary.motion_type_correct)
        + "/"
        + str(total)
        + " ("
        + f"{summary.motion_type_correct / total * 100:.0f}"
        + "%)"
    )
    print("  Partial:  " + str(summary.motion_type_partial) + "/" + str(total))
    print("  Wrong:    " + str(summary.motion_type_wrong) + "/" + str(total))
    print("  Null:     " + str(summary.motion_type_null) + "/" + str(total))

    # Per-fixture detail with regex vs LLM comparison
    print("\n## Per-Fixture Comparison (Regex vs LLM)")
    print(
        "  "
        + f"{'Fixture':<30}"
        + f"{'Regex Outcome':>18}"
        + f"{'LLM Outcome':>18}"
        + f"{'Expected':>18}"
        + f"{'Match':>6}"
    )
    print(
        "  "
        + "-" * 30
        + " "
        + "-" * 18
        + " "
        + "-" * 18
        + " "
        + "-" * 18
        + " "
        + "-" * 6
    )
    for detail in summary.fixture_details:
        fname = detail["fixture_name"]
        if len(fname) > 28:
            fname = fname[:25] + "..."
        regex_oc = str(detail["regex_outcome"] or "null")
        if len(regex_oc) > 16:
            regex_oc = regex_oc[:14] + ".."
        llm_oc = str(detail["extracted_outcome"] or "null")
        if len(llm_oc) > 16:
            llm_oc = llm_oc[:14] + ".."
        exp_oc = str(detail["expected_outcome"] or "null")
        if len(exp_oc) > 16:
            exp_oc = exp_oc[:14] + ".."
        match_tag = " OK" if detail["outcome_match"] else " MISS"
        print(
            "  "
            + f"{fname:<30}"
            + f"{regex_oc:>18}"
            + f"{llm_oc:>18}"
            + f"{exp_oc:>18}"
            + f"{match_tag:>6}"
        )

    print()
    print(
        "  "
        + f"{'Fixture':<30}"
        + f"{'Regex MotionType':>25}"
        + f"{'LLM MotionType':>25}"
        + f"{'Match':>7}"
    )
    print("  " + "-" * 30 + " " + "-" * 25 + " " + "-" * 25 + " " + "-" * 7)
    for detail in summary.fixture_details:
        fname = detail["fixture_name"]
        if len(fname) > 28:
            fname = fname[:25] + "..."
        regex_mt = str(detail["regex_motion_type"] or "null")
        if len(regex_mt) > 23:
            regex_mt = regex_mt[:21] + ".."
        llm_mt = str(detail["extracted_motion_type"] or "null")
        if len(llm_mt) > 23:
            llm_mt = llm_mt[:21] + ".."
        mt_tag = " " + detail["motion_type_level"].upper()
        print(
            "  " + f"{fname:<30}" + f"{regex_mt:>25}" + f"{llm_mt:>25}" + f"{mt_tag:>7}"
        )

    if summary.errors:
        print("\nErrors (" + str(len(summary.errors)) + "):")
        for err in summary.errors:
            print("  " + err)

    print("\nCost estimate:")
    print("  Per document: $" + f"{summary.cost_per_doc:.6f}")
    print("  Monthly (~10 docs/day): $" + f"{summary.estimated_monthly_cost:.2f}")


def print_comparison_table(summaries: dict[str, ModelSummary]) -> None:
    """Print a side-by-side comparison table of all models."""
    model_names = list(summaries.keys())

    print("\n" + "=" * 80)
    print("COMPARISON TABLE: San Diego ROA Extraction Eval")
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

    # Outcome accuracy
    row = f"{'Outcome accuracy':<35}"
    for name in model_names:
        s = summaries[name]
        pct = s.outcome_correct / s.total_fixtures * 100 if s.total_fixtures > 0 else 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Motion type exact match
    row = f"{'Motion type exact match':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            s.motion_type_correct / s.total_fixtures * 100
            if s.total_fixtures > 0
            else 0
        )
        row += f"  {pct:>19.1f}%"
    print(row)

    # Motion type partial+exact
    row = f"{'Motion type partial+ match':<35}"
    for name in model_names:
        s = summaries[name]
        pct = (
            (s.motion_type_correct + s.motion_type_partial) / s.total_fixtures * 100
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

    row = f"{'Monthly cost (~10/day)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.estimated_monthly_cost:>18.2f}"
    print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the San Diego LLM extraction evaluation."""
    parser = argparse.ArgumentParser(
        description="Eval LLM extraction accuracy on San Diego ROA HTML fixtures."
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

    google_models = [m for m in args.models if MODELS[m]["provider"] == "google"]
    anthropic_models = [m for m in args.models if MODELS[m]["provider"] == "anthropic"]

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
    fixtures = get_sd_fixtures()
    print("Found " + str(len(fixtures)) + " San Diego ROA fixtures with ground truth\n")
    for fp, exp in fixtures:
        motion = exp.get("motion_type", "?")
        print("  " + fp.name + ": " + motion)
    print()

    if not fixtures:
        print("ERROR: No SD ROA fixtures found.")
        print("Expected sd_roa_*.json files in " + str(EXPECTED_DIR))
        return 1

    # Extract ruling text from all fixtures
    print("Extracting ruling text from HTML fixtures...")
    fixture_texts: dict[str, str | None] = {}
    for fp, _ in fixtures:
        ruling_text = extract_ruling_text(fp)
        fixture_texts[fp.name] = ruling_text
        if ruling_text:
            char_count = len(ruling_text)
            print("  " + fp.name + ": " + str(char_count) + " chars of ruling text")
        else:
            print(
                "  "
                + fp.name
                + ": no ruling text found (expected for no-ruling fixtures)"
            )
    print()

    # Regex baseline comparison
    print("Regex baseline (current scraper):")
    for fp, expected in fixtures:
        text = fixture_texts[fp.name]
        if text:
            regex_oc = regex_extract_outcome(text) or "(none)"
            regex_mt = regex_extract_motion_type(text) or "(none)"
        else:
            regex_oc = "(no text)"
            regex_mt = "(no text)"
        exp_cases = expected.get("expected_cases", [])
        exp_oc = exp_cases[0].get("outcome", "?") if exp_cases else "?"
        exp_mt = exp_cases[0].get("motion_type", "?") if exp_cases else "?"
        print(
            "  "
            + fp.name
            + ": regex outcome="
            + regex_oc
            + " (expected: "
            + exp_oc
            + ") | regex motion="
            + regex_mt
            + " (expected: "
            + exp_mt
            + ")"
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
            + ") -- outcome + motion_type extraction"
        )
        print("=" * 70 + "\n")

        results: list[FixtureResult] = []

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            ruling_text = fixture_texts[fname]

            expected_cases = expected.get("expected_cases", [])
            exp_case = expected_cases[0] if expected_cases else {}
            expected_outcome = exp_case.get("outcome")
            expected_motion_type = exp_case.get("motion_type")

            # Skip fixtures with no ruling text (e.g., sd_roa_no_ruling)
            if ruling_text is None:
                print("  " + fname + ": SKIP (no ruling text)")
                continue

            print(
                "  " + fname + " (" + str(len(ruling_text)) + " chars)...",
                end=" ",
                flush=True,
            )

            # Regex baseline
            regex_oc = regex_extract_outcome(ruling_text)
            regex_mt = regex_extract_motion_type(ruling_text)

            fixture_result = FixtureResult(
                fixture_name=fname,
                model=model_name,
                expected=expected,
                ruling_text_length=len(ruling_text),
                expected_outcome=expected_outcome,
                expected_motion_type=expected_motion_type,
                regex_outcome=regex_oc,
                regex_motion_type=regex_mt,
            )

            try:
                user_message = _build_user_message(ruling_text)

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
                fixture_result.extracted_motion_type = extracted.get("motion_type")

                # Check outcome
                norm_ext = _normalize_outcome(fixture_result.extracted_outcome)
                norm_exp = _normalize_outcome(expected_outcome)
                fixture_result.outcome_match = (
                    norm_ext is not None
                    and norm_exp is not None
                    and norm_ext == norm_exp
                )

                # Check motion type
                fixture_result.motion_type_match = (
                    score_motion_type(
                        fixture_result.extracted_motion_type,
                        expected_motion_type,
                    )
                    == "exact"
                )

                outcome_tag = "OK" if fixture_result.outcome_match else "MISS"

                print(
                    "outcome="
                    + str(fixture_result.extracted_outcome)
                    + " ("
                    + outcome_tag
                    + "), "
                    + "motion="
                    + str(fixture_result.extracted_motion_type)
                    + " ["
                    + str(inp_tok)
                    + "+"
                    + str(out_tok)
                    + " tok, "
                    + f"{lat:.0f}"
                    + "ms]"
                )

                # Print comparison with regex
                norm_regex = _normalize_outcome(regex_oc)
                regex_tag = "OK" if (norm_regex == norm_exp) else "MISS"
                print(
                    "    regex: outcome="
                    + str(regex_oc)
                    + " ("
                    + regex_tag
                    + "), "
                    + "motion="
                    + str(regex_mt)
                )

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
        output_path = RESULTS_DIR / "sd_extraction_results.json"

        save_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "eval_type": "sd_roa_extraction",
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
                "motion_type_exact": summary.motion_type_correct,
                "motion_type_partial": summary.motion_type_partial,
                "motion_type_wrong": summary.motion_type_wrong,
                "motion_type_null": summary.motion_type_null,
                "motion_type_exact_pct": (
                    summary.motion_type_correct / summary.total_fixtures * 100
                    if summary.total_fixtures > 0
                    else 0
                ),
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "avg_latency_ms": summary.avg_latency_ms,
                "cost_per_doc": summary.cost_per_doc,
                "monthly_cost_estimate": summary.estimated_monthly_cost,
            }

        output_path.write_text(json.dumps(save_data, indent=2) + "\n")
        print("\nResults saved to " + str(output_path))

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
