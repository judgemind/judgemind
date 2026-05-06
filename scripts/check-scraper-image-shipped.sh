#!/usr/bin/env bash
# check-scraper-image-shipped.sh — assert every /app/scripts/<X>.{py,sh}
# reference in the repo has a matching COPY directive in the
# scraper-framework Dockerfile.
#
# Background — #4294 (retrospective from #4288 / PR #4292):
#
#   The scraper image (packages/scraper-framework/Dockerfile) bakes in a
#   list of operator-only scripts via explicit COPY lines:
#
#       COPY scripts/<name> /app/scripts/<name>
#
#   Today that list is maintained by hand: when a new operator-only
#   script is added that callers invoke via `python /app/scripts/<X>.py`
#   (e.g. on an ECS task command override), an agent or human has to
#   remember to add the COPY directive too. If they forget, the failure
#   mode is silent — the script works through scripts/ecs-run-task.sh
#   (which uploads a fresh copy via S3 every time) but does not work via
#   direct `python /app/scripts/<X>.py` invocation. That's exactly the
#   situation #4288 fixed for reingest_from_s3.py and rebuild_db.py.
#
#   This check is the hygiene gate that prevents recurrence: any new
#   /app/scripts/<X>.{py,sh} reference added without the corresponding
#   COPY in the scraper Dockerfile fails CI.
#
# What it checks
# ──────────────
# 1. Greps the repo for tokens of the form `/app/scripts/<name>.<ext>`
#    where <name> is a single segment (no slash) and <ext> is `py` or
#    `sh`. Subdirectory references (e.g. `/app/scripts/dispatcher/X.sh`)
#    are out of scope — those live in different image Dockerfiles
#    (Dockerfile.dispatcher*, Dockerfile.dispatcher-agent-runner) which
#    manage their own COPY rules.
# 2. Excludes paths the check should NOT scan:
#      - the scraper-framework Dockerfile itself (it CONTAINS the COPY
#        lines that satisfy references — counting them as references
#        would produce a tautology)
#      - all other Dockerfile* files in the repo (different images
#        manage their own COPY rules; their /app/scripts references are
#        their own concern, not the scraper image's)
#      - this check script itself and its test suite (they reference
#        /app/scripts/ in headers and fixtures)
#      - docs/  (descriptive, not invocation sites)
#      - tmp/   (worktree-local scratch)
#      - .git/, .venv/, node_modules/ (vendored / generated)
# 3. For each unique <name>.<ext> token found, asserts that
#    packages/scraper-framework/Dockerfile contains a matching
#    `COPY scripts/<name>.<ext> /app/scripts/<name>.<ext>` line.
# 4. Exits non-zero with a clear error naming each missing script and
#    the file:line where it was referenced.
#
# Usage:
#   scripts/check-scraper-image-shipped.sh
#
# Exit codes:
#   0 — all referenced scripts are COPY'd into the scraper image.
#   1 — one or more referenced scripts are missing a COPY directive.
#   2 — usage error or repo-structure assumption violated.
#
# Overrides (for the test harness; operators do not need these):
#   --repo-root <path>   Repo root to scan (default: detect from this
#                        script's location).
#   --dockerfile <path>  Scraper Dockerfile to check against
#                        (default: packages/scraper-framework/Dockerfile,
#                        relative to --repo-root).
#   --self-path <path>   Path of THIS check script relative to
#                        --repo-root, used for self-exclusion. Defaults
#                        to scripts/check-scraper-image-shipped.sh and
#                        is overridable so the test harness can run a
#                        copy of the check against a synthetic repo.

set -uo pipefail

# ── Defaults ───────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE_REL="packages/scraper-framework/Dockerfile"
SELF_REL="scripts/check-scraper-image-shipped.sh"
TEST_REL="scripts/tests/test_check_scraper_image_shipped.sh"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-root)
            REPO_ROOT="$2"
            shift 2
            ;;
        --dockerfile)
            DOCKERFILE_REL="$2"
            shift 2
            ;;
        --self-path)
            SELF_REL="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,/^set -uo pipefail$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

DOCKERFILE_ABS="$REPO_ROOT/$DOCKERFILE_REL"

if [[ ! -f "$DOCKERFILE_ABS" ]]; then
    echo "ERROR: scraper Dockerfile not found at $DOCKERFILE_ABS" >&2
    exit 2
fi

# ── Step 1: find every /app/scripts/<name>.{py,sh} reference ──────────
#
# Use grep -rEn with --exclude/--exclude-dir flags. The regex
# matches /app/scripts/<name>.py or /app/scripts/<name>.sh where
# <name> has at least one [A-Za-z_], may contain alphanumeric, _, -,
# and is followed immediately by .py or .sh and a non-word boundary.
# The trailing word-boundary check rejects e.g. /app/scripts/X.python
# and /app/scripts/X.shell.
PATTERN='/app/scripts/[A-Za-z_][A-Za-z0-9_-]*\.(py|sh)([^A-Za-z0-9_]|$)'

# Compute hits. Allow grep to exit 1 (no match) without tripping the
# whole script (we have set -uo pipefail, no -e, so this is fine).
HITS=$(
    grep -rEn "$PATTERN" "$REPO_ROOT" \
        --include='*.sh' \
        --include='*.py' \
        --include='*.tf' \
        --include='*.yml' \
        --include='*.yaml' \
        --include='*.json' \
        --include='*.md' \
        --include='*.txt' \
        --exclude-dir='.git' \
        --exclude-dir='.venv' \
        --exclude-dir='node_modules' \
        --exclude-dir='__pycache__' \
        --exclude-dir='dist' \
        --exclude-dir='build' \
        --exclude-dir='.next' \
        --exclude-dir='docs' \
        --exclude-dir='tmp' \
        2>/dev/null \
    | grep -v -F "$REPO_ROOT/$DOCKERFILE_REL:" \
    | grep -v -F "$REPO_ROOT/$SELF_REL:" \
    | grep -v -F "$REPO_ROOT/$TEST_REL:" \
    | grep -vE "$REPO_ROOT/Dockerfile[^/]*:" \
    | grep -vE "$REPO_ROOT/.+/Dockerfile[^/]*:"
) || true

# Note on the Dockerfile* exclusion: we drop hits in any Dockerfile in
# the repo (root-level Dockerfile.dispatcher* and per-package
# packages/<X>/Dockerfile, plus the .github/ecs-oneshot-test/Dockerfile
# fixture). Each image has its own COPY rules; their /app/scripts/
# references inside the image's own Dockerfile are not "callers
# expecting the scraper image to ship them" — they're declarations of
# what the OWN image ships. The Dockerfile-in-fixture-dir grep handles
# the .github/ecs-oneshot-test/Dockerfile case.

if [[ -z "$HITS" ]]; then
    # No /app/scripts/X.{py,sh} references anywhere in the repo (other
    # than the scraper Dockerfile itself and the self/test exclusions).
    # Nothing to check.
    echo "OK: no /app/scripts/<name>.{py,sh} references found outside the scraper Dockerfile"
    exit 0
fi

# ── Step 2: extract unique script names from the hits ─────────────────
#
# Each hit line looks like:
#   /repo/path/file.sh:42:    python /app/scripts/foo.py --bar
#
# We extract the basename token (foo.py) and dedupe. Keep the
# first-seen file:line for each token so the error message can name a
# concrete reference site.
#
# Bash 3.2 compat note (#3082): we cannot use associative arrays
# (`declare -A`). Instead we use two parallel indexed arrays: SCRIPT_NAMES
# holds the unique tokens in first-seen order, FIRST_REF_LINES holds the
# file:line for the same index. Lookup is by linear scan, which is fine
# because the count of unique tokens is tiny (≈10 at most in real-world
# use).
SCRIPT_NAMES=()
FIRST_REF_LINES=()

# Helper: 0 if $1 already in SCRIPT_NAMES, 1 otherwise.
_already_seen() {
    local needle="$1"
    local existing
    if [[ ${#SCRIPT_NAMES[@]} -eq 0 ]]; then
        return 1
    fi
    for existing in "${SCRIPT_NAMES[@]}"; do
        if [[ "$existing" == "$needle" ]]; then
            return 0
        fi
    done
    return 1
}

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    file_part="${line%%:*}"
    rest="${line#*:}"
    line_num="${rest%%:*}"

    # Extract every /app/scripts/<name>.<ext> token from this line.
    # A single line may contain multiple tokens (rare but possible).
    tokens=$(echo "$line" | grep -oE '/app/scripts/[A-Za-z_][A-Za-z0-9_-]*\.(py|sh)' | sort -u)
    while IFS= read -r tok; do
        [[ -z "$tok" ]] && continue
        name_ext="${tok#/app/scripts/}"
        if ! _already_seen "$name_ext"; then
            SCRIPT_NAMES+=("$name_ext")
            FIRST_REF_LINES+=("$file_part:$line_num")
        fi
    done <<< "$tokens"
done <<< "$HITS"

# Bash 3.2 + set -u: empty-array expansion is a fatal "unbound variable"
# unless the array was assigned at least once. We assigned to SCRIPT_NAMES
# above (even if no items were appended), so "${SCRIPT_NAMES[@]}" is safe
# below — but a guard for the empty case is clearer.
if [[ ${#SCRIPT_NAMES[@]} -eq 0 ]]; then
    echo "OK: no /app/scripts/<name>.{py,sh} references found outside the scraper Dockerfile"
    exit 0
fi

# ── Step 3: check each name has a matching COPY in the Dockerfile ─────
#
# A satisfying COPY line has the shape (whitespace-tolerant):
#   COPY scripts/<name>.<ext> /app/scripts/<name>.<ext>
#
# We do not allow alternate destinations — the contract is that the
# script is reachable at /app/scripts/<name>.<ext>.

MISSING_NAMES=()
MISSING_LINES=()
i=0
for name_ext in "${SCRIPT_NAMES[@]}"; do
    expected="COPY scripts/${name_ext} /app/scripts/${name_ext}"
    if ! grep -qF "$expected" "$DOCKERFILE_ABS"; then
        MISSING_NAMES+=("$name_ext")
        MISSING_LINES+=("${FIRST_REF_LINES[$i]}")
    fi
    i=$((i + 1))
done

if [[ ${#MISSING_NAMES[@]} -eq 0 ]]; then
    echo "OK: all ${#SCRIPT_NAMES[@]} /app/scripts/<name>.{py,sh} references are COPY'd into the scraper image"
    exit 0
fi

# ── Step 4: report missing scripts and exit non-zero ──────────────────

echo "FAIL: ${#MISSING_NAMES[@]} script(s) referenced as /app/scripts/<X>.{py,sh} but missing from $DOCKERFILE_REL:" >&2
i=0
for name_ext in "${MISSING_NAMES[@]}"; do
    echo "  - $name_ext (referenced at ${MISSING_LINES[$i]})" >&2
    i=$((i + 1))
done
echo "" >&2
echo "Fix: add a COPY line to $DOCKERFILE_REL alongside the existing scripts/* COPY block:" >&2
for name_ext in "${MISSING_NAMES[@]}"; do
    echo "  COPY scripts/${name_ext} /app/scripts/${name_ext}" >&2
done
echo "" >&2
echo "Why this matters: callers that invoke 'python /app/scripts/<X>.py' against the" >&2
echo "scraper image (e.g. via an ECS task command override) will fail at runtime if the" >&2
echo "script is not COPY'd in. The failure mode is silent under scripts/ecs-run-task.sh," >&2
echo "which uploads scripts via S3 every run — direct /app/scripts/ exec is the broken path." >&2
echo "See #4288 (the bug this check prevents recurring) and #4294 (the check itself)." >&2

exit 1
