#!/usr/bin/env bash
# test_check_markdown_links.sh — Tests for check-markdown-links.sh
#
# Creates temporary markdown files in a scratch directory and runs the
# checker against them to verify detection of broken pointers (both
# Markdown-style [text](path.md) links and backtick `path.md` references),
# while allowing legitimate uses (external URLs, existing files, prose).
#
# Usage:
#   scripts/tests/test_check_markdown_links.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-markdown-links.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    printf '%s\n' "$content" > "$TMPDIR_TEST/$name"
}

# Run the checker with --repo-root pointing at the temp dir and explicit
# file paths so we don't scan any real files.
run_checker() {
    local file="$1"
    "$CHECK_SCRIPT" --repo-root "$TMPDIR_TEST" "$TMPDIR_TEST/$file"
}

# Run the checker with --repo-root pointing at the temp dir and NO
# explicit paths, so it uses DEFAULT_SCAN_GLOBS against the temp dir.
# Used to verify the default scan set actually picks up a file.
run_checker_default_scan() {
    "$CHECK_SCRIPT" --repo-root "$TMPDIR_TEST"
}

assert_default_scan_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if run_checker_default_scan > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure from default scan, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_fails() {
    local desc="$1"
    local file="$2"
    TESTS=$((TESTS + 1))
    if run_checker "$file" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    local file="$2"
    TESTS=$((TESTS + 1))
    if run_checker "$file" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        run_checker "$file" || true
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    mkdir -p "$TMPDIR_TEST"
}

# ─── Test 1: Broken Markdown link should fail ────────────────────────────
create_test_file "CLAUDE.md" 'See [the guide](docs/missing.md) for details.'
assert_fails "Broken markdown link to docs/missing.md detected" "CLAUDE.md"
reset_tmpdir

# ─── Test 2: Valid Markdown link should pass ─────────────────────────────
create_test_file "CLAUDE.md" 'See [the guide](docs/guide.md) for details.'
create_test_file "docs/guide.md" '# Guide'
assert_passes "Valid markdown link to existing file passes" "CLAUDE.md"
reset_tmpdir

# ─── Test 3: Broken backtick reference should fail ──────────────────────
create_test_file "CLAUDE.md" 'For full details, see `docs/agent/missing-ref.md`.'
assert_fails "Broken backtick reference detected" "CLAUDE.md"
reset_tmpdir

# ─── Test 4: Valid backtick reference should pass ──────────────────────
create_test_file "CLAUDE.md" 'For full details, see `docs/agent/unattended-patterns.md`.'
create_test_file "docs/agent/unattended-patterns.md" '# Patterns'
assert_passes "Valid backtick reference passes" "CLAUDE.md"
reset_tmpdir

# ─── Test 5: External HTTPS link should pass ───────────────────────────
create_test_file "CLAUDE.md" 'See [the spec](https://example.com/spec.md) for details.'
assert_passes "External https link is not checked" "CLAUDE.md"
reset_tmpdir

# ─── Test 6: External HTTP link should pass ────────────────────────────
create_test_file "CLAUDE.md" 'See [the spec](http://example.com/spec.md) for details.'
assert_passes "External http link is not checked" "CLAUDE.md"
reset_tmpdir

# ─── Test 7: Markdown link with anchor should strip anchor ─────────────
create_test_file "CLAUDE.md" 'See [section](docs/guide.md#some-section) for details.'
create_test_file "docs/guide.md" '# Guide'
assert_passes "Anchor-suffixed valid link passes" "CLAUDE.md"
reset_tmpdir

# ─── Test 8: Markdown link with anchor to missing file fails ───────────
create_test_file "CLAUDE.md" 'See [section](docs/missing.md#some-section) for details.'
assert_fails "Anchor-suffixed broken link fails" "CLAUDE.md"
reset_tmpdir

# ─── Test 9: Markdown link relative to file's directory ────────────────
create_test_file "docs/page.md" 'See [other](other.md) for details.'
create_test_file "docs/other.md" '# Other'
assert_passes "Markdown link resolves relative to file directory" "docs/page.md"
reset_tmpdir

# ─── Test 10: Markdown link with ../ navigation ────────────────────────
create_test_file "docs/sub/page.md" 'See [top](../top.md) for details.'
create_test_file "docs/top.md" '# Top'
assert_passes "Markdown link with ../ navigation resolves" "docs/sub/page.md"
reset_tmpdir

# ─── Test 11: Backtick reference is repo-rooted (not file-relative) ────
# Even though docs/web-patterns.md is written inside docs/, the backtick
# reference `docs/web-lessons.md` is interpreted as a repo-rooted path.
create_test_file "docs/web-patterns.md" 'See `docs/web-lessons.md` for details.'
create_test_file "docs/web-lessons.md" '# Lessons'
assert_passes "Backtick ref from docs/ resolves repo-rooted" "docs/web-patterns.md"
reset_tmpdir

# ─── Test 12: Backtick ref to CLAUDE.md at repo root ───────────────────
create_test_file "docs/page.md" 'Read `CLAUDE.md` first.'
create_test_file "CLAUDE.md" '# Rules'
assert_passes "Backtick ref to top-level CLAUDE.md resolves" "docs/page.md"
reset_tmpdir

# ─── Test 13: Backtick prose reference to non-existent CLAUDE ─────────
# When CLAUDE.md doesn't exist at repo root, the prose mention fails.
create_test_file "docs/page.md" 'Read `CLAUDE.md` first.'
assert_fails "Backtick ref to missing top-level CLAUDE.md fails" "docs/page.md"
reset_tmpdir

# ─── Test 14: Backtick token with internal whitespace is ignored ───────
# Shell commands that include an .md filename mid-command should not be
# flagged.  Example:
#   `scripts/write-claude-file.sh {worktree}/tmp/file.md {worktree}/.claude/foo.md`
create_test_file "CLAUDE.md" 'Use `scripts/write-claude-file.sh some/file.md other/file.md` to write.'
assert_passes "Multi-word backtick token (command) is ignored" "CLAUDE.md"
reset_tmpdir

# ─── Test 15: Backtick token that is a bare filename (no directory) ────
# Bare filenames without a directory prefix are not treated as repo paths.
create_test_file "CLAUDE.md" 'Edit the `SKILL.md` file in the appropriate directory.'
assert_passes "Bare filename SKILL.md in prose is ignored" "CLAUDE.md"
reset_tmpdir

# ─── Test 16: Clean file with no links should pass ─────────────────────
create_test_file "CLAUDE.md" '# Project

This is a project with no markdown links.'
assert_passes "File with no markdown links passes" "CLAUDE.md"
reset_tmpdir

# ─── Test 17: Triple duplicate broken links ────────────────────────────
create_test_file "CLAUDE.md" 'See [a](docs/a.md), [b](docs/b.md), and [c](docs/c.md).'
assert_fails "Multiple broken links are detected" "CLAUDE.md"
reset_tmpdir

# ─── Test 18: Link with query string is treated normally ───────────────
create_test_file "CLAUDE.md" 'See [guide](docs/guide.md).'
create_test_file "docs/guide.md" '# Guide'
assert_passes "Simple valid link passes" "CLAUDE.md"
reset_tmpdir

# ─── Test 19: Non-md link targets are not checked ──────────────────────
# The checker only validates links that end with .md — image links,
# other file types are not in scope.
create_test_file "CLAUDE.md" 'See ![img](images/missing.png) and [script](scripts/run.sh).'
assert_passes "Non-md link targets are ignored" "CLAUDE.md"
reset_tmpdir

# ─── Test 20: Backtick ref with anchor ─────────────────────────────────
create_test_file "CLAUDE.md" 'See `docs/guide.md#setup` for setup instructions.'
create_test_file "docs/guide.md" '# Guide'
assert_passes "Backtick ref with anchor resolves" "CLAUDE.md"
reset_tmpdir

# ─── Test 21: Backtick ref to missing file with anchor fails ──────────
create_test_file "CLAUDE.md" 'See `docs/missing.md#setup` for setup instructions.'
assert_fails "Backtick ref with anchor to missing file fails" "CLAUDE.md"
reset_tmpdir

# ─── Test 22: Reproduction of #2400 — backtick ref pattern ────────────
# Before this check existed, CLAUDE.md contained:
#   see `docs/agent/telegram-reference.md`
# with no such file in the repo.
create_test_file "CLAUDE.md" 'Telegram integration is opt-in. For full details, see `docs/agent/telegram-reference.md`.'
create_test_file "docs/agent/other-file.md" '# Other'
assert_fails "Issue #2400 repro — backtick ref to missing telegram-reference.md" "CLAUDE.md"
reset_tmpdir

# ─── Test 23: Default scan picks up packages/*/README.md (#2428) ───────
# A broken link inside packages/scraper-framework/README.md must be
# caught by the default scan (no explicit paths passed).
create_test_file "packages/scraper-framework/README.md" \
    'See [spec](../../docs/specs/missing.md) for architecture.'
assert_default_scan_fails \
    "Default scan catches broken link in packages/*/README.md"
reset_tmpdir

# ─── Test 24: Default scan picks up packages/*/docs/**/*.md (#2428) ────
create_test_file "packages/api/docs/guide.md" \
    'See [other](../missing.md) for details.'
assert_default_scan_fails \
    "Default scan catches broken link in packages/*/docs/**/*.md"
reset_tmpdir

# ─── Test 25: Default scan picks up infra/**/*.md (#2428) ──────────────
create_test_file "infra/terraform/README.md" \
    'See `docs/missing-infra-ref.md` for details.'
assert_default_scan_fails \
    "Default scan catches broken backtick ref in infra/**/*.md"
reset_tmpdir

# ─── Test 26: Default scan picks up .github/**/*.md (#2428) ────────────
create_test_file ".github/ISSUE_TEMPLATE/task.md" \
    'See [guide](../../docs/missing-template.md) for guidance.'
assert_default_scan_fails \
    "Default scan catches broken link in .github/**/*.md"
reset_tmpdir

# ─── Test 27: Default scan picks up top-level AGENTS.md (#2428) ────────
create_test_file "AGENTS.md" \
    'See [claude](CLAUDE.md) for rules.'
# CLAUDE.md intentionally not created -> broken link
assert_default_scan_fails \
    "Default scan catches broken link in top-level AGENTS.md"
reset_tmpdir

# ─── Test 28: Default scan picks up top-level README.md (#2428) ────────
create_test_file "README.md" \
    'See [contributing](CONTRIBUTING.md) for details.'
# CONTRIBUTING.md intentionally not created -> broken link
assert_default_scan_fails \
    "Default scan catches broken link in top-level README.md"
reset_tmpdir

# ─── Test 29: Default scan picks up top-level CONTRIBUTING.md (#2428) ──
create_test_file "CONTRIBUTING.md" \
    'See [readme](README.md) for overview.'
# README.md intentionally not created -> broken link
assert_default_scan_fails \
    "Default scan catches broken link in top-level CONTRIBUTING.md"
reset_tmpdir

# ─── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
