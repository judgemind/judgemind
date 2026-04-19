#!/usr/bin/env python3
# venv: none (stdlib only)
# one-off: true
"""Validate spike 0.4 Gemini outputs match /task-v2-summary schema.

Reads:
- tmp/spike-0.4/summary_stdout.txt (Gemini --output-format json wrapper)

Writes:
- tmp/spike-0.4/summary_validation.json (parsed + schema-check result)

Throwaway — spike 0.4 only.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTDIR = REPO_ROOT / "tmp" / "spike-0.4"

REQUIRED_FIELDS = {
    "process_summary_md",
    "commit_message",
    "pr_title",
    "pr_body_md",
    "unmet_criteria",
}


def validate_gemini_output() -> dict:
    raw = (OUTDIR / "summary_stdout.txt").read_text(encoding="utf-8")
    envelope = json.loads(raw)
    result = {
        "envelope_fields": sorted(envelope.keys()),
        "has_session_id": "session_id" in envelope,
        "has_response": "response" in envelope,
        "has_stats": "stats" in envelope,
    }
    # Gemini wraps the model answer in envelope.response as a string.
    response_str = envelope.get("response", "")
    result["response_len"] = len(response_str)
    try:
        response_obj = json.loads(response_str)
        result["response_parsed"] = True
        result["response_fields"] = sorted(response_obj.keys())
        missing = REQUIRED_FIELDS - set(response_obj.keys())
        extra = set(response_obj.keys()) - REQUIRED_FIELDS
        result["missing_fields"] = sorted(missing)
        result["extra_fields"] = sorted(extra)
        result["schema_match"] = not missing
        # field-level checks
        result["field_types"] = {k: type(v).__name__ for k, v in response_obj.items()}
        result["commit_message"] = response_obj.get("commit_message", "")
        result["pr_title"] = response_obj.get("pr_title", "")
        result["unmet_criteria_count"] = (
            len(response_obj["unmet_criteria"])
            if isinstance(response_obj.get("unmet_criteria"), list)
            else None
        )
        # Preserve the response object for the investigation doc.
        (OUTDIR / "summary_response_parsed.json").write_text(
            json.dumps(response_obj, indent=2), encoding="utf-8"
        )
    except json.JSONDecodeError as e:
        result["response_parsed"] = False
        result["parse_error"] = str(e)

    # Token stats
    stats = envelope.get("stats", {})
    models = stats.get("models", {})
    totals = {}
    for model_name, model_stats in models.items():
        tokens = model_stats.get("tokens", {})
        totals[model_name] = {
            "prompt": tokens.get("prompt", 0),
            "candidates": tokens.get("candidates", 0),
            "total": tokens.get("total", 0),
            "cached": tokens.get("cached", 0),
            "thoughts": tokens.get("thoughts", 0),
            "api_requests": model_stats.get("api", {}).get("totalRequests", 0),
            "api_latency_ms": model_stats.get("api", {}).get("totalLatencyMs", 0),
        }
    result["token_usage_by_model"] = totals
    result["tool_stats"] = stats.get("tools", {})

    return result


def main() -> int:
    out = validate_gemini_output()
    (OUTDIR / "summary_validation.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print(json.dumps(out, indent=2))
    return 0 if out.get("schema_match") else 1


if __name__ == "__main__":
    raise SystemExit(main())
