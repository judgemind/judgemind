#!/usr/bin/env bash
# check-no-hardcoded-llm-provider.sh — Detect hardcoded ``provider="..."``
# arguments to LLM adapter call sites.
#
# Why this check exists
# ---------------------
# After #4032 dropped seven hardcoded ``provider="anthropic"`` callsites
# from ``packages/scraper-framework/``, this guard prevents the same
# class of bug from recurring: a ``call_llm()`` / ``call_llm_with_images()``
# / ``create_client()`` / ``create_llm_client()`` call outside
# ``llm_providers.py`` that pins the provider with a string literal
# silently bypasses the ``LLM_PROVIDER`` env var the rest of the pipeline
# relies on.  The next provider flip (or a new dev who copy-pastes one
# of these patterns) would be unable to flip without re-finding all the
# hardcoded sites.
#
# What is flagged
# ---------------
# Inside ``packages/scraper-framework/src/`` (excluding
# ``ingestion/llm_providers.py`` itself), any call where:
#   1. The callee is one of ``call_llm``, ``call_llm_with_images``,
#      ``create_client``, ``create_llm_client`` (matched by the bare
#      function name, regardless of attribute-style prefix).
#   2. The call has a ``provider=`` keyword argument whose value is a
#      string literal (e.g. ``provider="anthropic"``).
#
# Scope is **call sites**, not constructors.  The
# ``LlmExtractor(provider="google", ...)`` per-court override pattern
# is intentionally NOT flagged — that's a class-level boundary distinct
# from the env-var-controlled adapter layer (per #4050 Out of Scope).
#
# What is NOT flagged
# -------------------
#   - ``provider=`` arg whose value is a Name (``self._llm_provider``,
#     ``DEFAULT_PROVIDER``), an attribute access, or any non-literal —
#     the env var resolution stays in control.
#   - Calls inside ``packages/scraper-framework/src/ingestion/llm_providers.py``
#     itself — that's the adapter layer where the ``provider=`` literal
#     IS the canonical flip.
#   - Calls under ``packages/scraper-framework/tests/`` — not in scope.
#   - Files outside ``packages/scraper-framework/src/`` — out of scope
#     per the issue.  The companion check for ``packages/nlp-pipeline/``
#     is tracked under the NLP-provider-abstraction follow-up referenced
#     in #4032's "Out of Scope".
#   - Calls suppressed with a trailing
#     ``# hardcoded-provider-ok: <reason>`` comment on any line of the
#     call site (between the opening and closing parens of a multi-line
#     call).  The reason after the colon is required.
#
# Issue: #4050.  Reference fix: #4032.
#
# Usage
# -----
#   scripts/check-no-hardcoded-llm-provider.sh           # production scan
#   scripts/check-no-hardcoded-llm-provider.sh [path]    # scan a path (used by tests)
#
# Exit codes
# ----------
#   0 — No violations found.
#   1 — One or more files contain hardcoded LLM-adapter ``provider=`` args.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Determine scan targets ──────────────────────────────────────────────
# With no argument: scan packages/scraper-framework/src/ (the production
# scope per #4050).  With an argument: scan that path (file or directory)
# — used by tests.
if [[ $# -eq 0 ]]; then
    SCAN_TARGETS=("$REPO_ROOT/packages/scraper-framework/src")
else
    SCAN_TARGETS=("$1")
fi

# ─── Files to exclude (relative-suffix match) ────────────────────────────
# The adapter layer itself is the canonical home of ``provider=`` string
# literals — it's where the dispatch table on the env-var value lives.
EXCLUDE_FILE_SUFFIXES=(
    "packages/scraper-framework/src/ingestion/llm_providers.py"
)

# ─── Filter to existing .py paths, respecting exclusions ─────────────────
py_files=()
for target in "${SCAN_TARGETS[@]}"; do
    if [[ -f "$target" && "$target" == *.py ]]; then
        py_files+=("$target")
    elif [[ -d "$target" ]]; then
        while IFS= read -r found; do
            # Skip excluded files by suffix
            skip=false
            for excl in "${EXCLUDE_FILE_SUFFIXES[@]}"; do
                if [[ "$found" == *"$excl" ]]; then
                    skip=true
                    break
                fi
            done
            "$skip" && continue
            py_files+=("$found")
        done < <(find "$target" \
            -type d \( \
                -name '.venv' -o \
                -name '__pycache__' -o \
                -name 'node_modules' -o \
                -name '.git' -o \
                -name 'tests' \
            \) -prune \
            -o -type f -name '*.py' -print)
    fi
done

if [[ ${#py_files[@]} -eq 0 ]]; then
    exit 0
fi

# ─── Run the AST scanner ─────────────────────────────────────────────────
# The Python scanner emits one line per violation in the form:
#   <path>:<lineno>:<callee>(provider="<value>")
python_output="$(python3 "$REPO_ROOT/scripts/check_no_hardcoded_llm_provider.py" "${py_files[@]}")"

# ─── Report violations ───────────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    echo "All clean — no hardcoded LLM-adapter provider= literals found."
    exit 0
fi

violations=0
echo "ERROR: Found hardcoded provider= literal(s) in LLM-adapter call sites."
echo ""
echo "  Pinning the provider with a string literal at the call site"
echo "  silently bypasses the LLM_PROVIDER env var, which is the"
echo "  single point of control for which provider the pipeline uses."
echo "  See #4050 (this guard) and #4032 (the refactor that motivated it)."
echo ""
echo "  Violating call sites:"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

echo ""
echo "  Found $violations hardcoded provider= literal(s)."
echo ""
echo "  Fix: drop the provider= argument so the call resolves the"
echo "  provider from the LLM_PROVIDER env var via resolve_provider()."
echo ""
echo "    Before:"
echo "      response = call_llm(system_prompt, user_message,"
echo "                          provider=\"anthropic\")"
echo ""
echo "    After:"
echo "      response = call_llm(system_prompt, user_message)"
echo "      # provider resolves from LLM_PROVIDER env var"
echo ""
echo "  Suppress (audited callsite that intentionally pins the provider):"
echo "    response = call_llm(system_prompt, user_message,"
echo "                        provider=\"anthropic\")  # hardcoded-provider-ok: <reason-with-issue-ref>"
echo ""
exit 1
