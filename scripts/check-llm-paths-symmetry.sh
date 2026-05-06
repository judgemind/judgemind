#!/usr/bin/env bash
# check-llm-paths-symmetry.sh — Assert dual-LLM-extraction-path instrumentation
# parity between framework/llm_extractor.py and ingestion/llm_extract.py.
#
# Two distinct LLM extraction modules cohabit in
# packages/scraper-framework/src/ — a recurring shape that produced #4246 (path
# A only) immediately followed by #4249 (path B follow-up) when the agent
# instrumented one path on a hypothesis check, then discovered post-deploy that
# the actually-failing documents flowed through the other path.
#
# This guard codifies the invariant that BOTH paths emit a chunk-failure
# structured log event AND that both call sites carry ``document_id`` in their
# kwargs.  The guard does not (and should not) replace the docs subsection in
# docs/specs/architecture-spec-v1.md §3.3.2.1 — it just enforces the one
# instrumentation property that #4246/#4249 surfaced as the recurring footgun.
#
# Rules enforced (a violation in any rule produces a non-zero exit):
#
#   1. ingestion/llm_extract.py emits at least one ``llm_extract.chunk_api_failure``
#      logger.warning call.
#   2. framework/llm_extractor.py emits at least one
#      ``llm_extractor.google_api_failure`` logger.warning call.
#   3. Every emission of those two events includes ``document_id=`` in the
#      structured payload (the multiline kwarg block that immediately follows
#      the opening ``logger.warning(`` line).
#
# Rules 1 and 2 are stricter than #4233's hypothesis frame — they don't just
# assert "the event exists in the instrumented set"; they assert each path
# CARRIES its own event under its own canonical name.  If either module
# replaces its event with a different name (e.g. consolidation per H3), this
# script must be updated atomically with the rename.
#
# Usage:
#   scripts/check-llm-paths-symmetry.sh          # exits 0 if clean, 1 if violations
#
# Exit codes:
#   0 — Both paths are instrumented; both call sites include document_id=.
#   1 — One or more rules violated (details on stderr).
#
# Test override:
#   Set LLM_PATHS_SYMMETRY_ROOT to a directory containing alternate
#   ``packages/scraper-framework/src/{framework/llm_extractor.py,
#   ingestion/llm_extract.py}`` files.  Used by
#   scripts/tests/test_check_llm_paths_symmetry.sh to drive failure-mode
#   coverage without mutating the real source tree.

set -euo pipefail

REPO_ROOT="${LLM_PATHS_SYMMETRY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

PATH_A="$REPO_ROOT/packages/scraper-framework/src/framework/llm_extractor.py"
PATH_B="$REPO_ROOT/packages/scraper-framework/src/ingestion/llm_extract.py"

EVENT_A="llm_extractor.google_api_failure"
EVENT_B="llm_extract.chunk_api_failure"

violations=0

check_event_present() {
    local file="$1"
    local event="$2"

    if [[ ! -f "$file" ]]; then
        echo "ERROR: Expected LLM-extraction module is missing: $file" >&2
        violations=$((violations + 1))
        return
    fi

    # Match the log-call form ``"<event>",`` inside a logger.warning(...) call.
    # The trailing comma rules out doc-comment mentions of the event name.
    if ! grep -qE "\"$event\"," "$file"; then
        echo "ERROR: $file does not emit a ``$event`` structured log event." >&2
        echo "       Both LLM extraction paths must emit a chunk-failure event" >&2
        echo "       under their canonical name — see" >&2
        echo "       docs/specs/architecture-spec-v1.md §3.3.2.1." >&2
        violations=$((violations + 1))
    fi
}

# Rule 1 + 2: each path emits its canonical event.
check_event_present "$PATH_A" "$EVENT_A"
check_event_present "$PATH_B" "$EVENT_B"

# Rule 3: each emission includes ``document_id=`` in its kwargs.  The kwargs
# follow the event name on subsequent lines until the closing ``)`` of the
# logger.warning(...) call.  We use a small Python helper because awk-style
# multiline scanning is fragile across editors that mix tabs/spaces and
# wrap kwargs differently.
check_event_carries_document_id() {
    local file="$1"
    local event="$2"

    [[ -f "$file" ]] || return

    python3 - "$file" "$event" <<'PY' || violations=$((violations + 1))
import re
import sys

path, event = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    src = fh.read()

# Find every ``logger.warning(`` that is followed by ``"<event>",`` and capture
# the kwargs block up to the matching closing paren.  We approximate "matching
# closing paren" by scanning forward until paren depth returns to zero — good
# enough for the call shape used in both modules today.
needle = f'"{event}",'
errors = []
search_start = 0
while True:
    idx = src.find(needle, search_start)
    if idx == -1:
        break
    # Walk backwards to find the enclosing logger.warning( opening.
    open_idx = src.rfind("logger.warning(", 0, idx)
    if open_idx == -1:
        search_start = idx + len(needle)
        continue
    # Walk forward from open_idx to find the matching close paren.
    depth = 0
    end = None
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        errors.append(f"unterminated logger.warning(... call at offset {open_idx}")
        search_start = idx + len(needle)
        continue
    block = src[open_idx:end + 1]
    # The structured-log kwargs block is everything after the event-name comma.
    after_event = block[block.find(needle) + len(needle):]
    if not re.search(r"\bdocument_id\s*=", after_event):
        line_no = src.count("\n", 0, open_idx) + 1
        errors.append(f"{path}:{line_no}: logger.warning({event!r}, ...) missing document_id= kwarg")
    search_start = end + 1

if errors:
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    print(
        "       The chunk-failure event MUST include document_id= so the "
        "log line is\n"
        "       self-diagnosing — see docs/specs/architecture-spec-v1.md "
        "§3.3.2.1.",
        file=sys.stderr,
    )
    sys.exit(1)
PY
}

check_event_carries_document_id "$PATH_A" "$EVENT_A"
check_event_carries_document_id "$PATH_B" "$EVENT_B"

if [[ $violations -gt 0 ]]; then
    echo "" >&2
    echo "  Found $violations dual-LLM-path symmetry violation(s)." >&2
    echo "  See docs/specs/architecture-spec-v1.md §3.3.2.1 and #4254." >&2
    exit 1
fi

echo "All clean — both LLM extraction paths emit their chunk-failure event with document_id=."
exit 0
