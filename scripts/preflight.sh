#!/usr/bin/env bash
# scripts/preflight.sh — Runtime validation functions for agent workflows.
#
# Source this file, then call individual checks:
#   source scripts/preflight.sh
#   preflight_in_worktree || exit 1
#   preflight_not_on_main || exit 1
#
# Each function prints a diagnostic on failure and returns non-zero.
# All functions are safe to call from any directory.

# --------------------------------------------------------------------------
# preflight_in_worktree
#   Verify that the current directory is inside a git worktree, not the main
#   repo checkout. Prevents agents from accidentally modifying the shared
#   main checkout or using its venv.
# --------------------------------------------------------------------------
preflight_in_worktree() {
    local git_dir
    git_dir=$(git rev-parse --git-dir 2>/dev/null) || {
        echo "PREFLIGHT FAIL: Not inside a git repository." >&2
        return 1
    }

    # In a worktree, --git-dir returns a path ending in .git/worktrees/<name>.
    # In the main repo, it returns just ".git".
    if [[ "$git_dir" == *"/worktrees/"* ]]; then
        return 0
    fi

    echo "PREFLIGHT FAIL: Current directory is the main repo checkout, not a worktree." >&2
    echo "  pwd: $(pwd)" >&2
    echo "  Use the dispatcher's isolation: \"worktree\" mode to create a worktree." >&2
    return 1
}

# --------------------------------------------------------------------------
# preflight_not_on_main
#   Verify the current branch is not main/master. Prevents accidental
#   commits or pushes to the default branch during task work.
# --------------------------------------------------------------------------
preflight_not_on_main() {
    local branch
    branch=$(git symbolic-ref --short HEAD 2>/dev/null) || {
        echo "PREFLIGHT FAIL: HEAD is detached — not on any branch." >&2
        return 1
    }

    if [[ "$branch" == "main" || "$branch" == "master" ]]; then
        echo "PREFLIGHT FAIL: On branch '$branch'. Task work must happen on a feature branch." >&2
        echo "  Use the dispatcher's isolation: \"worktree\" mode, or checkout a feature branch." >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_branch_fresh
#   Verify the current branch is not behind origin/main. Catches the common
#   mistake of analyzing or modifying stale code.
#   Pass --fetch to also run git fetch origin main first.
# --------------------------------------------------------------------------
preflight_branch_fresh() {
    if [[ "${1:-}" == "--fetch" ]]; then
        git fetch origin main --quiet 2>/dev/null || true
    fi

    local behind
    behind=$(git rev-list --count HEAD..origin/main 2>/dev/null) || {
        echo "PREFLIGHT WARN: Could not determine if branch is behind origin/main." >&2
        return 0  # Don't block if we can't check (e.g., no remote)
    }

    if [[ "$behind" -gt 0 ]]; then
        echo "PREFLIGHT FAIL: Branch is $behind commit(s) behind origin/main." >&2
        echo "  Run: git fetch origin main && git rebase origin/main" >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_venv_local [path]
#   Verify that a .venv directory exists in the given path (default: pwd)
#   and that it is NOT a symlink to another worktree's venv.
# --------------------------------------------------------------------------
preflight_venv_local() {
    local dir="${1:-.}"
    local venv_path="$dir/.venv"

    if [[ ! -d "$venv_path" ]]; then
        echo "PREFLIGHT FAIL: No .venv found at $venv_path" >&2
        echo "  Create one: python3.12 -m venv $venv_path" >&2
        return 1
    fi

    # Check that .venv is not a symlink pointing outside this directory
    if [[ -L "$venv_path" ]]; then
        local target
        target=$(readlink "$venv_path")
        echo "PREFLIGHT FAIL: .venv at $venv_path is a symlink to $target" >&2
        echo "  Each worktree must have its own venv. Create a fresh one:" >&2
        echo "  rm $venv_path && python3.12 -m venv $venv_path" >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_tf_not_root [path]
#   Verify that a terraform apply/destroy is not being run from the root
#   infra/terraform/ directory. The root config shares module sources with
#   environment-specific configs but has its own state backend (key
#   "terraform.tfstate"). Running apply from the root creates duplicate
#   resources that collide with the real ones in environments/<env>/.
#
#   Pass the -chdir path or cwd if not provided.
#   Returns 0 (OK) if the path is an environment directory.
#   Returns 1 (FAIL) if the path is the root infra/terraform/ directory.
# --------------------------------------------------------------------------
preflight_tf_not_root() {
    local tf_dir="${1:-.}"

    # Resolve to absolute path
    tf_dir=$(cd "$tf_dir" 2>/dev/null && pwd) || tf_dir="$1"

    # Check if the path ends with infra/terraform (the root) rather than
    # infra/terraform/environments/<env>
    if [[ "$tf_dir" =~ infra/terraform/?$ ]]; then
        echo "PREFLIGHT FAIL: terraform apply/destroy from root infra/terraform/ is forbidden." >&2
        echo "  The root directory does not track deployed resources." >&2
        echo "  Use an environment-specific path instead:" >&2
        echo "    infra/terraform/environments/dev/" >&2
        echo "    infra/terraform/environments/production/" >&2
        echo "" >&2
        echo "  Example: terraform -chdir=infra/terraform/environments/dev apply" >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_no_duplicate_pr <issue_number>
#   Check whether an open PR already exists for the given issue number.
#   Prevents duplicate PRs when a session loses context (e.g. after context
#   compaction) or when multiple agents pick up the same issue.
#
#   Returns 0 and prints the existing PR number to stdout if a duplicate is
#   found (caller should adopt that PR instead of creating a new one).
#   Returns 1 if no duplicate exists (safe to create a new PR).
#   Returns 2 on error (e.g. gh CLI not available).
#
#   Usage:
#     existing_pr=$(preflight_no_duplicate_pr 42) && {
#         echo "Adopting existing PR #$existing_pr"
#     } || {
#         echo "No duplicate — safe to create PR"
#     }
# --------------------------------------------------------------------------
preflight_no_duplicate_pr() {
    local issue_number="${1:-}"

    if [[ -z "$issue_number" ]]; then
        echo "PREFLIGHT FAIL: preflight_no_duplicate_pr requires an issue number argument." >&2
        return 2
    fi

    # Strip leading # if present
    issue_number="${issue_number#\#}"

    if ! command -v gh &>/dev/null; then
        echo "PREFLIGHT WARN: gh CLI not found — cannot check for duplicate PRs." >&2
        return 2
    fi

    # Search for open PRs that reference this issue number in the title.
    # The title format is typically "feat(area): description (#N)" so we
    # search for "(#N)" to avoid false positives from unrelated numbers.
    local pr_json
    pr_json=$(gh pr list --repo judgemind/judgemind \
        --state open \
        --search "in:title #${issue_number}" \
        --json number,title \
        --limit 10 2>/dev/null) || {
        echo "PREFLIGHT WARN: gh pr list failed — cannot check for duplicate PRs." >&2
        return 2
    }

    # Parse results: look for PRs whose title contains "(#N)" or "#N"
    # to confirm the match (the search API can return fuzzy matches).
    local matching_pr=""
    matching_pr=$(echo "$pr_json" | python3 -c "
import sys, json, re
data = json.load(sys.stdin)
issue = '${issue_number}'
for pr in data:
    title = pr.get('title', '')
    # Match (#N) at the end of conventional commit titles, or #N anywhere
    if re.search(r'\(#' + re.escape(issue) + r'\)', title) or re.search(r'#' + re.escape(issue) + r'\b', title):
        print(pr['number'])
        break
" 2>/dev/null) || {
        echo "PREFLIGHT WARN: Could not parse PR search results." >&2
        return 2
    }

    if [[ -n "$matching_pr" ]]; then
        echo "PREFLIGHT WARN: Open PR #${matching_pr} already exists for issue #${issue_number}." >&2
        echo "  Adopt the existing PR instead of creating a new one." >&2
        echo "$matching_pr"
        return 0
    fi

    # Also check by branch name pattern — branches often contain the issue number
    local branch_name
    branch_name=$(git symbolic-ref --short HEAD 2>/dev/null) || true

    if [[ -n "$branch_name" ]]; then
        local branch_pr_json
        branch_pr_json=$(gh pr list --repo judgemind/judgemind \
            --state open \
            --head "$branch_name" \
            --json number \
            --limit 1 2>/dev/null) || true

        local branch_pr
        branch_pr=$(echo "$branch_pr_json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data:
    print(data[0]['number'])
" 2>/dev/null) || true

        if [[ -n "$branch_pr" ]]; then
            echo "PREFLIGHT WARN: Open PR #${branch_pr} already exists for branch '${branch_name}'." >&2
            echo "  Adopt the existing PR instead of creating a new one." >&2
            echo "$branch_pr"
            return 0
        fi
    fi

    # No duplicate found
    return 1
}

# --------------------------------------------------------------------------
# preflight_rate_budget [threshold]
#   Check the GitHub API rate limit and warn if remaining requests are below
#   the given threshold (default: 100). Useful before expensive polling
#   operations like `gh run watch`.
#
#   Returns 0 if budget is sufficient (remaining >= threshold).
#   Returns 1 if budget is low (remaining < threshold).
#   Returns 2 on error (e.g., gh CLI not available or API unreachable).
#
#   Prints remaining request count and reset time to stderr.
#
#   Usage:
#     preflight_rate_budget         # warn if < 100 remaining
#     preflight_rate_budget 200     # warn if < 200 remaining
# --------------------------------------------------------------------------
preflight_rate_budget() {
    local threshold="${1:-100}"

    if ! command -v gh &>/dev/null; then
        echo "PREFLIGHT WARN: gh CLI not found — cannot check rate limit." >&2
        return 2
    fi

    local rate_json
    rate_json=$(gh api rate_limit --jq '.rate | "\(.remaining) \(.reset) \(.limit)"' 2>/dev/null) || {
        echo "PREFLIGHT WARN: Could not query GitHub API rate limit." >&2
        return 2
    }

    # Parse "remaining reset_epoch limit"
    local remaining reset_epoch limit
    remaining=$(echo "$rate_json" | awk '{print $1}')
    reset_epoch=$(echo "$rate_json" | awk '{print $2}')
    limit=$(echo "$rate_json" | awk '{print $3}')

    if [[ -z "$remaining" || -z "$reset_epoch" ]]; then
        echo "PREFLIGHT WARN: Could not parse rate limit response." >&2
        return 2
    fi

    echo "GitHub API rate budget: $remaining/$limit remaining (resets at epoch $reset_epoch)." >&2

    if [[ "$remaining" -lt "$threshold" ]]; then
        echo "PREFLIGHT WARN: Rate budget low — $remaining requests remaining (threshold: $threshold)." >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_no_forbidden_syntax <command_string>
#   Check a command string for forbidden shell patterns. Mirrors the checks
#   in .claude/hooks/preflight-bash.sh but can be called from scripts.
# --------------------------------------------------------------------------
preflight_no_forbidden_syntax() {
    local cmd="$1"

    if echo "$cmd" | grep -qE '\$\('; then
        echo "PREFLIGHT FAIL: Command contains \$() substitution." >&2
        return 1
    fi

    if echo "$cmd" | grep -qE '<<-?[[:space:]]*["'"'"']?[A-Za-z_]+["'"'"']?'; then
        echo "PREFLIGHT FAIL: Command contains a heredoc." >&2
        return 1
    fi

    if echo "$cmd" | grep -qE 'python3?[[:space:]]+-c[[:space:]]'; then
        echo "PREFLIGHT FAIL: Command contains inline python -c." >&2
        return 1
    fi

    return 0
}
