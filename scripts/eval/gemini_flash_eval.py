#!/usr/bin/env python3
"""Gemini Flash extraction evaluation — same v2 prompt as Haiku eval.

Compares Gemini 2.5 Flash and Flash Lite against Haiku on the same test
fixtures using identical prompt and comparison logic.

Originally built during the LLM extraction investigation (#418). Results feed
into cost/quality decisions for the ingestion pipeline.

Usage:
    # Set API key
    export GOOGLE_API_KEY="AIza..."

    # Run from repo root (or any directory — paths are resolved automatically)
    python3 scripts/eval/gemini_flash_eval.py

    # Results are saved to scripts/eval/results/gemini_flash_results.json

Requirements:
    pip install google-genai pymupdf
"""

import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google import genai

try:
    import fitz  # pymupdf

    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = REPO_ROOT / "packages" / "scraper-framework" / "tests" / "fixtures"
EXPECTED_DIR = FIXTURES_DIR / "expected"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

MODELS = {
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
}

DOC_FIELDS = ["judge_name", "hearing_date", "department", "case_count"]
RULING_FIELDS = ["case_number", "case_title", "outcome", "motion_type"]
ALL_FIELDS = DOC_FIELDS + RULING_FIELDS

# Same v2 prompt as Haiku eval
EXTRACTION_PROMPT = """You are a legal document field extractor for California court tentative rulings.

Extract structured fields from the provided court ruling document. The document may contain multiple rulings/cases.

## Document-level fields

Extract these once for the entire document:
- **judge_name**: The presiding judge. Look for "Judge X", "Hon. X", "Honorable X", "JUDICIAL OFFICER: X", or similar patterns. **If METADATA is provided, use the judge name from METADATA — it is authoritative.** Only extract from document text if no METADATA is available.
- **hearing_date**: The hearing date in ISO format (YYYY-MM-DD). Look for dates in headers like "Tentative Rulings for March 2, 2026" or "Date: 03/04/26". Note: "Date: 02/19/26" means 2026-02-19 (2-digit year).
- **department**: The court department code. **If METADATA is provided, use the department from METADATA — it is authoritative.** Otherwise use the code from the document header. Preserve leading zeros (e.g., "CM02" not "CM2").
- **case_count**: The total number of distinct cases/rulings in this document. **Read through the ENTIRE document to count.** Count methods:
  - If cases have case numbers, count the distinct case numbers.
  - If cases are listed by line number (e.g., #101, #102, ...), count the numbered entries.
  - If the document says "No Tentative Rulings", case_count is 0.
  - Do NOT stop counting after the first few cases — scroll through the entire document.
  - LA HTML documents: count the number of `<td>` cells containing case numbers like "22SMCV01940".

## Per-ruling fields

For EACH case/ruling in the document, extract:
- **case_number**: The case number WITHOUT any county prefix. California formats include:
  - LA: "22SMCV01940", "24NNCV02551"
  - OC: "25-01455183", "2024-01437598", "01157766" (probate 8-digit)
  - Riverside: "CVMV2507098", "CVPS2306157"
  - San Bernardino: "CIVSB2600093"
  - If the number has a 2-digit county prefix before a dash (like "30-2024-01393434"), REMOVE the county prefix and return "2024-01393434".
- **case_title**: The case caption. Normalize ALL CAPS to Title Case, keep "v." lowercase. Use a regular hyphen-minus (-), not an en-dash or em-dash.
- **outcome**: The ruling outcome. Use EXACTLY one of these values:
  - "GRANTED" — motion/petition fully granted, sustained (for demurrers), or approved
  - "DENIED" — motion/petition fully denied or overruled (for demurrers). If a motion is denied on ALL grounds, use "DENIED" even if the ruling discusses multiple arguments.
  - "GRANTED IN PART" — some relief granted, some denied
  - "DENIED IN PART" — partially denied
  - "MOOT" — motion rendered moot
  - "CONTINUED" — hearing explicitly continued/rescheduled to a specific future date
  - "OFF CALENDAR" — hearing taken off calendar, dropped, vacated, or will not be heard. Key phrases: "off calendar", "taken off calendar", "vacated", "dropped". This is NOT the same as "continued" — off calendar means no future hearing is scheduled.
  - "SUBMITTED" — taken under submission for later decision
  - null — no clear outcome stated, or the document is just a calendar/list with no rulings
- **motion_type**: The type of motion/petition. Return the text as it appears in the document (e.g., "Motion for Summary Judgment", "Demurrer", "Request for Order", "Motion for Judgment on the Pleadings").

## Special cases

- If the document is an error page, contains no ruling content, or says "No Tentative Rulings": set case_count to 0 and rulings to an empty array. Still extract available metadata (judge, department, date).
- For documents where cases are listed by line number without case numbers (e.g., "101. Smith v. Jones"), extract case_title from the heading and set case_number to null.

## Output format

Return ONLY valid JSON (no markdown fences, no explanation):
{
  "judge_name": "string or null",
  "hearing_date": "YYYY-MM-DD or null",
  "department": "string or null",
  "case_count": integer,
  "rulings": [
    {
      "case_number": "string or null",
      "case_title": "string or null",
      "outcome": "string or null",
      "motion_type": "string or null"
    }
  ]
}"""


@dataclass
class EvalResult:
    fixture_name: str
    model: str
    extracted: dict
    expected: dict
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers (same normalization as Haiku eval)
# ---------------------------------------------------------------------------


def extract_html_ruling_text(html: str) -> str:
    """Extract ruling content from LA court HTML, stripping boilerplate."""
    idx = html.find("speechSynthesis")
    if idx > 0:
        start = html.rfind("<div", 0, idx)
        if start < 0:
            start = max(0, idx - 200)
        text = html[start:]
    else:
        text = html
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return text


def load_fixture_text(fixture_path: Path) -> str | None:
    """Load fixture content as text. Handles HTML and PDF."""
    suffix = fixture_path.suffix.lower()
    if suffix == ".html":
        raw = fixture_path.read_text(encoding="utf-8", errors="replace")
        return extract_html_ruling_text(raw)
    elif suffix == ".pdf":
        if not HAS_PYMUPDF:
            return None
        doc = fitz.open(str(fixture_path))
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n".join(text_parts)
    return None


def get_evaluable_fixtures() -> list[tuple[Path, dict]]:
    """Get fixtures that have expected outputs with extractable fields."""
    fixtures = []
    skip_files = {
        "la_main_page.json",
        "la_ruling_response.json",
        "oc_civil_page.json",
        "oc_family_law_page.json",
        "oc_probate_page.json",
        "riv_page.json",
        "sb_civil_page.json",
        "sb_iframe_page.json",
        "sc_landing_page.json",
        "sc_dept1_page.json",
        "sc_dept6_page.json",
        "sc_dept16_page.json",
        "sf_family_law_page.json",
        "sf_captcha_gate.json",
    }
    for expected_file in sorted(EXPECTED_DIR.glob("*.json")):
        if expected_file.name in skip_files:
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
        fixtures.append((fixture_path, expected))
    return fixtures


def normalize_unicode(s: str) -> str:
    """Replace unicode dashes, quotes, etc. with ASCII equivalents."""
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    return unicodedata.normalize("NFKD", s)


def normalize_value(val: object) -> str | None:
    """Normalize a value to a comparable string or None."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    return normalize_unicode(s)


def normalize_case_number(val: str | None) -> str | None:
    """Normalize case number: remove county prefix, spaces."""
    if val is None:
        return None
    s = str(val).strip().replace(" ", "")
    m = re.match(r"^\d{2}-(\d{4}-\d+)$", s)
    if m:
        s = m.group(1)
    return s


def normalize_department(val: str | None) -> str | None:
    """Normalize department to uppercase."""
    if val is None:
        return None
    return str(val).strip().upper()


def normalize_date(val: str | None) -> str | None:
    """Normalize date values to ISO format for comparison."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    for fmt in ["%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y"]:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def normalize_outcome(val: str | None) -> str | None:
    """Normalize outcome values for comparison."""
    if val is None:
        return None
    s = normalize_unicode(str(val)).strip().upper()
    mapping = {
        "GRANTED": "GRANTED",
        "DENIED": "DENIED",
        "GRANTED IN PART": "GRANTED IN PART",
        "GRANTED_IN_PART": "GRANTED IN PART",
        "DENIED IN PART": "DENIED IN PART",
        "DENIED_IN_PART": "DENIED IN PART",
        "OFF CALENDAR": "OFF CALENDAR",
        "OFF_CALENDAR": "OFF CALENDAR",
        "CONTINUED": "CONTINUED",
        "MOOT": "MOOT",
        "SUBMITTED": "SUBMITTED",
    }
    return mapping.get(s, s)


def normalize_motion_type(val: str | None) -> str | None:
    """Normalize motion type to lowercase canonical form."""
    if val is None:
        return None
    s = normalize_unicode(str(val)).strip().lower()
    s = re.sub(r"^motions?\s+", "motion ", s)
    s = s.replace("_", " ")
    return s


def normalize_judge(val: str | None) -> str | None:
    """Normalize judge name: remove honorific prefix, lowercase."""
    if val is None:
        return None
    s = str(val).strip()
    for prefix in [
        "Hon. ",
        "HON. ",
        "Hon ",
        "HON ",
        "Honorable ",
        "HONORABLE ",
        "Judge ",
        "JUDGE ",
        "Commissioner ",
    ]:
        if s.startswith(prefix):
            s = s[len(prefix) :]
    return s.strip().lower()


def compare_field(field_name: str, extracted_val: object, expected_val: object) -> bool:
    """Compare an extracted value against an expected value."""
    ext = normalize_value(str(extracted_val) if extracted_val is not None else None)
    exp = normalize_value(str(expected_val) if expected_val is not None else None)
    if ext is None and exp is None:
        return True
    if ext is None or exp is None:
        return False
    if field_name == "judge_name":
        return normalize_judge(ext) == normalize_judge(exp)
    if field_name == "hearing_date":
        return normalize_date(ext) == normalize_date(exp)
    if field_name == "department":
        return normalize_department(ext) == normalize_department(exp)
    if field_name == "case_number":
        return normalize_case_number(ext) == normalize_case_number(exp)
    if field_name == "case_count":
        try:
            return int(ext) == int(exp)
        except (ValueError, TypeError):
            return False
    if field_name == "outcome":
        return normalize_outcome(ext) == normalize_outcome(exp)
    if field_name == "motion_type":
        return normalize_motion_type(ext) == normalize_motion_type(exp)
    if field_name == "case_title":
        e1 = (
            normalize_unicode(ext)
            .lower()
            .replace("vs.", "v.")
            .replace(" vs ", " v. ")
            .strip()
        )
        e2 = (
            normalize_unicode(exp)
            .lower()
            .replace("vs.", "v.")
            .replace(" vs ", " v. ")
            .strip()
        )
        return e1 == e2 or e1 in e2 or e2 in e1
    return ext.lower() == exp.lower()


def get_expected_field_value(expected: dict, field_name: str) -> object:
    """Get the expected value for a field from the expected JSON."""
    if field_name == "case_number":
        return expected.get("primary_case_number")
    return expected.get(field_name)


# ---------------------------------------------------------------------------
# Gemini API call
# ---------------------------------------------------------------------------


def build_user_message(text: str, expected: dict) -> str:
    """Build the user message with document text and optional metadata hints."""
    parts = [EXTRACTION_PROMPT, "\n\n---\n"]
    link_text = expected.get("_link_text")
    if link_text:
        parts.append(
            "\nMETADATA (from court website"
            " — this is the authoritative source for judge and department):"
        )
        parts.append(f"  Link text: {link_text}")
        judge_from_link = expected.get("judge_name")
        dept_from_link = expected.get("department")
        if judge_from_link:
            parts.append(f"  Judge: {judge_from_link}")
        if dept_from_link:
            parts.append(f"  Department: {dept_from_link}")
        parts.append("")
    max_chars = 100000
    if len(text) > max_chars:
        parts.append(
            f"DOCUMENT TEXT (truncated from {len(text)} to {max_chars} chars):\n"
            f"{text[:max_chars]}"
        )
    else:
        parts.append(f"DOCUMENT TEXT:\n{text}")
    return "\n".join(parts)


def call_gemini(
    client: genai.Client,
    model_id: str,
    text: str,
    expected: dict,
) -> tuple[dict, int, int, float]:
    """Call Gemini API and return (parsed_result, input_tokens, output_tokens, latency_ms)."""
    user_msg = build_user_message(text, expected)
    start = time.monotonic()

    response = client.models.generate_content(
        model=model_id,
        contents=user_msg,
        config={
            "temperature": 0.0,
            "max_output_tokens": 8192,
        },
    )
    latency = (time.monotonic() - start) * 1000

    usage = response.usage_metadata
    input_tokens = usage.prompt_token_count or 0
    output_tokens = usage.candidates_token_count or 0

    response_text = response.text.strip()

    # Strip markdown code fences if present
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
            print(f"\n    PARSE ERROR: {response_text[:200]}")
            result = {}

    return result, input_tokens, output_tokens, latency


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

KNOWN_FIXTURE_ISSUES = {
    ("oc_north_n.pdf", "judge_name"),
    ("oc_north_n.pdf", "department"),
    ("oc_north_n.pdf", "case_count"),
    ("oc_family_law_claustro_c22.pdf", "outcome"),
    ("oc_family_law_kohler_l69.pdf", "outcome"),
    ("oc_probate_cm3.pdf", "outcome"),
}


def analyze_results(results: list[EvalResult], model_label: str, pricing: dict) -> dict:
    """Analyze and print results for a single model. Returns summary dict."""
    valid = [r for r in results if r.error is None]
    if not valid:
        print(f"  No valid results for {model_label}")
        return {}

    total_in = sum(r.input_tokens for r in valid)
    total_out = sum(r.output_tokens for r in valid)
    avg_latency = sum(r.latency_ms for r in valid) / len(valid)

    print(f"Fixtures processed: {len(valid)}")
    print(f"Total tokens: {total_in} input + {total_out} output")
    print(f"Avg latency: {avg_latency:.0f}ms")

    field_correct: dict[str, int] = {}
    field_total: dict[str, int] = {}
    mismatches: list[tuple[str, str, str, str]] = []

    for r in valid:
        extracted = r.extracted
        rulings = extracted.get("rulings", [])
        first_ruling = rulings[0] if rulings else {}

        for fld in ALL_FIELDS:
            exp_val = get_expected_field_value(r.expected, fld)
            if fld not in r.expected:
                if fld == "case_number" and "primary_case_number" not in r.expected:
                    continue
                elif fld != "case_number":
                    continue

            if exp_val is None:
                ext_val_check = (
                    extracted.get(fld) if fld in DOC_FIELDS else first_ruling.get(fld)
                )
                if (
                    ext_val_check is not None
                    and normalize_value(str(ext_val_check)) is not None
                ):
                    field_total[fld] = field_total.get(fld, 0) + 1
                    field_correct[fld] = field_correct.get(fld, 0) + 1
                    continue

            if fld in DOC_FIELDS:
                ext_val = extracted.get(fld)
            else:
                ext_val = first_ruling.get(fld)

            field_total[fld] = field_total.get(fld, 0) + 1
            correct = compare_field(fld, ext_val, exp_val)
            if correct:
                field_correct[fld] = field_correct.get(fld, 0) + 1
            else:
                mismatches.append((r.fixture_name, fld, str(exp_val), str(ext_val)))

    print("\nPer-field accuracy:")
    print(f"  {'Field':<25} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 10}")
    for fld in ALL_FIELDS:
        total = field_total.get(fld, 0)
        correct = field_correct.get(fld, 0)
        acc = (correct / total * 100) if total > 0 else 0
        print(f"  {fld:<25} {correct:>8} {total:>8} {acc:>9.1f}%")

    total_all = sum(field_total.values())
    correct_all = sum(field_correct.values())
    overall_acc = (correct_all / total_all * 100) if total_all > 0 else 0
    print(f"  {'OVERALL':<25} {correct_all:>8} {total_all:>8} {overall_acc:>9.1f}%")

    fixture_issues = []
    off_by_one = []
    real_errors = []

    for fixture, fld, exp, got in mismatches:
        if (fixture, fld) in KNOWN_FIXTURE_ISSUES:
            fixture_issues.append((fixture, fld, exp, got))
        elif fld == "case_count":
            try:
                if abs(int(exp) - int(got)) <= 1:
                    off_by_one.append((fixture, fld, exp, got))
                else:
                    real_errors.append((fixture, fld, exp, got))
            except (ValueError, TypeError):
                real_errors.append((fixture, fld, exp, got))
        else:
            real_errors.append((fixture, fld, exp, got))

    if mismatches:
        print(f"\nMismatches ({len(mismatches)}):")
        for fixture, fld, exp, got in mismatches:
            tag = ""
            if (fixture, fld) in KNOWN_FIXTURE_ISSUES:
                tag = " [FIXTURE]"
            elif fld == "case_count":
                try:
                    if abs(int(exp) - int(got)) <= 1:
                        tag = " [OFF-BY-1]"
                except (ValueError, TypeError):
                    pass
            print(f"  {fixture}: {fld} expected={exp!r} got={got!r}{tag}")

        print(
            f"\n  Fixture issues: {len(fixture_issues)},"
            f" Off-by-1: {len(off_by_one)},"
            f" Real errors: {len(real_errors)}"
        )

        adjusted_correct = correct_all + len(fixture_issues)
        adjusted_acc = (adjusted_correct / total_all * 100) if total_all > 0 else 0
        lenient_correct = adjusted_correct + len(off_by_one)
        lenient_acc = (lenient_correct / total_all * 100) if total_all > 0 else 0
        print(f"  Adjusted accuracy: {adjusted_acc:.1f}%")
        print(f"  Lenient accuracy: {lenient_acc:.1f}%")

    # Cost
    avg_in = total_in / len(valid)
    avg_out = total_out / len(valid)
    cost_per = (avg_in * pricing["input"] + avg_out * pricing["output"]) / 1_000_000
    monthly = cost_per * 6000
    print(f"\nCost: ${cost_per:.6f}/ruling, ${monthly:.2f}/month (6k rulings)")

    return {
        "overall_acc": overall_acc,
        "adjusted_acc": (
            (correct_all + len(fixture_issues)) / total_all * 100 if total_all else 0
        ),
        "lenient_acc": (
            (correct_all + len(fixture_issues) + len(off_by_one)) / total_all * 100
            if total_all
            else 0
        ),
        "avg_latency_ms": avg_latency,
        "cost_monthly": monthly,
        "avg_in_tokens": avg_in,
        "avg_out_tokens": avg_out,
        "real_errors": len(real_errors),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_evaluation() -> None:
    """Run the Gemini Flash extraction evaluation."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not set")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    fixtures = get_evaluable_fixtures()
    print(f"Found {len(fixtures)} evaluable fixtures\n")

    for fp, exp in fixtures:
        desc = exp.get("_description", "")
        print(f"  {fp.name}: {desc[:80]}")
    print()

    # Pricing per million tokens
    pricing = {
        "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
        "gemini-2.5-flash-lite": {"input": 0.075, "output": 0.30},
    }

    summaries = {}

    for model_name, model_id in MODELS.items():
        results: list[EvalResult] = []

        print(f"\n{'=' * 70}")
        print(f"MODEL: {model_name} ({model_id})")
        print(f"{'=' * 70}\n")

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            print(f"  Processing {fname}...", end=" ", flush=True)

            text = load_fixture_text(fixture_path)
            if text is None:
                print("SKIP")
                continue

            try:
                result, inp_tok, out_tok, lat = call_gemini(
                    client, model_id, text, expected
                )
                print(f"OK ({inp_tok}+{out_tok} tokens, {lat:.0f}ms)")
                results.append(
                    EvalResult(
                        fixture_name=fname,
                        model=model_name,
                        extracted=result,
                        expected=expected,
                        input_tokens=inp_tok,
                        output_tokens=out_tok,
                        latency_ms=lat,
                    )
                )
            except Exception as e:
                print(f"ERROR: {e}")
                results.append(
                    EvalResult(
                        fixture_name=fname,
                        model=model_name,
                        extracted={},
                        expected=expected,
                        error=str(e),
                    )
                )

        print(f"\n--- {model_name} ANALYSIS ---\n")
        summaries[model_name] = analyze_results(
            results, model_name, pricing[model_name]
        )

    # ---------------------------------------------------------------------------
    # Comparison table
    # ---------------------------------------------------------------------------
    print(f"\n\n{'=' * 70}")
    print("COMPARISON: Gemini Flash vs Haiku (from previous eval)")
    print(f"{'=' * 70}\n")

    # Haiku numbers from last v2 eval run
    haiku_summary = {
        "overall_acc": 88.9,
        "adjusted_acc": 94.9,
        "lenient_acc": 99.0,
        "avg_latency_ms": 3177,
        "cost_monthly": 47.23,
        "avg_in_tokens": 7992,
        "avg_out_tokens": 370,
    }

    print(f"  {'Metric':<30} {'Haiku':>12}", end="")
    for name in MODELS:
        print(f"  {name:>24}", end="")
    print()
    print(f"  {'-' * 30} {'-' * 12}", end="")
    for _ in MODELS:
        print(f"  {'-' * 24}", end="")
    print()

    metrics = [
        ("Raw accuracy", "overall_acc", ".1f", "%"),
        ("Adjusted accuracy", "adjusted_acc", ".1f", "%"),
        ("Lenient accuracy", "lenient_acc", ".1f", "%"),
        ("Avg latency (ms)", "avg_latency_ms", ".0f", "ms"),
        ("Monthly cost (6k)", "cost_monthly", ".2f", "$"),
    ]

    for label, key, fmt, unit in metrics:
        h_val = haiku_summary.get(key, 0)
        prefix = "$" if unit == "$" else ""
        suffix = unit if unit != "$" else ""
        print(f"  {label:<30} {prefix}{h_val:{fmt}}{suffix:>4}", end="")
        for name in MODELS:
            s = summaries.get(name, {})
            v = s.get(key, 0)
            print(f"  {prefix}{v:{fmt}}{suffix:>4}{'':>12}", end="")
        print()

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "gemini_flash_results.json"
    output_path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "summaries": summaries,
                "haiku_reference": haiku_summary,
            },
            indent=2,
            default=str,
        )
    )
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    run_evaluation()
