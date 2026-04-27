#!/usr/bin/env bash
# test_check_spec_action_vocabulary.sh — Tests for check-spec-action-vocabulary.sh
#
# Verifies that the diagnoser-vocabulary drift check (a) passes against the
# current repo state (regression: it was authored to be in-sync), and (b)
# fails when the daemon and SKILL.md disagree on the action vocabulary.
#
# Usage:
#   scripts/tests/test_check_spec_action_vocabulary.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/check-spec-action-vocabulary.sh"
FAILURES=0
TESTS=0

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    echo "FAIL: $CHECK_SCRIPT not found or not executable"
    exit 1
fi

# ─── Test 1: current repo state passes ──────────────────────────────────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: current repo state has no drift"
else
    echo "FAIL: current repo state should pass but the checker reported drift"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 2: injected drift fails ───────────────────────────────────────
# Stage a copy of the repo fragment with one extra action in the daemon's
# DIAGNOSER_ACTIONS frozenset and run the check against the staged copy.
TESTS=$((TESTS + 1))

STAGE=$(mktemp -d)
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p "$STAGE/scripts/dispatcher"
mkdir -p "$STAGE/.claude/skills/diagnose-failure"

# Synthesize a minimal daemon.py with an extra action that the SKILL won't have.
cat > "$STAGE/scripts/dispatcher/daemon.py" <<'PY'
DIAGNOSER_ACTIONS: frozenset[str] = frozenset(
    {
        "retry",
        "retry_with_hint",
        "reissue",
        "escalate",
        "close",
        "block_and_comment",
        "file_prerequisite_task",
        "block_on_existing_task",
        "terminal",
        "future_action_not_in_skill",
    }
)
PY

# Copy the real SKILL.md unchanged. Drift = action in daemon, missing from SKILL.
cp "$REPO_ROOT/../.claude/skills/diagnose-failure/SKILL.md" \
   "$STAGE/.claude/skills/diagnose-failure/SKILL.md"

# Run the check against the staged repo by copying the script and pointing it
# at the stage via REPO_ROOT.
cp "$CHECK_SCRIPT" "$STAGE/check-spec-action-vocabulary.sh"
chmod +x "$STAGE/check-spec-action-vocabulary.sh"

# Patch the script to use the staged paths (cheaper than re-architecting the
# script to take args; this is a test).
python3 - "$STAGE/check-spec-action-vocabulary.sh" "$STAGE" <<'PY'
import sys, pathlib
script_path = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2])
text = script_path.read_text()
text = text.replace(
    'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
    f'REPO_ROOT="{stage}"',
    1,
)
script_path.write_text(text)
PY

if "$STAGE/check-spec-action-vocabulary.sh" > "$STAGE/out.log" 2>&1; then
    echo "FAIL: injected daemon-side drift should have been detected"
    cat "$STAGE/out.log"
    FAILURES=$((FAILURES + 1))
else
    if grep -q 'future_action_not_in_skill' "$STAGE/out.log"; then
        echo "PASS: injected drift (extra action in daemon) is detected"
    else
        echo "FAIL: drift detected but the message did not name 'future_action_not_in_skill'"
        cat "$STAGE/out.log"
        FAILURES=$((FAILURES + 1))
    fi
fi

# ─── Test 3: SKILL.md mentions an action the daemon doesn't have ────────
TESTS=$((TESTS + 1))

STAGE2=$(mktemp -d)
cleanup2() { rm -rf "$STAGE2"; }
trap 'cleanup; cleanup2' EXIT

mkdir -p "$STAGE2/scripts/dispatcher"
mkdir -p "$STAGE2/.claude/skills/diagnose-failure"

cp "$REPO_ROOT/dispatcher/daemon.py" "$STAGE2/scripts/dispatcher/daemon.py"

# Synthesize a SKILL.md whose canonical action line invents an extra action.
cat > "$STAGE2/.claude/skills/diagnose-failure/SKILL.md" <<'MD'
# diagnose-failure

**Recommendation contract — 10 known actions; prefer `terminal`.** Issue #3032's
recommendation field stays for audit. The daemon's deterministic consumer reads
it and runs the matching action: `retry`, `retry_with_hint`, `reissue`,
`escalate`, `close`, `block_and_comment`, `file_prerequisite_task`,
`block_on_existing_task`, `terminal`, `phantom_action_not_in_daemon`.

## Audit trail
later sections.
MD

cp "$CHECK_SCRIPT" "$STAGE2/check-spec-action-vocabulary.sh"
chmod +x "$STAGE2/check-spec-action-vocabulary.sh"

python3 - "$STAGE2/check-spec-action-vocabulary.sh" "$STAGE2" <<'PY'
import sys, pathlib
script_path = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2])
text = script_path.read_text()
text = text.replace(
    'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
    f'REPO_ROOT="{stage}"',
    1,
)
script_path.write_text(text)
PY

if "$STAGE2/check-spec-action-vocabulary.sh" > "$STAGE2/out.log" 2>&1; then
    echo "FAIL: injected SKILL-side drift should have been detected"
    cat "$STAGE2/out.log"
    FAILURES=$((FAILURES + 1))
else
    if grep -q 'phantom_action_not_in_daemon' "$STAGE2/out.log"; then
        echo "PASS: injected drift (extra action in SKILL.md) is detected"
    else
        echo "FAIL: drift detected but the message did not name 'phantom_action_not_in_daemon'"
        cat "$STAGE2/out.log"
        FAILURES=$((FAILURES + 1))
    fi
fi

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
