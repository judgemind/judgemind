#!/usr/bin/env bash
# check-markdown-links.sh — Validate internal markdown pointers.
#
# Scans CLAUDE.md, docs/**/*.md, and .claude/skills/**/*.md for broken
# pointers to other markdown files (either Markdown-style links
# [text](path.md) or backtick references `docs/foo.md`).
#
# Usage:
#   scripts/check-markdown-links.sh          # scan the default file set
#   scripts/check-markdown-links.sh [paths]  # scan specific files
#
# Exit codes:
#   0 — All links resolve.
#   1 — One or more broken links found.
#
# Ref: https://github.com/judgemind/judgemind/issues/2425

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-markdown-links.py" "$@"
