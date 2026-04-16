#!/usr/bin/env python3
"""LLM-based field extraction evaluation v1.

Runs test fixtures through Haiku, Sonnet, and Opus to evaluate accuracy,
confidence calibration, latency, cost, and determinism.

This is the original v1 evaluation script. For production use, prefer the v2
eval (haiku_eval_v2.py) which has improved prompts and normalization.

Originally built during the LLM extraction investigation (#418).

Usage:
    # Set API key
    export ANTHROPIC_API_KEY="sk-ant-..."

    # Run from repo root (or any directory — paths are resolved automatically)
    python3 scripts/eval/haiku_eval_v1.py

    # Results are saved to scripts/eval/results/haiku_v1_results.json

Requirements:
    pip install anthropic pymupdf
"""

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic

try:
    from judgemind_config import DEFAULT_HAIKU_MODEL as _HAIKU_MODEL
except ImportError:
    _HAIKU_MODEL = "claude-haiku-4-5-20251001"

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

# Models to test
MODELS = {
    "haiku": _HAIKU_MODEL,
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

# Fields we want to extract
TARGET_FIELDS = [
    "judge_name",
    "hearing_date",
    "department",
    "case_number",  # maps to primary_case_number in expected
    "case_title",
    "outcome",
    "motion_type",
    "case_count",
]

# The extraction prompt
EXTRACTION_PROMPT = """You are a legal document field extractor for California court tentative rulings.

Extract the following structured fields from the provided court ruling text. For each field, provide:
1. The extracted value (or null if not present/extractable)
2. A confidence score from 0.0 to 1.0

Fields to extract:
- judge_name: The name of the presiding judge (e.g. "William A. Crowfoot", "Bobby P. Luna"). Look for patterns like "Judge X", "Hon. X", "Honorable X", "Presiding: X", "JUDICIAL OFFICER: X", or "X Judge of the Superior Court". Do NOT extract party names or organization names as judge names.
- hearing_date: The date of the hearing. Return in ISO format (YYYY-MM-DD) if possible, otherwise the exact text.
- department: The court department number/code (e.g. "205", "F46", "C25", "PS1", "S22", "CM3").
- case_number: The primary case number. California formats include: "24NNCV02551" (LA), "CIVRS2502080" (SB), "CVPS2306157" (Riverside), "25-01455183" (OC), "24CV443183" (Santa Clara), "FPT-25-378624" (SF), "24D006789" (OC Family), "01157766" (OC Probate).
- case_title: The case caption/title, e.g. "Smith v. Jones". If all caps, normalize to title case but keep "v." lowercase.
- outcome: The ruling outcome. Must be one of: granted, denied, granted_in_part, denied_in_part, moot, continued, off_calendar, submitted, or null.
- motion_type: The type of motion. Use these codes when possible: msj (summary judgment), msj_partial (summary adjudication), mtd (motion to dismiss), mil (motion in limine), demurrer, motion_to_compel, motion_to_strike, anti_slapp, preliminary_injunction, ex_parte_application, petition_writ_of_mandate, petition_habeas_corpus, petition, osc, motion_to_quash, motion_for_reconsideration, motion_for_protective_order, motion_for_attorney_fees, motion_to_set_aside_default, motion_to_vacate. If none match exactly, return the motion type text from the document.
- case_count: The number of cases/rulings in this document (integer).

IMPORTANT:
- If the document is an error page, access denied page, or contains no ruling content, set all fields to null and case_count to 0.
- If the document says "No Tentative Rulings", set case_count to 0 and extract whatever metadata is available (judge, department, date).
- For documents with multiple cases, extract fields from the FIRST case only (except case_count which should be total).
- Normalize SB case numbers by removing internal spaces (e.g. "CIVSB 2600093" -> "CIVSB2600093").

Return ONLY a valid JSON object with this exact structure:
{
  "judge_name": {"value": "string or null", "confidence": 0.0-1.0},
  "hearing_date": {"value": "string or null", "confidence": 0.0-1.0},
  "department": {"value": "string or null", "confidence": 0.0-1.0},
  "case_number": {"value": "string or null", "confidence": 0.0-1.0},
  "case_title": {"value": "string or null", "confidence": 0.0-1.0},
  "outcome": {"value": "string or null", "confidence": 0.0-1.0},
  "motion_type": {"value": "string or null", "confidence": 0.0-1.0},
  "case_count": {"value": 0, "confidence": 0.0-1.0}
}"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    fixture_name: str
    model: str
    extracted: dict  # field -> {"value": ..., "confidence": ...}
    expected: dict  # from the expected JSON
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class DeterminismRun:
    fixture_name: str
    run_number: int
    extracted: dict
    input_tokens: int = 0
    output_tokens: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_fixture_text(fixture_path: Path) -> str | None:
    """Load fixture content as text. Handles HTML and PDF."""
    suffix = fixture_path.suffix.lower()
    if suffix == ".html":
        return fixture_path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".pdf":
        if not HAS_PYMUPDF:
            print(f"  SKIP {fixture_path.name}: pymupdf not installed")
            return None
        doc = fitz.open(str(fixture_path))
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n".join(text_parts)
    return None


def get_evaluable_fixtures() -> list[tuple[Path, dict]]:
    """Get fixtures that have expected outputs with extractable fields.

    Skip error pages, access denied pages, and index/landing pages that
    don't contain ruling content.
    """
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
        if expected.get("_edge_case") == "stale_viewstate":
            continue
        if expected.get("_edge_case") == "access_denied":
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


def normalize_value(val: str | None) -> str | None:
    """Normalize a value for comparison."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "null" or s.lower() == "none":
        return None
    return s


def normalize_outcome(val: str | None) -> str | None:
    """Normalize outcome values for comparison."""
    if val is None:
        return None
    s = str(val).strip().lower().replace(" ", "_").replace("-", "_")
    mapping = {
        "off_calendar": "off_calendar",
        "off calendar": "off_calendar",
        "granted_in_part": "granted_in_part",
        "denied_in_part": "denied_in_part",
    }
    return mapping.get(s, s)


def normalize_date(val: str | None) -> str | None:
    """Normalize date values for comparison."""
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


def compare_field(field_name: str, extracted_val: object, expected_val: object) -> bool:
    """Compare an extracted value against an expected value."""
    ext = normalize_value(str(extracted_val) if extracted_val is not None else None)
    exp = normalize_value(str(expected_val) if expected_val is not None else None)

    if ext is None and exp is None:
        return True
    if ext is None or exp is None:
        return False

    if field_name == "outcome":
        return normalize_outcome(ext) == normalize_outcome(exp)

    if field_name == "hearing_date":
        return normalize_date(ext) == normalize_date(exp)

    if field_name == "case_count":
        try:
            return int(ext) == int(exp)
        except (ValueError, TypeError):
            return False

    if field_name == "case_number":
        return ext.replace(" ", "") == exp.replace(" ", "")

    if field_name == "judge_name":
        e1 = ext.lower().replace("hon. ", "").replace("hon ", "").strip()
        e2 = exp.lower().replace("hon. ", "").replace("hon ", "").strip()
        return e1 == e2

    if field_name == "case_title":
        e1 = ext.lower().replace("vs.", "v.").replace(" vs ", " v. ").strip()
        e2 = exp.lower().replace("vs.", "v.").replace(" vs ", " v. ").strip()
        return e1 == e2 or e1 in e2 or e2 in e1

    if field_name == "motion_type":
        e1 = ext.lower().strip()
        e2 = exp.lower().strip()
        if e1 == e2:
            return True
        motion_map = {
            "summary judgment": "msj",
            "motion for summary judgment": "msj",
            "summary adjudication": "msj_partial",
            "motion to dismiss": "mtd",
            "motion in limine": "mil",
            "motion to compel": "motion_to_compel",
            "motion to strike": "motion_to_strike",
            "preliminary injunction": "preliminary_injunction",
            "ex parte application": "ex_parte_application",
            "motion for protective order": "motion_for_protective_order",
            "motion for attorney fees": "motion_for_attorney_fees",
            "motions for judgment on the pleadings": "motion_for_judgment_on_pleadings",
        }
        return motion_map.get(e1, e1) == e2 or e1 == motion_map.get(e2, e2)

    return ext.lower() == exp.lower()


def get_expected_field_value(expected: dict, field_name: str) -> object:
    """Get the expected value for a field from the expected JSON."""
    if field_name == "case_number":
        return expected.get("primary_case_number")
    return expected.get(field_name)


def call_anthropic(
    client: anthropic.Anthropic, model_id: str, text: str
) -> tuple[dict, int, int, float]:
    """Call Anthropic API and return (parsed_result, input_tokens, output_tokens, latency_ms)."""
    start = time.monotonic()
    message = client.messages.create(
        model=model_id,
        max_tokens=1024,
        temperature=0.0,
        messages=[
            {
                "role": "user",
                "content": f"{EXTRACTION_PROMPT}\n\n---\n\nDOCUMENT TEXT:\n{text[:15000]}",
            }
        ],
    )
    latency = (time.monotonic() - start) * 1000

    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens

    response_text = message.content[0].text.strip()
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
            result = {}

    return result, input_tokens, output_tokens, latency


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------


def run_evaluation() -> None:
    """Run the v1 multi-model extraction evaluation."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    fixtures = get_evaluable_fixtures()
    print(f"Found {len(fixtures)} evaluable fixtures\n")

    for fp, exp in fixtures:
        desc = exp.get("_description", "")
        print(f"  {fp.name}: {desc[:80]}")
    print()

    # ---------------------------------------------------------------------------
    # Phase 1: Run all fixtures through all models
    # ---------------------------------------------------------------------------
    all_results: list[ExtractionResult] = []

    for model_name, model_id in MODELS.items():
        print(f"\n{'=' * 70}")
        print(f"MODEL: {model_name} ({model_id})")
        print(f"{'=' * 70}\n")

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            print(f"  Processing {fname}...", end=" ", flush=True)

            text = load_fixture_text(fixture_path)
            if text is None:
                print("SKIP (no text)")
                continue

            try:
                result, inp_tok, out_tok, latency = call_anthropic(
                    client, model_id, text
                )
                print(f"OK ({inp_tok}+{out_tok} tokens, {latency:.0f}ms)")
                all_results.append(
                    ExtractionResult(
                        fixture_name=fname,
                        model=model_name,
                        extracted=result,
                        expected=expected,
                        input_tokens=inp_tok,
                        output_tokens=out_tok,
                        latency_ms=latency,
                    )
                )
            except Exception as e:
                print(f"ERROR: {e}")
                all_results.append(
                    ExtractionResult(
                        fixture_name=fname,
                        model=model_name,
                        extracted={},
                        expected=expected,
                        error=str(e),
                    )
                )

    # ---------------------------------------------------------------------------
    # Phase 2: Determinism test -- run Haiku 5x on a subset
    # ---------------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print("DETERMINISM TEST: Haiku x5 on subset")
    print(f"{'=' * 70}\n")

    content_fixtures = [
        (fp, exp) for fp, exp in fixtures if exp.get("case_count", 0) > 0
    ][:5]

    determinism_runs: dict[str, list[DeterminismRun]] = {}

    for fixture_path, expected in content_fixtures:
        fname = fixture_path.name
        determinism_runs[fname] = []
        text = load_fixture_text(fixture_path)
        if text is None:
            continue

        for run_num in range(5):
            print(f"  {fname} run {run_num + 1}/5...", end=" ", flush=True)
            try:
                result, inp_tok, out_tok, _ = call_anthropic(
                    client, MODELS["haiku"], text
                )
                print(f"OK ({inp_tok}+{out_tok})")
                determinism_runs[fname].append(
                    DeterminismRun(
                        fixture_name=fname,
                        run_number=run_num,
                        extracted=result,
                        input_tokens=inp_tok,
                        output_tokens=out_tok,
                    )
                )
            except Exception as e:
                print(f"ERROR: {e}")

    # ---------------------------------------------------------------------------
    # Analysis
    # ---------------------------------------------------------------------------
    print(f"\n\n{'=' * 70}")
    print("RESULTS ANALYSIS")
    print(f"{'=' * 70}\n")

    for model_name in MODELS:
        model_results = [
            r for r in all_results if r.model == model_name and r.error is None
        ]
        if not model_results:
            continue

        print(f"\n--- {model_name.upper()} ---")
        print(f"Fixtures processed: {len(model_results)}")

        total_tokens_in = sum(r.input_tokens for r in model_results)
        total_tokens_out = sum(r.output_tokens for r in model_results)
        avg_latency = sum(r.latency_ms for r in model_results) / len(model_results)

        print(f"Total tokens: {total_tokens_in} input + {total_tokens_out} output")
        print(f"Avg latency: {avg_latency:.0f}ms")

        field_correct: dict[str, int] = {}
        field_total: dict[str, int] = {}
        field_confident_correct: dict[str, int] = {}
        field_confident_total: dict[str, int] = {}
        field_low_conf_correct: dict[str, int] = {}
        field_low_conf_total: dict[str, int] = {}
        mismatches: list[tuple[str, str, str, str]] = []

        for r in model_results:
            for fld in TARGET_FIELDS:
                exp_val = get_expected_field_value(r.expected, fld)
                ext_data = r.extracted.get(fld, {})
                if isinstance(ext_data, dict):
                    ext_val = ext_data.get("value")
                    confidence = ext_data.get("confidence", 0.5)
                else:
                    ext_val = ext_data
                    confidence = 0.5

                if fld not in r.expected and fld != "case_number":
                    if fld == "case_number" and "primary_case_number" not in r.expected:
                        continue
                    elif fld != "case_number":
                        continue

                field_total[fld] = field_total.get(fld, 0) + 1
                correct = compare_field(fld, ext_val, exp_val)
                if correct:
                    field_correct[fld] = field_correct.get(fld, 0) + 1

                if confidence >= 0.8:
                    field_confident_total[fld] = field_confident_total.get(fld, 0) + 1
                    if correct:
                        field_confident_correct[fld] = (
                            field_confident_correct.get(fld, 0) + 1
                        )
                else:
                    field_low_conf_total[fld] = field_low_conf_total.get(fld, 0) + 1
                    if correct:
                        field_low_conf_correct[fld] = (
                            field_low_conf_correct.get(fld, 0) + 1
                        )

                if not correct:
                    mismatches.append((r.fixture_name, fld, str(exp_val), str(ext_val)))

        print("\nPer-field accuracy:")
        print(f"  {'Field':<25} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
        print(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 10}")
        for fld in TARGET_FIELDS:
            total = field_total.get(fld, 0)
            correct = field_correct.get(fld, 0)
            acc = (correct / total * 100) if total > 0 else 0
            print(f"  {fld:<25} {correct:>8} {total:>8} {acc:>9.1f}%")

        total_all = sum(field_total.values())
        correct_all = sum(field_correct.values())
        overall_acc = (correct_all / total_all * 100) if total_all > 0 else 0
        print(f"  {'OVERALL':<25} {correct_all:>8} {total_all:>8} {overall_acc:>9.1f}%")

        print("\nConfidence calibration:")
        print(
            f"  {'Field':<25} {'Hi-conf correct':>16}"
            f" {'Hi-conf total':>14}"
            f" {'Lo-conf correct':>16}"
            f" {'Lo-conf total':>14}"
        )
        for fld in TARGET_FIELDS:
            hc = field_confident_correct.get(fld, 0)
            ht = field_confident_total.get(fld, 0)
            lc = field_low_conf_correct.get(fld, 0)
            lt = field_low_conf_total.get(fld, 0)
            ha = f"{hc / ht * 100:.0f}%" if ht > 0 else "n/a"
            la = f"{lc / lt * 100:.0f}%" if lt > 0 else "n/a"
            print(f"  {fld:<25} {hc:>8}/{ht:<6} ({ha:>4})  {lc:>8}/{lt:<6} ({la:>4})")

        if mismatches:
            print(f"\nMismatches ({len(mismatches)}):")
            for fixture, fld, exp, got in mismatches[:20]:
                print(f"  {fixture}: {fld} expected={exp!r} got={got!r}")
            if len(mismatches) > 20:
                print(f"  ... and {len(mismatches) - 20} more")

    # ---------------------------------------------------------------------------
    # Determinism analysis
    # ---------------------------------------------------------------------------
    print("\n\n--- DETERMINISM (Haiku x5) ---")
    for fname, runs in determinism_runs.items():
        if len(runs) < 2:
            continue
        print(f"\n  {fname}:")
        for fld in TARGET_FIELDS:
            values = []
            for run in runs:
                ext_data = run.extracted.get(fld, {})
                if isinstance(ext_data, dict):
                    values.append(str(ext_data.get("value")))
                else:
                    values.append(str(ext_data))
            unique = set(values)
            status = (
                "CONSISTENT" if len(unique) == 1 else f"VARIED ({len(unique)} values)"
            )
            if len(unique) > 1:
                print(f"    {fld}: {status}: {unique}")
            else:
                print(f"    {fld}: {status}")

    # ---------------------------------------------------------------------------
    # Cost projections
    # ---------------------------------------------------------------------------
    print("\n\n--- COST PROJECTIONS ---")
    print("Based on 200 rulings/day (6,000/month)\n")

    pricing = {
        "haiku": {"input": 0.80, "output": 4.00},
        "sonnet": {"input": 3.00, "output": 15.00},
        "opus": {"input": 15.00, "output": 75.00},
    }

    for model_name in MODELS:
        model_results = [
            r for r in all_results if r.model == model_name and r.error is None
        ]
        if not model_results:
            continue
        avg_in = sum(r.input_tokens for r in model_results) / len(model_results)
        avg_out = sum(r.output_tokens for r in model_results) / len(model_results)
        p = pricing[model_name]
        cost_per_ruling = (avg_in * p["input"] + avg_out * p["output"]) / 1_000_000
        monthly_cost = cost_per_ruling * 6000
        print(f"  {model_name.upper()}:")
        print(f"    Avg tokens/ruling: {avg_in:.0f} input + {avg_out:.0f} output")
        print(f"    Cost/ruling: ${cost_per_ruling:.6f}")
        print(f"    Monthly (6k rulings): ${monthly_cost:.2f}")

    # Tiered cost scenarios
    print("\n  Tiered scenarios (monthly):")
    haiku_results = [r for r in all_results if r.model == "haiku" and r.error is None]
    sonnet_results = [r for r in all_results if r.model == "sonnet" and r.error is None]
    opus_results = [r for r in all_results if r.model == "opus" and r.error is None]

    if haiku_results and sonnet_results and opus_results:
        h_avg_in = sum(r.input_tokens for r in haiku_results) / len(haiku_results)
        h_avg_out = sum(r.output_tokens for r in haiku_results) / len(haiku_results)
        s_avg_in = sum(r.input_tokens for r in sonnet_results) / len(sonnet_results)
        s_avg_out = sum(r.output_tokens for r in sonnet_results) / len(sonnet_results)
        o_avg_in = sum(r.input_tokens for r in opus_results) / len(opus_results)
        o_avg_out = sum(r.output_tokens for r in opus_results) / len(opus_results)

        h_cost = (
            h_avg_in * pricing["haiku"]["input"]
            + h_avg_out * pricing["haiku"]["output"]
        ) / 1_000_000
        s_cost = (
            s_avg_in * pricing["sonnet"]["input"]
            + s_avg_out * pricing["sonnet"]["output"]
        ) / 1_000_000
        o_cost = (
            o_avg_in * pricing["opus"]["input"] + o_avg_out * pricing["opus"]["output"]
        ) / 1_000_000

        scenarios = [
            ("All Haiku", 1.0, 0.0, 0.0),
            ("80% Haiku + 20% Sonnet", 0.8, 0.2, 0.0),
            ("70% Haiku + 25% Sonnet + 5% Opus", 0.7, 0.25, 0.05),
            ("50% Haiku + 40% Sonnet + 10% Opus", 0.5, 0.4, 0.1),
            ("All Sonnet", 0.0, 1.0, 0.0),
            ("All Opus", 0.0, 0.0, 1.0),
        ]

        for name, h_pct, s_pct, o_pct in scenarios:
            total = 6000 * (h_pct * h_cost + s_pct * s_cost + o_pct * o_cost)
            print(f"    {name:<45} ${total:.2f}/mo")

    # ---------------------------------------------------------------------------
    # Save raw results to JSON
    # ---------------------------------------------------------------------------
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "haiku_v1_results.json"
    raw_output: dict = {
        "timestamp": datetime.now().isoformat(),
        "fixtures_count": len(fixtures),
        "results": [],
        "determinism": {},
    }

    for r in all_results:
        raw_output["results"].append(
            {
                "fixture": r.fixture_name,
                "model": r.model,
                "extracted": r.extracted,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "latency_ms": r.latency_ms,
                "error": r.error,
            }
        )

    for fname, runs in determinism_runs.items():
        raw_output["determinism"][fname] = [
            {"run": dr.run_number, "extracted": dr.extracted} for dr in runs
        ]

    output_path.write_text(json.dumps(raw_output, indent=2, default=str))
    print(f"\nRaw results saved to: {output_path}")


if __name__ == "__main__":
    run_evaluation()
