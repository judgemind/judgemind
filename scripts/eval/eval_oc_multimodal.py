#!/usr/bin/env python3
"""Eval per-page multimodal extraction on OC PDF fixtures.

The LLM's per-page job is simple:
  1. Transcribe the ruling text for each case visible on the page
  2. Split cases -- identify where one case ends and another begins
  3. Regex heuristic detects continuations (NOT the LLM -- Flash Lite can't do it)

Structured field extraction (case_number, case_title, outcome, etc.) is NOT
the LLM's job -- the enrichment layer downstream handles that.

This eval validates:
  - case_count: does the LLM find the right number of cases per document?
  - ruling_text: is it non-empty and reasonable length for each case?
  - join logic: do cross-page continuations merge correctly?

Usage:
    export GOOGLE_API_KEY="AIza..."
    export ANTHROPIC_API_KEY="sk-ant-..."

    # Run all models (default)
    python3 scripts/eval/eval_oc_multimodal.py

    # Run specific model(s)
    python3 scripts/eval/eval_oc_multimodal.py --models gemini-2.5-flash-lite

    # Save results to JSON
    python3 scripts/eval/eval_oc_multimodal.py --save

Requirements:
    pip install google-genai anthropic pymupdf
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
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

# Skip fixtures that are index pages, not actual ruling documents
SKIP_FIXTURES = {
    "oc_civil_page.json",
    "oc_family_law_page.json",
    "oc_probate_page.json",
}

# Known fixture issues -- expected ground truth is inaccurate
KNOWN_FIXTURE_ISSUES: set[str] = {
    "oc_north_n.pdf",  # link text says N6/Bancroft but PDF is N14/Oberholzer
}

# ---------------------------------------------------------------------------
# Simplified per-page prompt
# ---------------------------------------------------------------------------

PER_PAGE_SYSTEM_PROMPT = (
    "You are a court ruling transcriber. You will receive a single page "
    "image from a California court tentative ruling PDF.\n\n"
    "The page contains a TABLE with THREE COLUMNS separated by TWO "
    "VERTICAL RULED LINES that run the full height of the page. ROWS "
    "are separated by HORIZONTAL RULED LINES.\n\n"
    "Use the VISUAL POSITION of text relative to the ruled lines to "
    "determine which column it belongs to:\n\n"
    "- Column 1 (entry_number): VERY NARROW column at the far left, "
    "LEFT of the first vertical line. Usually blank.\n"
    "- Column 2 (case_info): MEDIUM column BETWEEN the two vertical "
    "lines. Contains case name and case number.\n"
    "- Column 3 (ruling_text): the WIDEST column, taking up most "
    "of the page width, RIGHT of the second vertical line. Contains "
    "the full ruling text including all paragraphs, headings, and "
    "numbered sections.\n\n"
    "Most text on the page is in column 3. When in doubt about "
    "column membership, the text is in column 3.\n\n"
    "## Rules\n\n"
    "- Return ONE JSON object per ROW (between horizontal lines).\n"
    "- Read each column by its POSITION relative to the vertical "
    "lines.\n"
    "- Transcribe text VERBATIM. Do not summarize or omit.\n"
    "- If a column is blank in a row, set its value to empty string.\n"
    "- SKIP page headers, footers, and page numbers.\n\n"
    "{\n"
    '  "rulings": [\n'
    '    {"entry_number": "101", "case_info": "Smith vs Jones\\n'
    '25-01455183",\n'
    '     "ruling_text": "Full text of the ruling including all '
    'paragraphs and sub-sections..."},\n'
    '    {"entry_number": "", "case_info": "",\n'
    '     "ruling_text": "continuation from previous page..."}\n'
    "  ]\n"
    "}"
)

USER_PAGE_MESSAGE = (
    "Transcribe all rows of the ruling table on this page. "
    "One entry per row. Skip page headers and footers."
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PageResult:
    """Result from sending a single page to the LLM."""

    page_number: int
    rulings: list[dict]  # each has "continued" and "ruling_text"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class FixtureResult:
    """Result from processing all pages of a single fixture."""

    fixture_name: str
    model: str
    expected: dict
    page_results: list[PageResult] = field(default_factory=list)
    # After join
    joined_rulings: list[str] = field(default_factory=list)
    expected_case_count: int = 0
    actual_case_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    page_count: int = 0
    error: str | None = None


@dataclass
class ModelSummary:
    """Aggregated results for a single model."""

    model: str
    total_fixtures: int = 0
    case_count_correct: int = 0
    case_count_off_by_one: int = 0
    case_count_wrong: int = 0
    empty_rulings: int = 0  # rulings with empty text
    total_rulings: int = 0
    continuation_pages: int = 0  # pages that started with a continuation
    total_pages: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_per_fixture: float = 0.0
    estimated_monthly_cost: float = 0.0
    fixture_details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------


def render_pdf_to_images(
    pdf_path: Path,
    *,
    dpi: int = 150,
    max_pages: int = 50,
) -> list[bytes]:
    """Render PDF pages to PNG images using pymupdf."""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    images: list[bytes] = []

    zoom = dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)

    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=matrix)
        images.append(pix.tobytes("png"))

    doc.close()
    return images


MAX_RETRIES = 3
CALL_TIMEOUT_S = 20  # timeout per API call


def _call_with_retry(
    fn,
    *args,
    max_retries: int = MAX_RETRIES,
    timeout_s: float = CALL_TIMEOUT_S,
) -> tuple[dict, int, int, float]:
    """Retry wrapper for LLM API calls.

    Retries on 503 errors and timeouts (> timeout_s seconds).
    """
    import concurrent.futures

    for attempt in range(max_retries):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fn, *args)
                result = future.result(timeout=timeout_s)
                return result
        except concurrent.futures.TimeoutError:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"TIMEOUT ({timeout_s}s), retry {attempt + 1}...", end=" ", flush=True)
                time.sleep(wait)
            else:
                raise TimeoutError(f"Timed out after {max_retries} retries")
        except Exception as e:
            err_str = str(e)
            if "503" in err_str and attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"503, retry {attempt + 1}...", end=" ", flush=True)
                time.sleep(wait)
            else:
                raise

    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Per-page LLM calls
# ---------------------------------------------------------------------------


def call_gemini_page(
    model_id: str,
    image: bytes,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Gemini API with a single page image.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    parts: list[types.Part] = [
        types.Part.from_bytes(data=image, mime_type="image/png"),
        types.Part.from_text(text=USER_PAGE_MESSAGE),
    ]

    config = types.GenerateContentConfig(
        system_instruction=PER_PAGE_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=4096,
        response_mime_type="application/json",
    )

    start = time.monotonic()
    response = client.models.generate_content(
        model=model_id,
        contents=parts,
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

    # Normalize: if model returned a raw list, wrap it
    if isinstance(result, list):
        result = {"rulings": result}

    return result, input_tokens, output_tokens, latency


def call_anthropic_page(
    model_id: str,
    image: bytes,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Anthropic API with a single page image.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(image).decode("ascii"),
            },
        },
        {
            "type": "text",
            "text": USER_PAGE_MESSAGE,
        },
    ]

    start = time.monotonic()
    response = client.messages.create(
        model=model_id,
        max_tokens=4096,
        temperature=0,
        system=PER_PAGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
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
# Regex heuristic for continuation detection
# ---------------------------------------------------------------------------

# OC case numbers: 2-4 digit year, dash, 7-8 digit sequence
# Optionally prefixed with a 2-digit county code (e.g. "30-2024-01420730")
_OC_CASE_NUMBER_RE = re.compile(
    r"^\s*(?:\d{1,3}\s+)?"  # optional entry number (e.g. "1 ", "102 ")
    r"(?:\d{2}-)?",  # optional county prefix (e.g. "30-")
)
_OC_CASE_NUM_BODY_RE = re.compile(r"\b\d{2,4}-\d{7,8}\b")

# North JC entries: entry number + case name with "vs" or "v."
_NORTH_ENTRY_RE = re.compile(
    r"^\s*\d{1,3}\s+[A-Za-z].+(?:\bvs?\b|\bv\.)",
    re.IGNORECASE,
)

# Table header patterns (repeated on each page)
_TABLE_HEADER_RE = re.compile(
    r"^\s*(?:TENTATIVE\s+RULINGS?|(?:LAW\s*&?\s*MOTION|DEPT)\b|"
    r"Entry\s*#?\s*Case\s*(?:Name|Title|Number))",
    re.IGNORECASE,
)


def _text_starts_new_case(text: str) -> bool:
    """Heuristic: does this ruling text contain a new case?

    Returns True if the text contains a case number pattern anywhere
    (OC case numbers like 25-01455183 or 2024-01393434), or an entry
    number + case name with "vs" (North JC pattern).

    Returns False if no case identifier is found — meaning this is
    continuation text from a previous page's case.
    """
    if not text.strip():
        return False

    # Check for OC case number anywhere in the text
    if _OC_CASE_NUM_BODY_RE.search(text):
        return True

    # Check for North JC entry pattern (number + name with "vs")
    # Need to check each line since the pattern could be after headers
    for line in text.strip().split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _TABLE_HEADER_RE.match(stripped):
            continue
        if _NORTH_ENTRY_RE.match(stripped):
            return True

    return False


# ---------------------------------------------------------------------------
# Join logic: merge cross-page continuations
# ---------------------------------------------------------------------------


_VALID_ENTRY_NUMBER_RE = re.compile(r"^\d+$")


def _is_valid_entry_number(raw: str) -> bool:
    """Check if a raw entry_number value is a real entry number.

    Real entry numbers in OC PDFs are plain integers (1, 2, 101)
    sometimes with a trailing period (9., 10.). Roman numerals
    (III.), letters (B.), etc. are section headings from column 3.
    """
    cleaned = raw.rstrip(".")
    return bool(_VALID_ENTRY_NUMBER_RE.match(cleaned))


# Patterns that indicate real case_info (column 2 content):
# - Case number with year prefix (e.g. "2024-01393434", "30-2024-01393434")
# - Standalone 7-8 digit case number (e.g. "01430606" in probate)
# - Case name with adversarial indicator (e.g. "Smith vs Jones")
_CASE_NUMBER_RE = re.compile(r"\d{2,4}-\d{5,8}")
_STANDALONE_CASE_NUM_RE = re.compile(r"\b\d{7,8}\b")
_CASE_NAME_RE = re.compile(r"\bvs?\.?\b", re.IGNORECASE)


def join_page_results(page_results: list[PageResult]) -> list[str]:
    """Merge per-page ruling results into complete rulings.

    Uses entry_number + case_info to detect new cases:
    - entry_number is a valid integer AND case_info is non-empty → new case
    - Otherwise → continuation of previous case

    Real entry numbers are always plain integers (1, 2, 101), sometimes
    with trailing periods (9., 10.). The PDF always has case name/number
    in column 2 alongside a real entry number. Section headings (1., 2.)
    within column 3 lack case_info — this distinguishes them.

    Falls back to regex heuristic if entry_number field is not present.

    Returns a list of complete ruling texts.
    """
    joined: list[str] = []

    for pr in page_results:
        if pr.error or not pr.rulings:
            continue

        for ruling in pr.rulings:
            text = (ruling.get("ruling_text") or "").strip()
            case_info = (ruling.get("case_info") or "").strip()
            entry_num = (ruling.get("entry_number") or "").strip()

            # Skip entries with no content at all
            if not text and not case_info:
                continue

            if "entry_number" in ruling:
                # New prompt format: use entry_number + case_info
                # A new case requires:
                # 1. A valid integer entry_number (column 1)
                # 2. case_info that contains an OC case number
                #    (column 2 structurally always has one)
                # This filters out section headings and legal citations
                # that the LLM misattributes to columns 1/2.
                # case_info must look like real case identification:
                # a case number, standalone case ID, or "vs"/"v."
                looks_like_case = bool(
                    _CASE_NUMBER_RE.search(case_info)
                    or _STANDALONE_CASE_NUM_RE.search(case_info)
                    or _CASE_NAME_RE.search(case_info)
                ) if case_info else False
                has_valid_entry = (
                    bool(entry_num)
                    and _is_valid_entry_number(entry_num)
                    and looks_like_case
                )
                is_new_case = has_valid_entry
            else:
                # Fallback: regex heuristic
                is_new_case = _text_starts_new_case(text)

            if is_new_case or not joined:
                # New case — combine all columns into full text
                parts = [p for p in [case_info, text] if p]
                joined.append("\n".join(parts))
                ruling["_heuristic_continued"] = False
            else:
                # Continuation or spill-over — merge into previous
                parts = [p for p in [case_info, text] if p]
                if parts:
                    joined[-1] = joined[-1] + "\n" + "\n".join(parts)
                ruling["_heuristic_continued"] = True

    return joined


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def get_oc_fixtures() -> list[tuple[Path, dict]]:
    """Load all OC PDF fixtures that have expected ground truth JSON."""
    fixtures = []
    for expected_file in sorted(EXPECTED_DIR.glob("oc_*.json")):
        if expected_file.name in SKIP_FIXTURES:
            continue
        expected = json.loads(expected_file.read_text())
        if expected.get("_edge_case") in ("stale_viewstate", "access_denied"):
            continue
        if expected.get("_status") == "ACCESS_DENIED":
            continue
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


def score_fixture(result: FixtureResult) -> dict:
    """Score a fixture result. Returns a dict with scoring details."""
    expected_count = result.expected_case_count
    actual_count = result.actual_case_count

    # Case count accuracy
    count_diff = abs(expected_count - actual_count)
    count_exact = count_diff == 0
    count_off_by_one = count_diff == 1

    # Ruling text quality
    empty_count = 0
    short_count = 0
    total_text_len = 0
    ruling_lengths: list[int] = []
    for text in result.joined_rulings:
        text_len = len(text.strip())
        ruling_lengths.append(text_len)
        total_text_len += text_len
        if text_len == 0:
            empty_count += 1
        elif text_len < 50:
            short_count += 1

    # Continuation tracking (heuristic-based)
    continuation_count = 0
    for pr in result.page_results:
        if pr.rulings and pr.rulings[0].get("_heuristic_continued", False):
            continuation_count += 1

    return {
        "fixture_name": result.fixture_name,
        "expected_case_count": expected_count,
        "actual_case_count": actual_count,
        "count_exact": count_exact,
        "count_off_by_one": count_off_by_one,
        "count_diff": count_diff,
        "total_rulings": len(result.joined_rulings),
        "empty_rulings": empty_count,
        "short_rulings": short_count,
        "ruling_lengths": ruling_lengths,
        "avg_ruling_length": (
            total_text_len / len(result.joined_rulings)
            if result.joined_rulings
            else 0
        ),
        "continuation_pages": continuation_count,
        "page_count": result.page_count,
        "is_known_issue": result.fixture_name in KNOWN_FIXTURE_ISSUES,
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
        summary.total_input_tokens += r.total_input_tokens
        summary.total_output_tokens += r.total_output_tokens
        summary.total_pages += r.page_count

        scores = score_fixture(r)
        summary.fixture_details.append(scores)

        if scores["is_known_issue"]:
            # Don't count known fixture issues against the model
            pass
        elif scores["count_exact"]:
            summary.case_count_correct += 1
        elif scores["count_off_by_one"]:
            summary.case_count_off_by_one += 1
        else:
            summary.case_count_wrong += 1

        summary.total_rulings += scores["total_rulings"]
        summary.empty_rulings += scores["empty_rulings"]
        summary.continuation_pages += scores["continuation_pages"]

    for r in results:
        if r.error:
            summary.errors.append(r.fixture_name + ": " + r.error)

    latencies = [r.total_latency_ms for r in valid]
    summary.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    # Cost calculations
    avg_in = summary.total_input_tokens / len(valid) if valid else 0
    avg_out = summary.total_output_tokens / len(valid) if valid else 0
    summary.cost_per_fixture = (
        avg_in * pricing["input"] + avg_out * pricing["output"]
    ) / 1_000_000
    # Estimate: ~66 PDFs/day (33 departments x 2 scrapes)
    summary.estimated_monthly_cost = summary.cost_per_fixture * 66 * 30

    return summary


def print_model_report(summary: ModelSummary) -> None:
    """Print a detailed report for a single model."""
    print("\n--- " + summary.model + " ---\n")
    print("Fixtures processed: " + str(summary.total_fixtures))
    print(
        "Total tokens: " + f"{summary.total_input_tokens:,}" + " input + "
        + f"{summary.total_output_tokens:,}" + " output"
    )
    print("Total pages processed: " + str(summary.total_pages))
    print("Avg latency per fixture: " + f"{summary.avg_latency_ms:,.0f}" + "ms")

    # Exclude known fixture issues from denominator
    scorable = (
        summary.case_count_correct
        + summary.case_count_off_by_one
        + summary.case_count_wrong
    )
    if scorable == 0:
        print("\nNo scorable fixtures (all known issues).")
        return

    print("\n## Case Count Accuracy")
    print(
        "  Exact match:  "
        + str(summary.case_count_correct) + "/" + str(scorable)
    )
    print(
        "  Off by 1:     "
        + str(summary.case_count_off_by_one) + "/" + str(scorable)
    )
    print(
        "  Wrong (>1):   "
        + str(summary.case_count_wrong) + "/" + str(scorable)
    )
    exact_pct = summary.case_count_correct / scorable * 100
    lenient_pct = (
        (summary.case_count_correct + summary.case_count_off_by_one)
        / scorable * 100
    )
    print("  Exact accuracy:   " + f"{exact_pct:.1f}" + "%")
    print(
        "  Lenient accuracy: " + f"{lenient_pct:.1f}"
        + "% (off-by-1 counted as correct)"
    )

    print("\n## Ruling Text Quality")
    print("  Total rulings extracted: " + str(summary.total_rulings))
    print("  Empty rulings:           " + str(summary.empty_rulings))
    if summary.total_rulings > 0:
        non_empty_pct = (
            (summary.total_rulings - summary.empty_rulings)
            / summary.total_rulings * 100
        )
        print("  Non-empty rate:          " + f"{non_empty_pct:.1f}" + "%")

    print("\n## Cross-Page Join")
    print(
        "  Pages with continuations: "
        + str(summary.continuation_pages) + "/" + str(summary.total_pages)
    )

    # Per-fixture detail
    print("\n## Per-Fixture Detail")
    print(
        "  " + f"{'Fixture':<40}" + f"{'Exp':>5}" + f"{'Got':>5}"
        + f"{'Match':>7}" + f"{'Empty':>7}" + f"{'Cont':>6}"
        + f"{'Pages':>7}"
    )
    print(
        "  " + "-" * 40 + " " + "-" * 5 + " " + "-" * 5 + " "
        + "-" * 7 + " " + "-" * 7 + " " + "-" * 6 + " " + "-" * 7
    )
    for detail in summary.fixture_details:
        fname = detail["fixture_name"]
        if len(fname) > 38:
            fname = fname[:35] + "..."
        tag = ""
        if detail["is_known_issue"]:
            tag = " [KI]"
        elif detail["count_exact"]:
            tag = " OK"
        elif detail["count_off_by_one"]:
            tag = " ~1"
        else:
            tag = " ERR"
        print(
            "  " + f"{fname:<40}"
            + f"{detail['expected_case_count']:>5}"
            + f"{detail['actual_case_count']:>5}"
            + f"{tag:>7}"
            + f"{detail['empty_rulings']:>7}"
            + f"{detail['continuation_pages']:>6}"
            + f"{detail['page_count']:>7}"
        )

    if summary.errors:
        print("\nErrors (" + str(len(summary.errors)) + "):")
        for err in summary.errors:
            print("  " + err)

    print("\nCost estimate:")
    print("  Per fixture: $" + f"{summary.cost_per_fixture:.6f}")
    print("  Monthly (66 PDFs/day): $" + f"{summary.estimated_monthly_cost:.2f}")


def print_comparison_table(summaries: dict[str, ModelSummary]) -> None:
    """Print a side-by-side comparison table of all models."""
    model_names = list(summaries.keys())

    print("\n" + "=" * 80)
    print("COMPARISON TABLE: Per-Page Transcription Eval")
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
        scorable = (
            s.case_count_correct + s.case_count_off_by_one + s.case_count_wrong
        )
        pct = (s.case_count_correct / scorable * 100) if scorable > 0 else 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Case count lenient (off-by-1 OK)
    row = f"{'Case count lenient':<35}"
    for name in model_names:
        s = summaries[name]
        scorable = (
            s.case_count_correct + s.case_count_off_by_one + s.case_count_wrong
        )
        lenient = s.case_count_correct + s.case_count_off_by_one
        pct = (lenient / scorable * 100) if scorable > 0 else 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Non-empty ruling rate
    row = f"{'Non-empty ruling rate':<35}"
    for name in model_names:
        s = summaries[name]
        if s.total_rulings > 0:
            pct = (
                (s.total_rulings - s.empty_rulings) / s.total_rulings * 100
            )
        else:
            pct = 0
        row += f"  {pct:>19.1f}%"
    print(row)

    # Total pages
    row = f"{'Total pages processed':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.total_pages:>20}"
    print(row)

    # Continuations detected
    row = f"{'Continuation pages':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.continuation_pages:>20}"
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

    row = f"{'Monthly cost (66/day)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.estimated_monthly_cost:>18.2f}"
    print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the per-page multimodal extraction evaluation."""
    parser = argparse.ArgumentParser(
        description="Eval per-page transcription accuracy on OC PDF fixtures."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(MODELS.keys()),
        default=list(MODELS.keys()),
        help="Models to evaluate (default: all).",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to JSON file.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="DPI for PDF rendering (default: 150).",
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
            + ", ".join(google_models) + ")"
        )
        return 1
    if anthropic_models and not anthropic_key:
        print(
            "ERROR: ANTHROPIC_API_KEY not set (needed for: "
            + ", ".join(anthropic_models) + ")"
        )
        return 1

    # Load fixtures
    fixtures = get_oc_fixtures()
    print(
        "Found " + str(len(fixtures))
        + " OC PDF fixtures with ground truth\n"
    )
    for fp, exp in fixtures:
        desc = exp.get("_description", "")
        cc = exp.get("case_count", "?")
        print(
            "  " + fp.name + ": " + str(cc) + " cases -- " + desc[:70]
        )
    print()

    # Pre-render all PDFs to images (shared across models)
    print("Rendering PDFs to images...")
    fixture_images: dict[str, list[bytes]] = {}
    for fp, _ in fixtures:
        images = render_pdf_to_images(fp, dpi=args.dpi)
        fixture_images[fp.name] = images
        print("  " + fp.name + ": " + str(len(images)) + " pages")
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
            "MODEL: " + model_name + " (" + model_id
            + ") -- per-page transcription"
        )
        print("=" * 70 + "\n")

        results: list[FixtureResult] = []

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            images = fixture_images[fname]
            page_count = len(images)
            expected_case_count = expected.get("case_count", 0)

            print(
                "  " + fname + " (" + str(page_count) + " pages, "
                + str(expected_case_count) + " expected cases)"
            )

            fixture_result = FixtureResult(
                fixture_name=fname,
                model=model_name,
                expected=expected,
                expected_case_count=expected_case_count,
                page_count=page_count,
            )

            page_results: list[PageResult] = []
            total_inp = 0
            total_out = 0
            total_lat = 0.0

            for page_idx, image in enumerate(images):
                print(
                    "    page " + str(page_idx + 1) + "/"
                    + str(page_count) + "...",
                    end=" ", flush=True,
                )

                try:
                    if provider == "google":
                        call_fn = call_gemini_page
                    else:
                        call_fn = call_anthropic_page
                    extracted, inp_tok, out_tok, lat = _call_with_retry(
                        call_fn, model_id, image, api_key
                    )

                    rulings_raw = extracted.get("rulings", [])
                    # Filter to dicts only — LLM occasionally returns
                    # malformed entries (lists, strings, etc.)
                    rulings = [r for r in rulings_raw if isinstance(r, dict)]
                    pr = PageResult(
                        page_number=page_idx + 1,
                        rulings=rulings,
                        input_tokens=inp_tok,
                        output_tokens=out_tok,
                        latency_ms=lat,
                    )
                    page_results.append(pr)
                    total_inp += inp_tok
                    total_out += out_tok
                    total_lat += lat

                    # Summary of what was found on this page
                    n_rulings = len(rulings)
                    print(
                        str(n_rulings) + " rulings"
                        + " (" + str(inp_tok) + "+" + str(out_tok)
                        + " tok, " + f"{lat:.0f}" + "ms)"
                    )

                except Exception as e:
                    print("ERROR: " + str(e))
                    page_results.append(
                        PageResult(
                            page_number=page_idx + 1,
                            rulings=[],
                            error=str(e),
                        )
                    )

            # Join cross-page results
            fixture_result.page_results = page_results
            fixture_result.joined_rulings = join_page_results(page_results)
            fixture_result.actual_case_count = len(
                fixture_result.joined_rulings
            )
            fixture_result.total_input_tokens = total_inp
            fixture_result.total_output_tokens = total_out
            fixture_result.total_latency_ms = total_lat

            # Count heuristic continuations
            cont_count = sum(
                1 for pr in page_results
                if pr.rulings and pr.rulings[0].get(
                    "_heuristic_continued", False
                )
            )
            print(
                "    -> joined: "
                + str(fixture_result.actual_case_count)
                + " cases (expected " + str(expected_case_count) + ")"
                + ", " + str(cont_count) + " continuations detected"
            )

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
        output_path = RESULTS_DIR / "oc_multimodal_results.json"

        save_data = {
            "timestamp": datetime.now().isoformat(),
            "eval_type": "per_page_transcription",
            "dpi": args.dpi,
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
                    summary.case_count_correct / scorable * 100
                    if scorable > 0 else 0
                ),
                "total_rulings": summary.total_rulings,
                "empty_rulings": summary.empty_rulings,
                "continuation_pages": summary.continuation_pages,
                "total_pages": summary.total_pages,
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
