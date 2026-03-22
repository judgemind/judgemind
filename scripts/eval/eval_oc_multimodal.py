#!/usr/bin/env python3
"""Eval multimodal extraction accuracy on OC PDF fixtures.

Renders OC PDF fixtures to page images and sends them to three models:
  - Gemini 2.5 Flash Lite
  - Gemini 2.5 Flash
  - Claude Haiku 4.5

Compares extraction results against expected ground truth and reports
per-field accuracy, per-fixture accuracy, and cost per model.

Usage:
    # Both API keys required (or just the ones for models you want to run)
    export GOOGLE_API_KEY="AIza..."
    export ANTHROPIC_API_KEY="sk-ant-..."

    # Run all models (default)
    python3 scripts/eval/eval_oc_multimodal.py

    # Run specific model(s)
    python3 scripts/eval/eval_oc_multimodal.py --models gemini-2.5-flash-lite

    # Save results to JSON (in addition to console output)
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

# Fields to evaluate
DOC_FIELDS = ["judge_name", "hearing_date", "department", "case_count"]
RULING_FIELDS = ["case_number", "case_title", "outcome"]
ALL_FIELDS = DOC_FIELDS + RULING_FIELDS

# Known fixture issues — mismatches caused by fixture ground truth inconsistencies
KNOWN_FIXTURE_ISSUES: set[tuple[str, str]] = {
    ("oc_north_n.pdf", "judge_name"),
    ("oc_north_n.pdf", "department"),
    ("oc_north_n.pdf", "case_count"),
    ("oc_family_law_claustro_c22.pdf", "outcome"),
}

# Skip fixtures that are index pages, not actual ruling documents
SKIP_FIXTURES = {
    "oc_civil_page.json",
    "oc_family_law_page.json",
    "oc_probate_page.json",
}

# System prompt — reuses the same prompt from the production extraction pipeline
# (framework/llm_schema.py EXTRACTION_SYSTEM_PROMPT), adapted for image input
MULTIMODAL_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings.\n\n"
    "You will receive page images from a court ruling PDF document. "
    "Extract ALL structured fields into JSON.\n\n"
    "## Rules\n\n"
    "1. **Multi-ruling documents:** A single document may "
    "contain rulings for MANY cases (sometimes 10+). You MUST "
    "return an array of ALL cases found in the entire document. "
    "Read through ALL page images systematically — do not "
    "stop after the first few cases.\n\n"
    "2. **Multi-department documents:** Some documents contain "
    "rulings from multiple departments. Extract ALL rulings from ALL "
    "departments. The top-level judge_name and department should "
    "reflect the FIRST department in the document.\n\n"
    "3. **Outcome taxonomy** — use EXACTLY one of these values:\n"
    "   - granted\n"
    "   - denied\n"
    "   - granted_in_part\n"
    "   - denied_in_part\n"
    "   - moot\n"
    "   - continued\n"
    "   - off_calendar\n"
    "   - submitted\n"
    "   - other (only if none of the above fit)\n\n"
    "4. **Case number normalization:** Strip any 2-digit county "
    "prefix before the year. For example, "
    '"30-2024-01393434" becomes "2024-01393434". '
    "Keep the full number for other formats.\n\n"
    "5. **Department normalization:** Preserve the department "
    "identifier exactly as it appears, INCLUDING leading zeros.\n\n"
    "6. **Parties:** Extract plaintiff(s) and defendant(s) "
    "from case captions.\n\n"
    "7. **Case type:** Classify using: civil, criminal, family, "
    "probate, small_claims, juvenile, traffic, other.\n\n"
    "8. **Motion type:** Use a short descriptive label.\n\n"
    "9. **Hearing date:** Return as ISO format YYYY-MM-DD.\n\n"
    "10. **Judge name:** Extract the judge's full name without "
    'titles like "Hon.", "Judge", or "Commissioner".\n\n'
    "11. If metadata is provided (judge_name, department), "
    "treat it as authoritative.\n\n"
    "12. **Ruling text:** Transcribe ruling text VERBATIM from "
    "the images. Do not summarize or rephrase.\n\n"
    "13. **case_count:** The total number of distinct cases/rulings "
    "in this document. Read through ALL pages to count.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "C25" or "N14" or "CM02" or null,\n'
    '  "case_count": integer,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "2024-01393434" or null,\n'
    '      "extracted_case_title": "Smith v. Jones" or null,\n'
    '      "case_type": "civil" or null,\n'
    '      "outcome": "granted" or null,\n'
    '      "motion_type": "msj" or null,\n'
    '      "ruling_text": "The motion is GRANTED..." or null,\n'
    '      "extracted_parties": [\n'
    '        {"name": "John Smith", "role": "plaintiff"},\n'
    '        {"name": "Jane Jones", "role": "defendant"}\n'
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FixtureResult:
    """Result from running a single fixture through a model."""

    fixture_name: str
    model: str
    extracted: dict
    expected: dict
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    page_count: int = 0
    error: str | None = None


@dataclass
class ModelSummary:
    """Aggregated results for a single model."""

    model: str
    total_fixtures: int = 0
    field_correct: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_pages: int = 0
    avg_latency_ms: float = 0.0
    cost_per_fixture: float = 0.0
    estimated_monthly_cost: float = 0.0
    mismatches: list[tuple[str, str, str, str]] = field(default_factory=list)
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
    """Render PDF pages to PNG images using pymupdf.

    Args:
        pdf_path: Path to the PDF file.
        dpi: Resolution for rendering. 150 balances quality vs token cost.
        max_pages: Maximum pages to render.

    Returns:
        List of PNG image bytes, one per page.
    """
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


# ---------------------------------------------------------------------------
# Normalization helpers (adapted from eval_scorer.py / gemini_flash_eval.py)
# ---------------------------------------------------------------------------


def normalize_unicode(s: str) -> str:
    """Replace common Unicode variants with ASCII equivalents."""
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    return s


def normalize_value(val: object) -> str | None:
    """Normalize a value for comparison."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    return normalize_unicode(s)


def normalize_case_number(val: str | None) -> str | None:
    """Normalize case number: remove county prefix."""
    if val is None:
        return None
    s = str(val).strip().replace(" ", "")
    m = re.match(r"^\d{2,3}-(.+)$", s)
    if m:
        return m.group(1)
    return s


def normalize_department(val: str | None) -> str | None:
    """Normalize department to uppercase."""
    if val is None:
        return None
    return str(val).strip().upper()


def normalize_date(val: str | None) -> str | None:
    """Normalize date values to ISO format."""
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


def normalize_judge(val: str | None) -> str | None:
    """Normalize judge name: remove honorific, lowercase."""
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
            break
    return normalize_unicode(s.lower().strip())


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


def compare_field(
    field_name: str,
    extracted_val: object,
    expected_val: object,
) -> bool:
    """Compare an extracted value against expected with field-specific normalization."""
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

    if field_name in ("case_number", "primary_case_number"):
        return normalize_case_number(ext) == normalize_case_number(exp)

    if field_name == "case_count":
        try:
            return int(ext) == int(exp)
        except (ValueError, TypeError):
            return False

    if field_name == "outcome":
        return normalize_outcome(ext) == normalize_outcome(exp)

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
        if not fixture_path.suffix == ".pdf":
            continue
        fixtures.append((fixture_path, expected))
    return fixtures


# ---------------------------------------------------------------------------
# Model API calls (multimodal)
# ---------------------------------------------------------------------------


def call_gemini_multimodal(
    model_id: str,
    images: list[bytes],
    metadata: dict[str, str],
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Gemini API with page images and return structured extraction.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # Build multimodal content: all page images + text prompt
    parts: list[types.Part] = []
    for img_bytes in images:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    # Add metadata and extraction prompt
    text_parts = []
    if metadata:
        meta_lines = []
        if metadata.get("judge_name"):
            meta_lines.append("Judge name (authoritative): " + metadata["judge_name"])
        if metadata.get("department"):
            meta_lines.append("Department (authoritative): " + metadata["department"])
        if meta_lines:
            text_parts.append("Metadata from scraper:\n" + "\n".join(meta_lines))
    text_parts.append(
        "Extract all structured fields from these court ruling page images."
    )
    parts.append(types.Part.from_text(text="\n\n".join(text_parts)))

    config = types.GenerateContentConfig(
        system_instruction=MULTIMODAL_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=8192,
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


def call_anthropic_multimodal(
    model_id: str,
    images: list[bytes],
    metadata: dict[str, str],
    api_key: str,
) -> tuple[dict, int, int, float]:
    """Call Anthropic API with page images and return structured extraction.

    Returns: (parsed_result, input_tokens, output_tokens, latency_ms)
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    # Build content blocks: all images first, then text prompt
    content: list[dict] = []
    for img_bytes in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(img_bytes).decode("ascii"),
                },
            }
        )

    # Add metadata and extraction prompt
    text_parts = []
    if metadata:
        meta_lines = []
        if metadata.get("judge_name"):
            meta_lines.append("Judge name (authoritative): " + metadata["judge_name"])
        if metadata.get("department"):
            meta_lines.append("Department (authoritative): " + metadata["department"])
        if meta_lines:
            text_parts.append("Metadata from scraper:\n" + "\n".join(meta_lines))
    text_parts.append(
        "Extract all structured fields from these court ruling page images."
    )
    content.append({"type": "text", "text": "\n\n".join(text_parts)})

    start = time.monotonic()
    response = client.messages.create(
        model=model_id,
        max_tokens=8192,
        temperature=0,
        system=MULTIMODAL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    latency = (time.monotonic() - start) * 1000

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    # Find the text content block
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
            print(f"\n    PARSE ERROR: {response_text[:200]}")
            result = {}

    return result, input_tokens, output_tokens, latency


# ---------------------------------------------------------------------------
# Field comparison and scoring
# ---------------------------------------------------------------------------


def get_expected_field_value(expected: dict, field_name: str) -> object:
    """Get the expected value for a field from the expected JSON."""
    if field_name == "case_number":
        return expected.get("primary_case_number")
    return expected.get(field_name)


def score_fixture(
    result: FixtureResult,
) -> dict[str, dict]:
    """Score a fixture result, returning per-field match/mismatch info."""
    scores: dict[str, dict] = {}
    extracted = result.extracted
    expected = result.expected
    rulings = extracted.get("rulings", [])
    first_ruling = rulings[0] if rulings else {}

    for fld in DOC_FIELDS:
        exp_val = get_expected_field_value(expected, fld)
        if fld not in expected:
            continue
        if exp_val is None:
            continue

        if fld == "judge_name":
            ext_val = extracted.get("extracted_judge_name") or extracted.get(
                "judge_name"
            )
        elif fld == "case_count":
            ext_val = extracted.get("case_count")
            if ext_val is None:
                ext_val = len(rulings)
        else:
            ext_val = extracted.get(fld)

        match = compare_field(fld, ext_val, exp_val)
        is_known = (result.fixture_name, fld) in KNOWN_FIXTURE_ISSUES
        scores[fld] = {
            "expected": str(exp_val),
            "extracted": str(ext_val),
            "match": match,
            "known_issue": is_known,
        }

    for fld in RULING_FIELDS:
        if fld == "case_number":
            exp_val = expected.get("primary_case_number")
            if exp_val is None and "primary_case_number" not in expected:
                continue
        else:
            exp_val = expected.get(fld)
            if fld not in expected:
                continue

        if exp_val is None:
            continue

        if fld == "case_number":
            ext_val = first_ruling.get("extracted_case_number") or first_ruling.get(
                "case_number"
            )
        elif fld == "case_title":
            ext_val = first_ruling.get("extracted_case_title") or first_ruling.get(
                "case_title"
            )
        else:
            ext_val = first_ruling.get(fld)

        match = compare_field(fld, ext_val, exp_val)
        is_known = (result.fixture_name, fld) in KNOWN_FIXTURE_ISSUES
        scores[fld] = {
            "expected": str(exp_val),
            "extracted": str(ext_val),
            "match": match,
            "known_issue": is_known,
        }

    return scores


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
        summary.total_pages += r.page_count

        field_scores = score_fixture(r)
        for fld, score in field_scores.items():
            summary.field_total.setdefault(fld, 0)
            summary.field_correct.setdefault(fld, 0)
            summary.field_total[fld] += 1

            if score["match"]:
                summary.field_correct[fld] += 1
            else:
                summary.mismatches.append(
                    (r.fixture_name, fld, score["expected"], score["extracted"])
                )

    for r in results:
        if r.error:
            summary.errors.append(f"{r.fixture_name}: {r.error}")

    latencies = [r.latency_ms for r in valid]
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
    print(f"\n--- {summary.model} ---\n")
    print(f"Fixtures processed: {summary.total_fixtures}")
    print(
        f"Total tokens: {summary.total_input_tokens:,} input + "
        f"{summary.total_output_tokens:,} output"
    )
    print(f"Total pages rendered: {summary.total_pages}")
    print(f"Avg latency: {summary.avg_latency_ms:,.0f}ms")

    print("\nPer-field accuracy:")
    print(f"  {'Field':<25} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 10}")

    total_correct = 0
    total_total = 0
    for fld in ALL_FIELDS:
        total = summary.field_total.get(fld, 0)
        correct = summary.field_correct.get(fld, 0)
        acc = (correct / total * 100) if total > 0 else 0
        print(f"  {fld:<25} {correct:>8} {total:>8} {acc:>9.1f}%")
        total_correct += correct
        total_total += total

    overall = (total_correct / total_total * 100) if total_total > 0 else 0
    print(f"  {'OVERALL':<25} {total_correct:>8} {total_total:>8} {overall:>9.1f}%")

    # Known issue breakdown
    fixture_issues = [
        m for m in summary.mismatches if (m[0], m[1]) in KNOWN_FIXTURE_ISSUES
    ]
    off_by_one = []
    real_errors = []
    for fixture, fld, exp, got in summary.mismatches:
        if (fixture, fld) in KNOWN_FIXTURE_ISSUES:
            continue
        if fld == "case_count":
            try:
                if abs(int(exp) - int(got)) <= 1:
                    off_by_one.append((fixture, fld, exp, got))
                    continue
            except (ValueError, TypeError):
                pass
        real_errors.append((fixture, fld, exp, got))

    if summary.mismatches:
        print(f"\nMismatches ({len(summary.mismatches)}):")
        for fixture, fld, exp, got in summary.mismatches:
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

        adjusted = total_correct + len(fixture_issues)
        adjusted_acc = (adjusted / total_total * 100) if total_total > 0 else 0
        lenient = adjusted + len(off_by_one)
        lenient_acc = (lenient / total_total * 100) if total_total > 0 else 0
        print(
            f"\n  Fixture issues: {len(fixture_issues)},"
            f" Off-by-1: {len(off_by_one)},"
            f" Real errors: {len(real_errors)}"
        )
        print(f"  Adjusted accuracy: {adjusted_acc:.1f}%")
        print(f"  Lenient accuracy: {lenient_acc:.1f}%")

    if summary.errors:
        print(f"\nErrors ({len(summary.errors)}):")
        for err in summary.errors:
            print(f"  {err}")

    print("\nCost estimate:")
    print(f"  Per fixture: ${summary.cost_per_fixture:.6f}")
    print(f"  Monthly (66 PDFs/day): ${summary.estimated_monthly_cost:.2f}")


def print_comparison_table(
    summaries: dict[str, ModelSummary],
) -> None:
    """Print a side-by-side comparison table of all models."""
    model_names = list(summaries.keys())

    print(f"\n{'=' * 80}")
    print("COMPARISON TABLE: Multimodal OC Extraction")
    print(f"{'=' * 80}\n")

    # Header
    header = f"{'Metric':<30}"
    for name in model_names:
        header += f"  {name:>20}"
    print(header)
    print(f"{'-' * 30}" + "".join(f"  {'-' * 20}" for _ in model_names))

    # Fixtures processed
    row = f"{'Fixtures processed':<30}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.total_fixtures:>20}"
    print(row)

    # Per-field accuracy
    for fld in ALL_FIELDS:
        row = f"{fld + ' accuracy':<30}"
        for name in model_names:
            s = summaries[name]
            total = s.field_total.get(fld, 0)
            correct = s.field_correct.get(fld, 0)
            acc = (correct / total * 100) if total > 0 else 0
            row += f"  {acc:>19.1f}%"
        print(row)

    # Overall accuracy
    row = f"{'Overall accuracy':<30}"
    for name in model_names:
        s = summaries[name]
        total = sum(s.field_total.values())
        correct = sum(s.field_correct.values())
        acc = (correct / total * 100) if total > 0 else 0
        row += f"  {acc:>19.1f}%"
    print(row)

    # Token counts
    row = f"{'Avg input tokens':<30}"
    for name in model_names:
        s = summaries[name]
        avg = s.total_input_tokens / s.total_fixtures if s.total_fixtures else 0
        row += f"  {avg:>20,.0f}"
    print(row)

    row = f"{'Avg output tokens':<30}"
    for name in model_names:
        s = summaries[name]
        avg = s.total_output_tokens / s.total_fixtures if s.total_fixtures else 0
        row += f"  {avg:>20,.0f}"
    print(row)

    # Latency
    row = f"{'Avg latency (ms)':<30}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.avg_latency_ms:>20,.0f}"
    print(row)

    # Cost
    row = f"{'Cost per fixture':<30}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.cost_per_fixture:>18.6f}"
    print(row)

    row = f"{'Monthly cost (66/day)':<30}"
    for name in model_names:
        s = summaries[name]
        row += f"  ${s.estimated_monthly_cost:>18.2f}"
    print(row)

    # Total pages
    row = f"{'Total pages rendered':<30}"
    for name in model_names:
        s = summaries[name]
        row += f"  {s.total_pages:>20}"
    print(row)


def generate_markdown_summary(
    summaries: dict[str, ModelSummary],
) -> str:
    """Generate a Markdown summary table for posting as a GitHub comment."""
    model_names = list(summaries.keys())

    lines = [
        "## Multimodal OC Extraction Eval Results",
        "",
        "Sent OC PDF fixtures as page images to three models for structured extraction. "
        "Compared against ground truth expected values.",
        "",
    ]

    # Build table header
    header = "| Metric |"
    separator = "|---|"
    for name in model_names:
        header += f" {name} |"
        separator += "---|"
    lines.append(header)
    lines.append(separator)

    # Fixtures
    row = "| Fixtures processed |"
    for name in model_names:
        s = summaries[name]
        row += f" {s.total_fixtures} |"
    lines.append(row)

    # Per-field accuracy
    for fld in ALL_FIELDS:
        row = f"| {fld} accuracy |"
        for name in model_names:
            s = summaries[name]
            total = s.field_total.get(fld, 0)
            correct = s.field_correct.get(fld, 0)
            acc = (correct / total * 100) if total > 0 else 0
            row += f" {acc:.0f}% ({correct}/{total}) |"
        lines.append(row)

    # Overall
    row = "| **Overall accuracy** |"
    for name in model_names:
        s = summaries[name]
        total = sum(s.field_total.values())
        correct = sum(s.field_correct.values())
        acc = (correct / total * 100) if total > 0 else 0
        row += f" **{acc:.1f}%** ({correct}/{total}) |"
    lines.append(row)

    # Token usage
    row = "| Avg input tokens |"
    for name in model_names:
        s = summaries[name]
        avg = s.total_input_tokens / s.total_fixtures if s.total_fixtures else 0
        row += f" {avg:,.0f} |"
    lines.append(row)

    row = "| Avg output tokens |"
    for name in model_names:
        s = summaries[name]
        avg = s.total_output_tokens / s.total_fixtures if s.total_fixtures else 0
        row += f" {avg:,.0f} |"
    lines.append(row)

    # Latency
    row = "| Avg latency |"
    for name in model_names:
        s = summaries[name]
        row += f" {s.avg_latency_ms:,.0f}ms |"
    lines.append(row)

    # Cost
    row = "| Cost per fixture |"
    for name in model_names:
        s = summaries[name]
        row += f" ${s.cost_per_fixture:.6f} |"
    lines.append(row)

    row = "| **Monthly cost** (66 PDFs/day) |"
    for name in model_names:
        s = summaries[name]
        row += f" **${s.estimated_monthly_cost:.2f}** |"
    lines.append(row)

    # Mismatches detail
    lines.append("")
    lines.append("### Mismatches")
    lines.append("")

    for name in model_names:
        s = summaries[name]
        if not s.mismatches:
            lines.append(f"**{name}**: No mismatches")
            continue

        lines.append(f"**{name}** ({len(s.mismatches)} mismatches):")
        for fixture, fld, exp, got in s.mismatches:
            tag = ""
            if (fixture, fld) in KNOWN_FIXTURE_ISSUES:
                tag = " `[FIXTURE ISSUE]`"
            elif fld == "case_count":
                try:
                    if abs(int(exp) - int(got)) <= 1:
                        tag = " `[OFF-BY-1]`"
                except (ValueError, TypeError):
                    pass
            lines.append(f"- `{fixture}`: {fld} expected=`{exp}` got=`{got}`{tag}")
        lines.append("")

    # Recommendation
    lines.append("### Recommendation")
    lines.append("")

    # Find best model by accuracy
    best_model = None
    best_acc = 0.0
    cheapest_model = None
    cheapest_cost = float("inf")

    for name in model_names:
        s = summaries[name]
        total = sum(s.field_total.values())
        correct = sum(s.field_correct.values())
        acc = (correct / total) if total > 0 else 0
        if acc > best_acc:
            best_acc = acc
            best_model = name
        if s.estimated_monthly_cost < cheapest_cost:
            cheapest_cost = s.estimated_monthly_cost
            cheapest_model = name

    if best_model and cheapest_model:
        if best_model == cheapest_model:
            lines.append(
                f"**{best_model}** is both the most accurate ({best_acc:.1%}) "
                f"and cheapest (${cheapest_cost:.2f}/month). Clear winner."
            )
        else:
            best_s = summaries[best_model]
            cheap_s = summaries[cheapest_model]
            cheap_total = sum(cheap_s.field_total.values())
            cheap_correct = sum(cheap_s.field_correct.values())
            cheap_acc = (cheap_correct / cheap_total) if cheap_total > 0 else 0
            lines.append(
                f"- **Best accuracy**: {best_model} ({best_acc:.1%}), "
                f"${best_s.estimated_monthly_cost:.2f}/month"
            )
            lines.append(
                f"- **Cheapest**: {cheapest_model} ({cheap_acc:.1%}), "
                f"${cheapest_cost:.2f}/month"
            )
            acc_diff = best_acc - cheap_acc
            cost_ratio = (
                best_s.estimated_monthly_cost / cheapest_cost
                if cheapest_cost > 0
                else 0
            )
            if acc_diff < 0.05:  # Less than 5% accuracy difference
                lines.append(
                    f"\nAccuracy gap is small ({acc_diff:.1%}). Recommend "
                    f"**{cheapest_model}** for cost efficiency."
                )
            else:
                lines.append(
                    f"\n{best_model} is {acc_diff:.1%} more accurate but "
                    f"{cost_ratio:.1f}x more expensive. "
                    f"Recommend **{best_model}** if the accuracy gain justifies the cost."
                )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the multimodal OC extraction evaluation."""
    parser = argparse.ArgumentParser(
        description="Eval multimodal extraction accuracy on OC PDF fixtures."
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
        "--markdown",
        action="store_true",
        help="Output Markdown summary (for posting to GitHub).",
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
    fixtures = get_oc_fixtures()
    print(f"Found {len(fixtures)} OC PDF fixtures with ground truth\n")
    for fp, exp in fixtures:
        desc = exp.get("_description", "")
        print(f"  {fp.name}: {desc[:80]}")
    print()

    # Pre-render all PDFs to images (shared across models)
    print("Rendering PDFs to images...")
    fixture_images: dict[str, list[bytes]] = {}
    for fp, _ in fixtures:
        images = render_pdf_to_images(fp, dpi=args.dpi)
        fixture_images[fp.name] = images
        print(f"  {fp.name}: {len(images)} pages")
    print()

    # Run evaluation for each model
    all_summaries: dict[str, ModelSummary] = {}
    all_results: dict[str, list[FixtureResult]] = {}

    for model_name in args.models:
        model_config = MODELS[model_name]
        provider = model_config["provider"]
        model_id = model_config["model_id"]
        pricing = model_config["pricing_per_m"]

        if provider == "google":
            api_key = google_key
        else:
            api_key = anthropic_key

        print(f"\n{'=' * 70}")
        print(f"MODEL: {model_name} ({model_id})")
        print(f"{'=' * 70}\n")

        results: list[FixtureResult] = []

        for fixture_path, expected in fixtures:
            fname = fixture_path.name
            images = fixture_images[fname]
            page_count = len(images)

            # Build metadata from expected JSON
            metadata: dict[str, str] = {}
            link_text = expected.get("_link_text")
            if link_text:
                judge = expected.get("judge_name")
                dept = expected.get("department")
                if judge:
                    metadata["judge_name"] = judge
                if dept:
                    metadata["department"] = dept

            print(f"  {fname} ({page_count} pages)...", end=" ", flush=True)

            try:
                if provider == "google":
                    extracted, inp_tok, out_tok, lat = call_gemini_multimodal(
                        model_id,
                        images,
                        metadata,
                        api_key,
                    )
                else:
                    extracted, inp_tok, out_tok, lat = call_anthropic_multimodal(
                        model_id,
                        images,
                        metadata,
                        api_key,
                    )

                print(f"OK ({inp_tok:,}+{out_tok:,} tokens, {lat:,.0f}ms)")
                results.append(
                    FixtureResult(
                        fixture_name=fname,
                        model=model_name,
                        extracted=extracted,
                        expected=expected,
                        input_tokens=inp_tok,
                        output_tokens=out_tok,
                        latency_ms=lat,
                        page_count=page_count,
                    )
                )
            except Exception as e:
                print(f"ERROR: {e}")
                results.append(
                    FixtureResult(
                        fixture_name=fname,
                        model=model_name,
                        extracted={},
                        expected=expected,
                        page_count=page_count,
                        error=str(e),
                    )
                )

        summary = analyze_model(results, model_name, pricing)
        print_model_report(summary)
        all_summaries[model_name] = summary
        all_results[model_name] = results

    # Comparison table
    if len(all_summaries) > 1:
        print_comparison_table(all_summaries)

    # Markdown output
    if args.markdown or args.save:
        md = generate_markdown_summary(all_summaries)
        if args.markdown:
            print(f"\n{'=' * 70}")
            print("MARKDOWN SUMMARY")
            print(f"{'=' * 70}\n")
            print(md)

    # Save results
    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        output_path = RESULTS_DIR / "oc_multimodal_results.json"

        save_data = {
            "timestamp": datetime.now().isoformat(),
            "dpi": args.dpi,
            "fixtures_count": len(fixtures),
            "models": {},
        }
        for model_name, summary in all_summaries.items():
            total = sum(summary.field_total.values())
            correct = sum(summary.field_correct.values())
            save_data["models"][model_name] = {
                "total_fixtures": summary.total_fixtures,
                "field_accuracy": {
                    fld: (
                        summary.field_correct.get(fld, 0)
                        / summary.field_total.get(fld, 1)
                        * 100
                    )
                    for fld in ALL_FIELDS
                    if summary.field_total.get(fld, 0) > 0
                },
                "overall_accuracy": (correct / total * 100) if total > 0 else 0,
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "avg_latency_ms": summary.avg_latency_ms,
                "cost_per_fixture": summary.cost_per_fixture,
                "estimated_monthly_cost": summary.estimated_monthly_cost,
                "total_pages": summary.total_pages,
                "mismatches": [
                    {
                        "fixture": f,
                        "field": fld,
                        "expected": exp,
                        "got": got,
                        "known_issue": (f, fld) in KNOWN_FIXTURE_ISSUES,
                    }
                    for f, fld, exp, got in summary.mismatches
                ],
                "errors": summary.errors,
            }

        # Also save per-fixture raw results
        save_data["raw_results"] = {}
        for model_name, results in all_results.items():
            save_data["raw_results"][model_name] = [
                {
                    "fixture_name": r.fixture_name,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "latency_ms": r.latency_ms,
                    "page_count": r.page_count,
                    "error": r.error,
                    "extracted": r.extracted,
                }
                for r in results
            ]

        output_path.write_text(
            json.dumps(save_data, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nResults saved to: {output_path}")

        # Save markdown summary
        md = generate_markdown_summary(all_summaries)
        md_path = RESULTS_DIR / "oc_multimodal_summary.md"
        md_path.write_text(md, encoding="utf-8")
        print(f"Markdown summary saved to: {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
