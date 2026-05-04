#!/usr/bin/env bash
# block-on-new-issue.sh — Atomically file a new tracking issue AND mark another
# issue as blocked by it.
#
# This is the canonical wrapper for the "BLOCK on a new issue" pattern that
# /task agents need when they discover an upstream blocker that does not yet
# have a GitHub issue (e.g. a billing problem, an operator-only handoff, an
# infra failure with no existing tracker). Without this helper, agents tend
# to do part of the workflow (post a BLOCKED comment) but skip the durable
# parts (file the tracker, set `Blocked by #N`, add `status/blocked`). The
# next agent then re-investigates the same upstream cause.
#
# What this script does, atomically from the operator's perspective:
#   1. Creates a new GitHub issue (`gh issue create`) with the supplied
#      title / body-file / labels. Captures its number.
#   2. Calls `scripts/block-issue.sh <dependent> <new-issue>` which:
#       - Adds `Blocked by #<new>` to the dependent's body (under a
#         `## Dependencies` section, creating it if absent).
#       - Adds the `status/blocked` label and removes `agent/ready`.
#   3. Prints both numbers (and URLs) to stdout so the caller can reference
#      them in a follow-up comment / PR / log line.
#
# Usage:
#   scripts/block-on-new-issue.sh <dependent-issue> \
#       --title "<conventional-commits style title>" \
#       --body-file <path> \
#       [--label <label> ...] \
#       [--priority p0|p1|p2|p3] \
#       [--repo <owner/name>]    # default: judgemind/judgemind
#
# Notes:
# - --label may be specified multiple times. Common labels for tracker issues
#   include the relevant `area/*` and `type/*` labels. Do NOT pass
#   `agent/ready` if the new issue requires operator action — agent-ready on
#   an operator-only blocker just sends another agent down the same dead end.
# - --priority is sugar for `--label priority/<level>`; supplying both is fine
#   (label dedup is handled by GitHub).
# - The new issue's body MUST come from a file (`--body-file`) rather than
#   inline — this matches the rest of the codebase's convention (see
#   CLAUDE.md §Issue Creation in MEMORY.md) and avoids quoting headaches.
# - On any failure after the new issue is created, the script does NOT delete
#   the new issue. The operator can either edit it or close it manually. The
#   common failure mode (block-issue.sh fails) leaves a tracker that the
#   operator can wire up by hand with `scripts/block-issue.sh`.

set -euo pipefail

usage() {
    cat <<'USAGE' >&2
Usage:
  scripts/block-on-new-issue.sh <dependent-issue> \
      --title "<title>" \
      --body-file <path> \
      [--label <label> ...] \
      [--priority p0|p1|p2|p3] \
      [--repo <owner/name>]

Files a new tracker issue and marks <dependent-issue> as Blocked by it.
USAGE
}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

case "$1" in
    -h|--help)
        usage
        exit 0
        ;;
esac

DEPENDENT="${1#\#}"
shift

if ! [[ "$DEPENDENT" =~ ^[0-9]+$ ]]; then
    echo "Error: <dependent-issue> must be an issue number (digits only). Got: $DEPENDENT" >&2
    usage
    exit 1
fi

TITLE=""
BODY_FILE=""
LABELS=()
PRIORITY=""
REPO="judgemind/judgemind"

while [ $# -gt 0 ]; do
    case "$1" in
        --title)
            TITLE="${2:-}"
            shift 2
            ;;
        --body-file)
            BODY_FILE="${2:-}"
            shift 2
            ;;
        --label)
            LABELS+=("${2:-}")
            shift 2
            ;;
        --priority)
            PRIORITY="${2:-}"
            shift 2
            ;;
        --repo)
            REPO="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [ -z "$TITLE" ]; then
    echo "Error: --title is required." >&2
    usage
    exit 1
fi

if [ -z "$BODY_FILE" ]; then
    echo "Error: --body-file is required." >&2
    usage
    exit 1
fi

if [ ! -f "$BODY_FILE" ]; then
    echo "Error: --body-file '$BODY_FILE' does not exist or is not a regular file." >&2
    exit 1
fi

if [ -n "$PRIORITY" ]; then
    case "$PRIORITY" in
        p0|p1|p2|p3)
            LABELS+=("priority/$PRIORITY")
            ;;
        *)
            echo "Error: --priority must be one of p0, p1, p2, p3. Got: $PRIORITY" >&2
            exit 1
            ;;
    esac
fi

# Resolve the script's own directory so we can call sibling helpers reliably
# regardless of caller cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$SCRIPT_DIR/block-issue.sh" ]; then
    echo "Error: companion '$SCRIPT_DIR/block-issue.sh' is not executable." >&2
    exit 1
fi

# Build the gh issue create invocation.
CREATE_ARGS=(issue create --repo "$REPO" --title "$TITLE" --body-file "$BODY_FILE")
for label in "${LABELS[@]}"; do
    if [ -n "$label" ]; then
        CREATE_ARGS+=(--label "$label")
    fi
done

echo "Creating new tracker issue in $REPO..." >&2
NEW_URL="$(gh "${CREATE_ARGS[@]}")"

if [ -z "$NEW_URL" ]; then
    echo "Error: 'gh issue create' returned an empty URL." >&2
    exit 1
fi

# Extract the issue number from the URL (last path segment).
NEW_NUM="${NEW_URL##*/}"
if ! [[ "$NEW_NUM" =~ ^[0-9]+$ ]]; then
    echo "Error: could not parse issue number from URL: $NEW_URL" >&2
    exit 1
fi

echo "Created issue #$NEW_NUM ($NEW_URL)" >&2

# Wire the dependency. block-issue.sh handles label + body atomically.
"$SCRIPT_DIR/block-issue.sh" "$DEPENDENT" "$NEW_NUM"

echo
echo "Done."
echo "  new tracker: #$NEW_NUM ($NEW_URL)"
echo "  dependent:   #$DEPENDENT (now Blocked by #$NEW_NUM)"
