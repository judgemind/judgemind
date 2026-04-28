#!/usr/bin/env bash
# check-blocked-issues.sh — Detect issues with status/blocked but no Blocked by in body.
#
# Scans all open issues with the status/blocked label and verifies each one
# has at least one "Blocked by #N" line in the issue body's ## Dependencies
# section. Issues that have the label but lack the body text will cause the
# unblock-issues CI workflow to never fire, leaving them permanently stuck.
#
# Usage:
#   scripts/check-blocked-issues.sh          # Check and report
#   scripts/check-blocked-issues.sh --fix    # Auto-fix using comment history
#
# Exit codes:
#   0 — All blocked issues have matching body text (or no blocked issues exist)
#   1 — Mismatches found (issues have label but no Blocked by in body)

set -euo pipefail

REPO="judgemind/judgemind"
FIX_MODE=false

if [[ "${1:-}" == "--fix" ]]; then
    FIX_MODE=true
fi

# Fetch all open issues with status/blocked label
ISSUES=$(gh issue list --repo "$REPO" \
    --label "status/blocked" \
    --state open \
    --json number,title,body \
    --limit 100 2>/dev/null) || {
    echo "Error: Could not fetch issues from $REPO." >&2
    exit 1
}

COUNT=$(echo "$ISSUES" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
    echo "No open issues with status/blocked label found."
    exit 0
fi

echo "Checking $COUNT open issues with status/blocked label..."
echo ""

MISMATCHES=0
TMPFILE=$(mktemp)

# Check each issue
echo "$ISSUES" | python3 -c "
import sys, json, re

issues = json.load(sys.stdin)
mismatches = []

for issue in issues:
    n = issue['number']
    title = issue['title']
    body = issue.get('body') or ''

    # Look for 'Blocked by #N' or 'Blocked by: #N' lines in the body
    blockers = re.findall(r'Blocked by:?\s+#(\d+)', body)

    if not blockers:
        mismatches.append((n, title))
        print(f'MISMATCH: #{n} — {title}')
        print(f'  Has status/blocked label but no \"Blocked by #N\" in issue body.')
        print(f'  The unblock-issues workflow will never unblock this issue.')
        print()

if not mismatches:
    print('All blocked issues have matching \"Blocked by #N\" lines in their body.')
    sys.exit(0)
else:
    print(f'{len(mismatches)} issue(s) have mismatched blocking state.')
    print('Fix by adding \"Blocked by #N\" to the issue body, or use:')
    print('  scripts/block-issue.sh <issue> <blocker>')
    sys.exit(1)
" 2>&1 | tee "$TMPFILE"

# Capture the exit code from python
RESULT=${PIPESTATUS[1]:-1}

rm -f "$TMPFILE"
exit "$RESULT"
