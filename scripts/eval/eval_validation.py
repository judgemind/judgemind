#!/usr/bin/env python3
"""Eval LLM-based validation of ingested ruling fields.

Tests a validation prompt that checks extracted ruling fields for common
ingestion errors (wrong text assigned, motion type in title, suspiciously
short text, etc.) against known-good and known-bad test cases.

Two modes:
  --live     Call the LLM API for each test case, save results to cache.
  --cached   Load cached results and score (no API calls, CI-friendly).

Usage:
    export GOOGLE_API_KEY="AIza..."
    export ANTHROPIC_API_KEY="sk-ant-..."

    # Run live eval against all models
    python3 scripts/eval/eval_validation.py --live

    # Run live eval for specific model
    python3 scripts/eval/eval_validation.py --live --models gemini-2.5-flash-lite

    # Score cached results (no API calls)
    python3 scripts/eval/eval_validation.py --cached

    # Compare models side by side
    python3 scripts/eval/eval_validation.py --cached --compare

Requirements:
    pip install google-genai anthropic
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
RESULTS_DIR = SCRIPT_DIR / "results"

sys.path.insert(0, str(SCRIPT_DIR))

from eval_validation_scorer import (  # noqa: E402
    EvalSummary,
    ValidationResult,
    format_report,
    score_results,
)
from eval_validation_test_cases import TestCase, get_all_test_cases  # noqa: E402

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
# Validation prompt
# ---------------------------------------------------------------------------

VALIDATION_SYSTEM_PROMPT = """\
You are a quality validator for California court tentative ruling data.

You will receive the extracted fields from an ingested ruling. Your job is to
check for common ingestion errors and return a structured verdict.

## Common Error Patterns

1. **Wrong text assigned**: The ruling_text does not match what the motion_type
   and case context suggest. For example, a "motion to compel" case should NOT
   have ruling text about an unrelated grant with no opposition. A complex
   motion (summary judgment, demurrer, motion to compel) should typically have
   a substantive ruling, not a one-liner.

2. **Motion type in case_title**: The case_title field should contain ONLY
   party names (e.g., "Smith v. Jones"). If the case_title contains words
   like "Motion", "Demurrer", "Hearing", "Responses To", "Petition", or
   other procedural terms after the party names, this is a data error —
   the motion description was incorrectly concatenated with the title.

3. **Cross-reference as full ruling**: A ruling_text of "See #N Above" or
   similar is valid ONLY when it is a companion case cross-referencing
   another entry on the same calendar. However, if the case appears to be
   a primary case (not a companion), "See #N Above" as the sole text
   indicates the real ruling text was lost.

4. **Suspiciously short ruling for complex motion**: Some motion types
   (summary judgment, demurrer, motion for new trial) require substantial
   legal analysis. A ruling_text under 50 characters for these motions is
   suspicious — it may indicate truncated or wrong text.

5. **Empty ruling text**: ruling_text is empty or null when other fields are
   present. This always indicates a data error.

## When NOT to flag

- "Motion withdrawn" or "Matter taken off calendar" rulings are legitimately
  short, regardless of motion type. These are PASS.
- "No tentative ruling, the matter will be heard as scheduled" is legitimate.
- Cross-references ("See #N Above") are legitimate when the case_number and
  case_title differ from the referenced case (it's a companion case).
- Short rulings for simple motions (ex parte applications, motions to
  continue) are normal and should PASS.

## Output format

Respond with ONLY a JSON object:
{
  "result": "pass" | "flag" | "fail",
  "reason": "Brief explanation of the verdict"
}

Verdicts:
- "pass": No issues detected, the data appears correct.
- "flag": Likely data quality issue detected. Explain what looks wrong.
- "fail": Definite data error detected (empty text, clearly corrupted data).
"""


def build_validation_input(tc: TestCase) -> str:
    """Build the user message for a validation call from a TestCase."""
    parts = ["## Extracted Fields", ""]

    if tc.county:
        parts.append("county: " + tc.county)
    if tc.case_number:
        parts.append("case_number: " + tc.case_number)
    if tc.case_title:
        parts.append("case_title: " + tc.case_title)
    if tc.motion_type:
        parts.append("motion_type: " + tc.motion_type)
    if tc.judge_name:
        parts.append("judge_name: " + tc.judge_name)
    if tc.hearing_date:
        parts.append("hearing_date: " + tc.hearing_date)
    if tc.department:
        parts.append("department: " + tc.department)
    if tc.outcome:
        parts.append("outcome: " + tc.outcome)

    parts.append("")
    parts.append("## Ruling Text")
    parts.append("")

    if tc.ruling_text:
        # Truncate to first 500 chars for cost efficiency
        text = tc.ruling_text
        if len(text) > 500:
            text = (
                text[:500]
                + " [... truncated, "
                + str(len(tc.ruling_text))
                + " chars total]"
            )
        parts.append(text)
    else:
        parts.append("[EMPTY]")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

MAX_RETRIES = 3


def call_gemini(
    model_id: str,
    system_prompt: str,
    user_message: str,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Gemini API and return (parsed_json, input_tokens, output_tokens, latency_ms)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0,
        max_output_tokens=256,
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
            result = {
                "result": "error",
                "reason": "Failed to parse: " + response_text[:100],
            }

    return result, input_tokens, output_tokens, latency


def call_anthropic(
    model_id: str,
    system_prompt: str,
    user_message: str,
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Anthropic API and return (parsed_json, input_tokens, output_tokens, latency_ms)."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    start = time.monotonic()
    response = client.messages.create(
        model=model_id,
        max_tokens=256,
        temperature=0,
        system=system_prompt,
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

    # Strip markdown code fences if present
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
            result = {
                "result": "error",
                "reason": "Failed to parse: " + response_text[:100],
            }

    return result, input_tokens, output_tokens, latency


def call_with_retry(
    call_fn: object,
    *args: object,
    max_retries: int = MAX_RETRIES,
) -> tuple[dict, int, int, float]:
    """Retry wrapper for LLM API calls."""
    for attempt in range(max_retries):
        try:
            return call_fn(*args)
        except Exception as e:
            err_str = str(e)
            if ("503" in err_str or "429" in err_str) and attempt < max_retries - 1:
                wait = 2**attempt
                print(
                    "retrying (" + str(attempt + 1) + ")...",
                    end=" ",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise

    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Live evaluation
# ---------------------------------------------------------------------------


def run_live_eval(
    model_name: str,
    test_cases: list[TestCase],
) -> list[ValidationResult]:
    """Run live validation eval for a single model."""
    model_config = MODELS[model_name]
    provider = model_config["provider"]
    model_id = model_config["model_id"]

    if provider == "google":
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        call_fn = call_gemini
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        call_fn = call_anthropic

    if not api_key:
        print("ERROR: API key not set for " + provider)
        return []

    results: list[ValidationResult] = []

    for tc in test_cases:
        user_message = build_validation_input(tc)

        print(
            "  " + tc.id + " (" + tc.category + ")...",
            end=" ",
            flush=True,
        )

        vr = ValidationResult(
            test_case_id=tc.id,
            category=tc.category,
            expected_verdict=tc.expected_verdict,
            actual_verdict="error",
            bug_pattern=tc.bug_pattern,
        )

        try:
            parsed, inp_tok, out_tok, lat = call_with_retry(
                call_fn, model_id, VALIDATION_SYSTEM_PROMPT, user_message, api_key
            )

            vr.actual_verdict = parsed.get("result", "error")
            vr.reason = parsed.get("reason", "")
            vr.input_tokens = inp_tok
            vr.output_tokens = out_tok
            vr.latency_ms = lat

            # Normalize verdict
            if vr.actual_verdict not in ("pass", "flag", "fail"):
                vr.actual_verdict = "error"
                vr.error = "Invalid verdict: " + str(parsed.get("result"))

            marker = ""
            if tc.category == "known_bad" and vr.actual_verdict == "pass":
                marker = " MISS"
            elif (
                tc.category in ("known_good", "edge_case")
                and vr.actual_verdict != "pass"
            ):
                marker = " FP"

            print(
                vr.actual_verdict
                + " ["
                + str(inp_tok)
                + "+"
                + str(out_tok)
                + " tok, "
                + f"{lat:.0f}"
                + "ms]"
                + marker
            )

        except Exception as e:
            vr.error = str(e)
            print("ERROR: " + str(e))

        results.append(vr)

    return results


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def save_results(
    results: list[ValidationResult],
    model_name: str,
) -> Path:
    """Save results to JSON cache file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / ("validation_" + model_name.replace(".", "_") + ".json")

    data = {
        "model": model_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "results": [],
    }

    for r in results:
        data["results"].append(
            {
                "test_case_id": r.test_case_id,
                "category": r.category,
                "expected_verdict": r.expected_verdict,
                "actual_verdict": r.actual_verdict,
                "reason": r.reason,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "bug_pattern": r.bug_pattern,
            }
        )

    output_path.write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )
    return output_path


def load_results(model_name: str) -> list[ValidationResult] | None:
    """Load cached results for a model. Returns None if cache missing."""
    cache_path = RESULTS_DIR / ("validation_" + model_name.replace(".", "_") + ".json")
    if not cache_path.exists():
        return None

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    results = []
    for entry in data["results"]:
        results.append(
            ValidationResult(
                test_case_id=entry["test_case_id"],
                category=entry["category"],
                expected_verdict=entry["expected_verdict"],
                actual_verdict=entry["actual_verdict"],
                reason=entry.get("reason", ""),
                input_tokens=entry.get("input_tokens", 0),
                output_tokens=entry.get("output_tokens", 0),
                latency_ms=entry.get("latency_ms", 0.0),
                error=entry.get("error"),
                bug_pattern=entry.get("bug_pattern"),
            )
        )

    return results


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


def print_comparison(summaries: dict[str, EvalSummary]) -> None:
    """Print side-by-side comparison of multiple models."""
    model_names = list(summaries.keys())

    print("\n" + "=" * 80)
    print("COMPARISON TABLE: Validation Prompt Eval")
    print("=" * 80 + "\n")

    header = f"{'Metric':<35}"
    for name in model_names:
        header += f"  {name:>20}"
    print(header)
    print("-" * 35 + "".join("  " + "-" * 20 for _ in model_names))

    # Detection rate
    row = f"{'Detection rate (known-bad)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.detection_rate:>19.0%}"
    print(row)

    # False positive rate
    row = f"{'False positive rate (overall)':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.overall_false_positive_rate:>19.0%}"
    print(row)

    # Cost per call
    row = f"{'Cost per validation call':<35}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.cost_per_call:>18.6f}"
    print(row)

    # Monthly cost
    row = f"{'Monthly (1k rulings/day)':<35}"
    for name in model_names:
        s = summaries[name]
        monthly = s.cost_per_call * 1000 * 30
        row += f"  ${monthly:>18.2f}"
    print(row)

    # Passes thresholds
    row = f"{'Passes thresholds?':<35}"
    for name in model_names:
        s = summaries[name]
        status = "YES" if s.passes_thresholds else "NO"
        row += f"  {status:>20}"
    print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the validation prompt eval."""
    parser = argparse.ArgumentParser(
        description="Eval validation prompt against known-good and known-bad ingestions."
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Run live eval with API calls, save results to cache.",
    )
    mode_group.add_argument(
        "--cached",
        action="store_true",
        help="Score cached results (no API calls).",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(MODELS.keys()),
        default=list(MODELS.keys()),
        help="Models to evaluate (default: all).",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Print comparison table of all models.",
    )
    args = parser.parse_args()

    test_cases = get_all_test_cases()
    print("Test cases: " + str(len(test_cases)) + " total")
    for category in ("known_good", "known_bad", "edge_case"):
        count = len([tc for tc in test_cases if tc.category == category])
        print("  " + category + ": " + str(count))
    print()

    all_summaries: dict[str, EvalSummary] = {}

    for model_name in args.models:
        model_config = MODELS[model_name]
        pricing = model_config["pricing_per_m"]

        print("\n" + "=" * 70)
        print("MODEL: " + model_name)
        print("=" * 70 + "\n")

        if args.live:
            results = run_live_eval(model_name, test_cases)
            if not results:
                print("No results (missing API key?)")
                continue
            path = save_results(results, model_name)
            print("\nResults saved to: " + str(path))
        else:
            results = load_results(model_name)
            if results is None:
                print("No cached results found. Run with --live first.")
                continue
            print("Loaded " + str(len(results)) + " cached results")

        summary = score_results(results, model_name, pricing)
        report = format_report(summary)
        print("\n" + report)
        all_summaries[model_name] = summary

    # Comparison
    if args.compare and len(all_summaries) > 1:
        print_comparison(all_summaries)

    # Overall pass/fail
    if all_summaries:
        print("\n" + "=" * 70)
        any_pass = False
        for name, summary in all_summaries.items():
            status = "PASS" if summary.passes_thresholds else "FAIL"
            if summary.passes_thresholds:
                any_pass = True
            print(
                name
                + ": "
                + status
                + " (detection="
                + f"{summary.detection_rate:.0%}"
                + ", FP="
                + f"{summary.overall_false_positive_rate:.0%}"
                + ", cost=$"
                + f"{summary.cost_per_call:.6f}"
                + "/call)"
            )

        if not any_pass:
            print("\nNo model meets acceptance criteria.")
            return 1
        print("\nAt least one model meets acceptance criteria.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
