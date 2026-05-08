#!/usr/bin/env bash
# check-llm-json-loads.sh — Flag raw json.loads calls on LLM response text.
#
# After fixing #2518 (LLM JSON parse errors from unescaped control chars) we
# centralized LLM JSON parsing on ``framework.llm_utils.parse_llm_json`` which
# wraps ``json.loads(..., strict=False)``.  Future callers could easily
# regress to a raw ``json.loads(response.text)`` call and re-introduce the
# same class of ``Invalid control character`` failures.
#
# This check greps for two suspect patterns in Python source under
# packages/*/src/:
#
#   1. ``json.loads(<expr>.text<word-boundary>)`` — the attribute access
#      ``.text`` is the LLM response convention across google-genai
#      (``response.text``) and anthropic (``response.content[0].text``).
#   2. ``json.loads(raw_text)`` — the variable name our callers used
#      pre-fix (see PR #2543 diff).
#
# When matched, the script emits a warning pointing at
# ``framework.llm_utils.parse_llm_json``.  An explicit allowlist covers the
# handful of sites that legitimately use ``json.loads`` on non-LLM response
# text (e.g. httpx responses from a court data API, Redis stream events).
#
# Usage:
#   scripts/check-llm-json-loads.sh          # exits 0 if clean, 1 if violations
#   scripts/check-llm-json-loads.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No suspect json.loads patterns found.
#   1 — One or more files call json.loads on apparent LLM response text.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Suspect patterns ─────────────────────────────────────────────────
# Pattern 1: json.loads(...text<word-boundary>)
#   Catches json.loads(response.text), json.loads(response.text.strip()),
#   json.loads(response.content[0].text), json.loads(foo.text[0]), etc.
#   The leading ``\.`` ensures we match the ``.text`` attribute access
#   rather than a standalone ``text`` identifier.
#
# Pattern 2: json.loads(<ws>raw_text<ws>)
#   Catches the literal variable name used by the pre-#2518 callers.
#   Allows leading/trailing whitespace for formatting tolerance.
PATTERN='json\.loads\([^)]*\.text\b|json\.loads\(\s*raw_text\s*[,)]'

# ─── Per-check extras on top of REPO_WALK_EXCLUSIONS ──────────────────
# ``tests`` and ``test`` directories are excluded because LLM response
# fixtures and assertions in tests legitimately call ``json.loads`` on
# fake LLM-shaped strings; the production-source-only scope keeps the
# guard signal clean. Repo-wide dir exclusions come from preflight.sh.
EXTRA_EXCLUDE_DIRS=(tests test)

# Allowlist — files where json.loads on .text or raw_text is legitimate
# (non-LLM source).  Keep this list small and justified.  New entries
# must be documented with why the call is safe.
EXCLUDE_FILES=(
    # This script itself (documents the forbidden patterns in comments).
    "scripts/check-llm-json-loads.sh"
    "scripts/tests/test_check_llm_json_loads.sh"
    # Source of truth for parse_llm_json — the docstring mentions the
    # forbidden pattern in its explanation.
    "packages/scraper-framework/src/framework/llm_utils.py"
    # SF civil fetches rulings via httpx GET from tr.dll — response.text
    # is an HTTP API response, not an LLM response.  Unescaped control
    # chars are not a risk here because the server emits RFC-compliant
    # JSON.
    "packages/scraper-framework/src/courts/ca/sf_civil_tentatives.py"
)

# ─── Build grep arguments ─────────────────────────────────────────────
# Canonical list (REPO_WALK_EXCLUSIONS in preflight.sh, #4308) plus the
# per-check extras above.
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}" ${EXTRA_EXCLUDE_DIRS[@]+"${EXTRA_EXCLUDE_DIRS[@]}"}; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Determine scan targets ───────────────────────────────────────────
# When scanning the repo root, only look inside packages/*/src/.
# When a custom directory is passed (e.g. from tests), scan everything in it.
scan_targets=()
if [[ "$SCAN_DIR" == "$REPO_ROOT" ]]; then
    for pkg_src in "$REPO_ROOT"/packages/*/src/; do
        [[ -d "$pkg_src" ]] && scan_targets+=("$pkg_src")
    done
    if [[ ${#scan_targets[@]} -eq 0 ]]; then
        echo "All clean — no packages/*/src/ directories found, nothing to scan."
        exit 0
    fi
else
    scan_targets+=("$SCAN_DIR")
fi

# ─── Scan for violations ──────────────────────────────────────────────
violations=0

for target in "${scan_targets[@]}"; do
    [[ -e "$target" ]] || continue

    matches=$(grep -rnE "$PATTERN" "$target" --include='*.py' "${exclude_args[@]}" 2>/dev/null || true)

    [[ -z "$matches" ]] && continue

    while IFS= read -r line; do
        # Check if this match is in an excluded file
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            if [[ "$line" == *"$excl"* ]]; then
                skip=true
                break
            fi
        done
        "$skip" && continue

        # Extract the content portion (after filename:lineno:)
        content="${line#*:}"   # strip filename
        content="${content#*:}"  # strip line number
        trimmed="${content#"${content%%[![:space:]]*}"}"

        # Skip comment lines
        if [[ "$trimmed" == "#"* ]]; then
            continue
        fi

        # Skip calls that already opt into relaxed parsing.  Both
        # ``json.loads(..., strict=False)`` and ``parse_llm_json(...)``
        # are safe against the #2518 control-char failure mode; the
        # former is the inline fix pattern used where importing
        # ``parse_llm_json`` would introduce a cross-package dependency
        # (e.g. ``nlp-pipeline`` which does not depend on
        # ``scraper-framework``).
        if [[ "$content" == *"strict=False"* ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found raw json.loads call(s) on apparent LLM response text."
            echo ""
            echo "  LLM responses occasionally contain unescaped ASCII control"
            echo "  characters inside JSON string values (see #2518).  Parsing"
            echo "  them with a raw json.loads rejects the whole payload."
            echo ""
            echo "  Use parse_llm_json instead — it wraps json.loads with"
            echo "  strict=False and strips markdown code fences:"
            echo ""
            echo "    from framework.llm_utils import parse_llm_json"
            echo "    parsed = parse_llm_json(response.text)"
            echo ""
            echo "  Violations:"
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations suspect json.loads call(s)."
    echo ""
    echo "  Fix: replace json.loads(<llm_response>.text) or"
    echo "  json.loads(raw_text) with parse_llm_json(...) imported from"
    echo "  framework.llm_utils."
    echo ""
    echo "  If the call is legitimately on non-LLM text (e.g. an HTTP API"
    echo "  response from a court data server), add the file to the"
    echo "  EXCLUDE_FILES allowlist in scripts/check-llm-json-loads.sh"
    echo "  with a comment explaining why the call is safe."
    echo ""
    echo "  See: https://github.com/judgemind/judgemind/issues/2518"
    exit 1
fi

echo "All clean — no raw json.loads calls on LLM response text found."
exit 0
