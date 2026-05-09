#!/usr/bin/env bash
# permanent: true
# gh-comment-with-retry.sh — Post a `gh issue comment` and tolerate two
# distinct flaky failure modes (504-after-success + GraphQL-quota
# exhaustion).
#
# Why this helper exists
# ----------------------
# `gh issue comment <N> --body-file <file>` has two flaky failure modes
# that the bare `gh` call does not handle:
#
#   1. **504-after-success (#4478):** the request periodically returns a
#      `504 Gateway Timeout` (with a multi-KB HTML "Unicorn!" error page
#      in stderr) **after the comment has already posted**. A naive
#      retry creates a duplicate; treating it as a hard failure
#      misleads the agent about pipeline state. Recipe: re-fetch the
#      issue's recent comments, treat already-posted-with-matching-body
#      as success.
#
#   2. **GraphQL-quota-exhausted (#4503):** `gh issue comment` posts
#      the comment via GitHub's GraphQL API, which has a separate
#      5,000-req/hr quota from the REST core API. Long agent sessions
#      (dispatcher, multi-PR /task runs) regularly exhaust GraphQL —
#      consumed by `gh pr view`, `gh pr merge`, `gh issue view --json`,
#      etc. — while the REST core quota stays healthy. Recipe: fall
#      back to `gh api -X POST /repos/<owner>/<repo>/issues/<N>/comments
#      -F body=@<file>`. The REST endpoint is the same logical
#      operation and `-F body=@<file>` accepts the body the same way
#      `--body-file` does.
#
# Both failure modes share the same shape — the API can accept the
# request, the wrapper just needs the right fallback path. See
# `.claude/skills/task/SKILL.md` §A.7 for the `gh pr merge` 5xx
# analogue (#4231).
#
# Tracking: issues #4478 (504-after-success) and #4503 (GraphQL-quota
# REST fallback).
#
# Usage
# -----
#   scripts/gh-comment-with-retry.sh <issue-N> --body-file <path>
#   scripts/gh-comment-with-retry.sh <issue-N> --body-file <path> --repo <owner/name>
#   scripts/gh-comment-with-retry.sh --help
#
# Behavior
# --------
#   1. Calls ``gh issue comment <N> --repo <repo> --body-file <path>``.
#   2. On exit 0: prints the returned comment URL to stdout, exits 0.
#   3. On non-zero exit AND the captured stderr matches
#      ``GraphQL: API rate limit already exceeded`` (#4503):
#      - Falls back to ``gh api -X POST /repos/<owner>/<repo>/issues/<N>/comments
#        -F body=@<path>``.
#      - On REST success: prints "comment posted (REST fallback): <url>"
#        and exits 0.
#      - On REST failure: prints both stderrs (gh + REST, truncated to
#        5 KB each) and exits non-zero with the REST exit code.
#   4. On non-zero exit AND the captured stderr matches
#      ``504 Gateway Timeout`` or ``<title>Unicorn!`` (#4478):
#      - Re-fetches the issue's recent comments via
#        ``gh api /repos/<repo>/issues/<N>/comments?per_page=100&sort=created&direction=desc``.
#      - Filters to comments by the current ``gh`` user
#        (``gh api /user --jq .login``).
#      - Compares SHA-256 of the most recent comment's body to SHA-256
#        of the ``--body-file`` content.
#      - If they match: prints "comment posted (504 swallowed): <url>"
#        and exits 0.
#      - If they don't match: prints the captured stderr (truncated to
#        first 5 KB) and exits non-zero.
#   5. On any other non-zero exit (auth failure, generic 5xx, etc.):
#      prints the captured stderr and exits with the original code.
#
# Decision rules for the REST fallback (#4503)
# --------------------------------------------
# - Trigger ONLY on the explicit GraphQL-rate-limit-exceeded marker.
#   Generic 5xx failures hit the existing 504 path.
# - Do NOT trigger on 403 / authentication errors — those need
#   operator intervention and the REST endpoint will reject for the
#   same reason.
# - The REST endpoint (`POST /repos/.../issues/N/comments`) consumes
#   from the REST core 5,000-req/hr quota, which is rarely exhausted
#   even in long sessions.
#
# Rate-budget hygiene
# -------------------
# Stays well within the 5,000 reqs/hr GitHub API budget — at most one
# extra ``GET /repos/.../issues/<N>/comments`` and one ``GET /user``
# per 504, or one ``POST /repos/.../issues/<N>/comments`` per
# GraphQL-quota-exhaustion. No exponential backoff retries: a single
# confirm-or-fail round trip is enough.
#
# Integration
# -----------
# Replaces direct ``gh issue comment ... --body-file ...`` calls in
# ``.claude/skills/task/SKILL.md`` (Steps 2, A.2b, A.8 Step 3 — claim
# comment, process summary, verification evidence) and the
# documentation in ``.claude/skills/task-v2-summary/SKILL.md`` (process
# summary post — daemon side already retries via
# ``_gh_issue_comment``; the helper is the bash-path equivalent).
#
# Out of scope: ``gh pr comment`` calls share the same bug class but
# are rarer; a follow-up issue can pick those up.

set -euo pipefail

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

print_help() {
    cat << 'EOF'
gh-comment-with-retry.sh — post `gh issue comment` with 504-swallow recovery.

Usage:
  scripts/gh-comment-with-retry.sh <issue-N> --body-file <path>
  scripts/gh-comment-with-retry.sh <issue-N> --body-file <path> --repo <owner/name>
  scripts/gh-comment-with-retry.sh --help | -h

Arguments:
  <issue-N>            Issue number to comment on (digits, optional leading '#').
  --body-file <path>   Path to the comment body file.
  --repo <owner/name>  Repository (default: judgemind/judgemind).
  -h, --help           Show this help and exit.

Exit codes:
  0  — Comment posted successfully (including 504-after-success).
  1  — Real failure (auth, rate limit, real server failure, etc.).
  2  — Usage error (missing args, body-file not found).

See header comment for behavior details. Tracking: #4478.
EOF
}

if [[ $# -eq 0 ]]; then
    print_help >&2
    exit 2
fi

ISSUE=""
BODY_FILE=""
REPO="judgemind/judgemind"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            print_help
            exit 0
            ;;
        --body-file)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --body-file requires a value." >&2
                exit 2
            fi
            BODY_FILE="$2"
            shift 2
            ;;
        --repo)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --repo requires a value." >&2
                exit 2
            fi
            REPO="$2"
            shift 2
            ;;
        -*)
            echo "ERROR: unknown flag: $1" >&2
            print_help >&2
            exit 2
            ;;
        *)
            if [[ -z "$ISSUE" ]]; then
                ISSUE="${1#\#}"
            else
                echo "ERROR: unexpected positional argument: $1" >&2
                print_help >&2
                exit 2
            fi
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

if [[ -z "$ISSUE" ]]; then
    echo "ERROR: missing <issue-N> argument." >&2
    print_help >&2
    exit 2
fi

if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
    echo "ERROR: <issue-N> must be a positive integer, got '$ISSUE'." >&2
    exit 2
fi

if [[ -z "$BODY_FILE" ]]; then
    echo "ERROR: missing --body-file argument." >&2
    print_help >&2
    exit 2
fi

if [[ ! -f "$BODY_FILE" ]]; then
    echo "ERROR: --body-file does not exist: $BODY_FILE" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Step 1 — Try the comment post, capturing stdout and stderr separately.
# ---------------------------------------------------------------------------

# Use mktemp for the captured stderr so we can both inspect (for 504 signature)
# and replay (on real failure) without buffering megabytes in shell variables.
STDERR_FILE=$(mktemp)
STDOUT_FILE=$(mktemp)
# shellcheck disable=SC2064  # cleanup paths are fixed at trap-install time.
trap "rm -f '$STDERR_FILE' '$STDOUT_FILE'" EXIT

set +e
gh issue comment "$ISSUE" --repo "$REPO" --body-file "$BODY_FILE" \
    > "$STDOUT_FILE" 2> "$STDERR_FILE"
GH_EXIT=$?
set -e

if [[ "$GH_EXIT" -eq 0 ]]; then
    # Happy path — pass through stdout (the comment URL) and exit 0.
    cat "$STDOUT_FILE"
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 2 — Non-zero exit. Inspect stderr for known recoverable signatures.
# ---------------------------------------------------------------------------

# Step 2a — GraphQL-rate-limit-exceeded (#4503).
#
# `gh issue comment` posts via GitHub's GraphQL API. The GraphQL quota
# is a separate 5,000-req/hr bucket from the REST core quota and is
# routinely exhausted on long agent sessions. The explicit marker is:
#   "GraphQL: API rate limit already exceeded for user ID <N>."
# When we see it, fall back to the REST endpoint (which draws from the
# core quota) instead of failing the whole post.
if grep -qE "GraphQL: API rate limit already exceeded" "$STDERR_FILE"; then
    echo "gh-comment-with-retry: GraphQL rate-limit exhausted, falling back to REST API..." >&2

    # Split owner / repo for the REST URL. The wrapper validates REPO
    # as `<owner>/<repo>` upstream by passing `--repo` to gh; the
    # default is judgemind/judgemind. We don't need additional parsing
    # — the slash form is what the REST endpoint expects.
    REST_STDERR_FILE=$(mktemp)
    REST_STDOUT_FILE=$(mktemp)
    # shellcheck disable=SC2064  # cleanup paths fixed at trap-install time.
    trap "rm -f '$STDERR_FILE' '$STDOUT_FILE' '$REST_STDERR_FILE' '$REST_STDOUT_FILE'" EXIT

    set +e
    gh api -X POST "/repos/$REPO/issues/$ISSUE/comments" \
        -F "body=@$BODY_FILE" \
        --jq '.html_url' \
        > "$REST_STDOUT_FILE" 2> "$REST_STDERR_FILE"
    REST_EXIT=$?
    set -e

    if [[ "$REST_EXIT" -eq 0 ]]; then
        REST_URL=$(cat "$REST_STDOUT_FILE")
        echo "comment posted (REST fallback): $REST_URL"
        exit 0
    fi

    # REST also failed. Surface both stderrs so the operator can
    # diagnose (likely cause: the REST core quota is also exhausted,
    # or the body file changed mid-flight).
    echo "ERROR: GraphQL rate-limit fallback to REST also failed (gh exit $REST_EXIT)." >&2
    echo "  --- gh issue comment stderr (first 5 KB) ---" >&2
    head -c 5120 "$STDERR_FILE" >&2
    echo "" >&2
    echo "  --- gh api POST stderr (first 5 KB) ---" >&2
    head -c 5120 "$REST_STDERR_FILE" >&2
    echo "" >&2
    exit "$REST_EXIT"
fi

# Step 2b — 504-after-success (#4478).
#
# Two markers are diagnostic of the GitHub 504 / Unicorn page:
#   - Literal "504 Gateway Timeout" (HTTP status line gh sometimes echoes)
#   - "<title>Unicorn!" (HTML error page GitHub renders for 5xx)
# Either match triggers the re-fetch-and-compare path. Anything else is a
# real failure that should pass through.
if ! grep -qE "504 Gateway Timeout|<title>Unicorn!" "$STDERR_FILE"; then
    # Real failure. Pass stderr through and exit with the original code.
    cat "$STDERR_FILE" >&2
    exit "$GH_EXIT"
fi

# ---------------------------------------------------------------------------
# Step 3 — 504-after-success suspected. Re-fetch and compare bodies.
# ---------------------------------------------------------------------------

echo "gh-comment-with-retry: detected 504/Unicorn signature, verifying via comment list..." >&2

# Compute SHA-256 of the body file we tried to post. ``shasum -a 256`` is
# available on macOS and Linux; ``sha256sum`` is Linux-only. Prefer
# ``shasum`` for portability with Fargate + operator macOS laptops.
if command -v shasum >/dev/null 2>&1; then
    LOCAL_SHA=$(shasum -a 256 "$BODY_FILE" | awk '{print $1}')
elif command -v sha256sum >/dev/null 2>&1; then
    LOCAL_SHA=$(sha256sum "$BODY_FILE" | awk '{print $1}')
else
    echo "ERROR: neither shasum nor sha256sum is available — cannot verify." >&2
    cat "$STDERR_FILE" >&2
    exit "$GH_EXIT"
fi

# Fetch current gh user login. Used to filter the comment list to comments
# WE posted (avoids matching a coincidentally-identical comment from
# someone else who happened to post moments before our attempt).
GH_USER=$(gh api /user --jq '.login' 2>/dev/null) || GH_USER=""
if [[ -z "$GH_USER" ]]; then
    echo "ERROR: could not resolve gh user login — cannot verify." >&2
    cat "$STDERR_FILE" >&2
    exit "$GH_EXIT"
fi

# Fetch the most recent 100 comments, sorted descending by creation. The
# helper compares the FIRST (most recent) comment by GH_USER against the
# body file. ``per_page=100`` is enough for any plausible thread; the
# direction=desc + first-by-user pattern is robust to other-user comments
# that may have arrived in between.
COMMENTS_JSON=$(gh api \
    "/repos/$REPO/issues/$ISSUE/comments?per_page=100&sort=created&direction=desc" \
    2>/dev/null) || {
    echo "ERROR: could not fetch comment list for verification." >&2
    cat "$STDERR_FILE" >&2
    exit "$GH_EXIT"
}

# Extract the most recent comment by GH_USER as a tab-separated row:
#   <html_url>\t<body-sha256>
# We let jq compute the sha256 via @sha256d? Nope — jq has no sha256
# builtin in the GitHub-shipped version. Compute it externally: jq prints
# just the body, then we shasum it. Two-step pipeline keeps the data flow
# simple and avoids embedding a sha256-in-jq workaround.
LATEST_BODY_FILE=$(mktemp)
LATEST_URL=""
# shellcheck disable=SC2064  # cleanup paths are fixed at trap-install time.
trap "rm -f '$STDERR_FILE' '$STDOUT_FILE' '$LATEST_BODY_FILE'" EXIT

# Walk the JSON via the sibling python helper — same pattern as the
# scripts/_check_shipped_pr_*.py family. The helper writes the matched
# comment's body to LATEST_BODY_FILE and prints html_url on stdout.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LATEST_URL=$(
    GH_USER="$GH_USER" \
    LATEST_BODY_FILE="$LATEST_BODY_FILE" \
    python3 "$SCRIPT_DIR/_gh_comment_with_retry_match.py" <<< "$COMMENTS_JSON"
) || {
    echo "ERROR: no recent comment by '$GH_USER' found on issue #$ISSUE — original 504 was not a swallowed-success." >&2
    # Truncate stderr to first 5 KB before passthrough.
    head -c 5120 "$STDERR_FILE" >&2
    echo "" >&2
    exit "$GH_EXIT"
}

# Compute SHA-256 of the fetched body and compare.
if command -v shasum >/dev/null 2>&1; then
    REMOTE_SHA=$(shasum -a 256 "$LATEST_BODY_FILE" | awk '{print $1}')
else
    REMOTE_SHA=$(sha256sum "$LATEST_BODY_FILE" | awk '{print $1}')
fi

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
    echo "comment posted (504 swallowed): $LATEST_URL"
    exit 0
fi

# Bodies don't match — the 504 was a real failure (or our comment got
# clobbered by a racing other-user post). Treat as failure.
echo "ERROR: 504 detected but most recent comment by '$GH_USER' does not match body-file content." >&2
echo "  local sha:  $LOCAL_SHA" >&2
echo "  remote sha: $REMOTE_SHA" >&2
echo "  remote url: $LATEST_URL" >&2
echo "" >&2
# Truncate stderr to first 5 KB before passthrough.
head -c 5120 "$STDERR_FILE" >&2
echo "" >&2
exit "$GH_EXIT"
