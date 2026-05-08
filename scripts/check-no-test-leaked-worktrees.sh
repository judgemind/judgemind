#!/usr/bin/env bash
# check-no-test-leaked-worktrees.sh — Fail when synthetic agent worktrees
# leak into <repo_root>/.claude/worktrees/ during a test run (issue #4307).
#
# A real per-agent worktree always has a hex-only short id
# (e.g. ``agent-aabbccdd``) or a full uuid-like id
# (e.g. ``agent-aabbccdd-eeff-0011-2233-445566778899``). Anything else
# under ``<repo_root>/.claude/worktrees/`` after a test shard runs is a
# test-fixture leak — typically a pytest test that exercised
# ``DispatcherDaemon._create_worktree`` or
# ``DispatcherDaemon._compute_worktree_path`` against a real
# ``_repo_root`` (rather than a ``tmp_path``) and forgot to monkeypatch
# the repo-root accessor or the agent_id source.
#
# Pre-#4307 the symptom was an ``agent-<MagicMock id='...'>``
# directory landing in the workspace and tripping the next CI step's
# repo-walking hygiene check (#4300 was the original consumer
# casualty). The dispatcher pytest suite's session-scoped autouse
# fixture in ``scripts/dispatcher/tests/conftest.py`` is the primary
# guard; this script is defense-in-depth for cases where the fixture
# is bypassed (e.g. ``pytest --confcutdir=...`` or a foreign caller).
#
# Usage:
#   scripts/check-no-test-leaked-worktrees.sh             # scan <repo_root>/.claude/worktrees/
#   scripts/check-no-test-leaked-worktrees.sh [repo_root] # scan an alternate root (test harness)
#
# Exit codes:
#   0 — No leaked synthetic worktrees found.
#   1 — One or more synthetic worktree directories present at the
#       expected leak site.

# venv: none
# permanent: true

set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WORKTREES_DIR="$REPO_ROOT/.claude/worktrees"

# Real worktree names match agent-<8+ hex chars>(-<more hex>)*. This
# regex is intentionally tolerant — uuid-like suffixes (8-4-4-4-12 hex
# segments) and the bare 8-hex short_id form both pass. Everything
# else is rejected.
REAL_NAME_RE='^agent-[0-9a-f]{8,}(-[0-9a-f]+)*$'

if [[ ! -d "$WORKTREES_DIR" ]]; then
    # No .claude/worktrees/ at all → nothing to leak. This is the
    # normal state of a freshly-cloned CI runner.
    exit 0
fi

# ``find -maxdepth 1`` so we only enumerate direct children. Each
# child is checked against $REAL_NAME_RE; mismatches are reported.
leaked=0
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    name="$(basename "$entry")"
    if [[ ! "$name" =~ ^agent- ]]; then
        # Non-agent entries (lockfiles, subdirs from older tooling)
        # are out of scope for this check.
        continue
    fi
    if [[ "$name" =~ $REAL_NAME_RE ]]; then
        continue
    fi
    if [[ "$leaked" -eq 0 ]]; then
        echo "FAIL: synthetic agent worktree directories leaked under .claude/worktrees/ (issue #4307):" >&2
    fi
    echo "  - $entry" >&2
    leaked=$((leaked + 1))
done < <(find "$WORKTREES_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)

if [[ "$leaked" -gt 0 ]]; then
    echo "" >&2
    echo "These directories do not match the real-worktree naming pattern" >&2
    echo "(${REAL_NAME_RE}). They were almost certainly created by a" >&2
    echo "dispatcher test that forgot to monkeypatch \`_repo_root\` or that" >&2
    echo "interpolated a non-string (typically a MagicMock) into the" >&2
    echo "agent_id passed to \`_create_worktree\` / \`_compute_worktree_path\`." >&2
    echo "" >&2
    echo "Fix: clean these directories and update the responsible test to" >&2
    echo "pin \`_repo_root\` to \`tmp_path\` (or stub \`_create_worktree\`" >&2
    echo "directly). See \`scripts/dispatcher/tests/conftest.py\` for the" >&2
    echo "session-scoped invariant that should have caught this earlier." >&2
    exit 1
fi

echo "OK: no synthetic agent worktree leaks under $WORKTREES_DIR"
exit 0
