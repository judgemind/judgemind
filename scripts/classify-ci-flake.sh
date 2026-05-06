#!/usr/bin/env bash
# classify-ci-flake.sh — Classify a CI failure as a known flake or a real failure.
#
# # venv: none
# # permanent: true
#
# ── Why ──────────────────────────────────────────────────────────────────────
#
# CI hits transient infra flakes that look identical to real failures from the
# outside (`gh pr view` reports `failure`, `wait-for-ci.sh` exits 1, the agent
# is forced into the manual rerun loop). The most common offender is the
# `schema-drift-check` job aborting on `ERROR: postgres failed to start within
# 30 seconds` because the postgres service container failed to bind in time on
# a slow runner. Each occurrence costs ~5-10 min of wall-clock per agent task.
#
# This helper classifies a failed-job log tail by tail-pattern match. It does
# NOT decide whether to rerun — that is the caller's job (see
# `scripts/wait-for-ci.sh`). Keeping the classifier as a separate single-input
# / single-output script makes it trivially unit-testable and allows other
# callers (post-mortem scripts, the dispatcher's diagnoser, etc.) to reuse the
# same rule table.
#
# Adding a new flake pattern is a one-line edit to the `FLAKE_PATTERNS` table
# below: append `<label>=<grep -E regex>`. The label is the human-readable
# string emitted on stdout when a match is found and is what the caller logs
# in the transcript (`flake detected: postgres-startup`).
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Usage:
#   scripts/classify-ci-flake.sh           # reads job log on stdin
#   scripts/classify-ci-flake.sh <file>    # reads from file
#
# Output (stdout, single line):
#   flake/<label>     — the log matched a known flake pattern
#   real              — no known flake pattern matched
#
# Exit codes:
#   0 — Classification emitted to stdout (success regardless of flake/real).
#   1 — Usage error (no stdin and missing file argument).
#
# ── Pattern table ────────────────────────────────────────────────────────────
#
# Each entry is `<label>|<grep -E regex>`. The script applies them in order;
# the first matching pattern wins. Labels MUST NOT contain a `|` character.
# Patterns are extended-regex (`grep -E`).
#
# Today's known-flake catalogue:
#
# - postgres-startup: `schema-drift-check` and other jobs that boot a postgres
#   service container print this exact line on a slow-runner timeout. Source:
#   `scripts/check_schema_drift.sh:39`.
# - docker-daemon: GitHub-hosted runners occasionally lose the Docker socket
#   for the first ~10s of a job. Manifests as `Cannot connect to the Docker
#   daemon`.
# - dns-resolution: transient DNS hiccups inside the runner; `apt-get`,
#   `npm install`, `git clone` all surface this with substring `Could not
#   resolve host`.
# - github-network: GitHub Actions runner occasionally loses connectivity to
#   `api.github.com` mid-job; surfaces as `unable to access` or `Failed to
#   connect to github.com`.
#
# To add a new pattern: append a line to FLAKE_PATTERNS below. Keep the regex
# specific enough that real test failures will not match — broad patterns mask
# real outages.

set -euo pipefail

# Pattern table — order matters (first match wins). Format: label|regex
# shellcheck disable=SC2034  # FLAKE_PATTERNS values consumed via `${row#*|}` below
FLAKE_PATTERNS=(
    "postgres-startup|ERROR: postgres failed to start within [0-9]+ seconds"
    "docker-daemon|Cannot connect to the Docker daemon"
    "dns-resolution|Could not resolve host"
    "github-network|Failed to connect to github\.com"
)

print_help() {
    grep '^# Usage:' "$0" | sed 's/^# //'
    echo ""
    grep '^# Output' "$0" | sed 's/^# //'
    echo ""
    grep '^# Exit codes:' "$0" | sed 's/^# //'
    grep '^#   [012]' "$0" | sed 's/^# //'
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    print_help
    exit 0
fi

# Read input from file or stdin.
if [ $# -ge 1 ]; then
    INPUT_FILE="$1"
    if [ ! -f "$INPUT_FILE" ]; then
        echo "ERROR: input file does not exist: $INPUT_FILE" >&2
        exit 1
    fi
    LOG_CONTENT=$(cat "$INPUT_FILE")
else
    # No file arg — read stdin.
    if [ -t 0 ]; then
        echo "ERROR: no input — pipe a job log on stdin or pass a file path." >&2
        echo "Run with --help for usage." >&2
        exit 1
    fi
    LOG_CONTENT=$(cat)
fi

# Walk the pattern table in order. First match wins.
for ROW in "${FLAKE_PATTERNS[@]}"; do
    LABEL="${ROW%%|*}"
    REGEX="${ROW#*|}"
    if printf '%s' "$LOG_CONTENT" | grep -E -q -- "$REGEX"; then
        echo "flake/${LABEL}"
        exit 0
    fi
done

echo "real"
exit 0
