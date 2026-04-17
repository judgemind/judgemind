#!/usr/bin/env python3
"""check-markdown-links.py — Validate internal markdown pointers.

Scans the agent-facing and contributor-facing Markdown surfaces for
pointers to other markdown files and reports any that point to files
that do not exist.  The default scan set covers:

* top-level docs (CLAUDE.md, README.md, AGENTS.md, CONTRIBUTING.md)
* docs/**/*.md
* .claude/skills/**/*.md
* packages/*/README.md and packages/*/docs/**/*.md
* infra/**/*.md
* .github/**/*.md

Two link styles are checked:

1. Markdown links:   [text](relative/path.md)  or  [text](path.md#anchor)
2. Backtick refs:    `docs/agent/path.md`  or  `.claude/skills/x/SKILL.md`

Backtick references are the common style in CLAUDE.md (e.g. see
`docs/agent/infrastructure-reference.md`), and were the pattern behind the
#2400 bug that motivated this check.

External URLs (http://, https://, mailto:) are skipped.  Anchors (#...) are
stripped before checking file existence.  Backtick references are only
flagged when the token *looks like* a repo-relative path — i.e. it starts
with a known top-level directory prefix (docs/, packages/, scripts/,
infra/, .claude/, .github/) or is a top-level markdown file (CLAUDE.md,
README.md, AGENTS.md).  This avoids false positives on filenames used in
prose like `schema.sql` or `SKILL.md` without a path.

Usage:
    scripts/check-markdown-links.py                  # scan the default set
    scripts/check-markdown-links.py [path ...]       # scan specific files

Exit codes:
    0 — All links resolve.
    1 — One or more broken links found.

Ref: https://github.com/judgemind/judgemind/issues/2425
"""
# permanent: true

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ─── Configuration ──────────────────────────────────────────────────────

# Prefixes that indicate a backtick token is a repo-relative path.
REPO_PATH_PREFIXES = (
    "docs/",
    "packages/",
    "scripts/",
    "infra/",
    ".claude/",
    ".github/",
    ".githooks/",
    "tests/",
    "memory/",
)

# Top-level markdown files that are recognizable as repo paths without a
# directory prefix.
TOP_LEVEL_MD_FILES = frozenset(
    {
        "CLAUDE.md",
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "LICENSE.md",
        "CHANGELOG.md",
    }
)

# Glob patterns for the default scan set.
DEFAULT_SCAN_GLOBS = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "README.md",
    "docs/**/*.md",
    ".claude/skills/**/*.md",
    "packages/*/README.md",
    "packages/*/docs/**/*.md",
    "infra/**/*.md",
    ".github/**/*.md",
)

# ─── Regexes ────────────────────────────────────────────────────────────

# Markdown link:  [text](target)  — captures the target in group 1.
# We match only targets that end with .md, optionally followed by #anchor.
MARKDOWN_LINK_RE = re.compile(r"\]\(([^)\s]+\.md(?:#[^)]*)?)\)")

# Backtick-quoted token:  `content`  — captures content in group 1.
# We include the whole content; path-filtering is done later.
BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# External URL protocols we skip.
EXTERNAL_PROTOCOLS = ("http://", "https://", "mailto:", "ftp://", "ftps://")


def is_repo_path(token: str) -> bool:
    """Return True if the backtick token looks like a repo-relative path.

    We require a recognizable prefix (e.g. docs/, .claude/) or a known
    top-level markdown filename to avoid flagging bare filenames that
    appear in prose or code examples.
    """
    if token.startswith(REPO_PATH_PREFIXES):
        return True
    # Strip optional anchor and query-ish suffix before checking top-level.
    bare = token.split("#", 1)[0].split("?", 1)[0]
    if bare in TOP_LEVEL_MD_FILES:
        return True
    return False


def is_external(target: str) -> bool:
    return target.startswith(EXTERNAL_PROTOCOLS)


def strip_anchor(target: str) -> str:
    """Drop any #anchor and trailing whitespace from a link target."""
    return target.split("#", 1)[0].strip()


def resolve_markdown_link(base_dir: Path, target: str, repo_root: Path) -> Path:
    """Resolve a Markdown-link target relative to the file's directory.

    Absolute-style paths that start with '/' are treated as repo-rooted.
    Everything else is resolved relative to base_dir (standard Markdown
    link semantics).
    """
    if target.startswith("/"):
        return (repo_root / target.lstrip("/")).resolve()
    return (base_dir / target).resolve()


def resolve_backtick_ref(target: str, repo_root: Path) -> Path:
    """Resolve a backtick-quoted path token.

    In our docs, backtick tokens like `docs/foo.md` or `.claude/x/SKILL.md`
    are written as repo-rooted paths, not relative to the enclosing file.
    A backtick token only reaches this function if it already passed the
    is_repo_path() filter, so it's safe to resolve from repo_root.
    """
    return (repo_root / target.lstrip("/")).resolve()


def find_markdown_link_targets(line: str) -> list[str]:
    """Extract all markdown-link targets pointing to .md files."""
    return MARKDOWN_LINK_RE.findall(line)


def find_backtick_repo_paths(line: str) -> list[str]:
    """Extract backtick tokens that look like repo-relative .md paths."""
    results: list[str] = []
    for token in BACKTICK_RE.findall(line):
        # Drop any surrounding whitespace from the token.
        token = token.strip()
        if not token:
            continue
        # A backtick token can contain anything (commands, filenames,
        # prose).  Reject anything that looks like a command rather than
        # a lone path: any internal whitespace, shell operators, etc.
        if any(c in token for c in (" ", "\t", "|", ";", "&", ">", "<")):
            continue
        # We only care about .md references for this checker (per #2425).
        # The path part (before any anchor/query) must end in .md.
        bare = token.split("#", 1)[0].split("?", 1)[0]
        if not bare.endswith(".md"):
            continue
        if not is_repo_path(token):
            continue
        results.append(token)
    return results


def gather_default_files(repo_root: Path) -> list[Path]:
    """Collect the default set of files to scan."""
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in DEFAULT_SCAN_GLOBS:
        for path in repo_root.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return sorted(files)


def check_file(path: Path, repo_root: Path) -> list[tuple[int, str, Path]]:
    """Return a list of (lineno, target, resolved_path) for broken links."""
    broken: list[tuple[int, str, Path]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"WARNING: could not read {path}: {exc}", file=sys.stderr)
        return broken

    base_dir = path.parent

    for lineno, line in enumerate(lines, start=1):
        # Markdown links resolve relative to the file's directory.
        for target in find_markdown_link_targets(line):
            if is_external(target):
                continue
            rel = strip_anchor(target)
            if not rel:
                continue
            resolved = resolve_markdown_link(base_dir, rel, repo_root)
            if not resolved.exists():
                broken.append((lineno, target, resolved))

        # Backtick references are repo-rooted (e.g. `docs/foo.md`).
        for target in find_backtick_repo_paths(line):
            rel = strip_anchor(target)
            if not rel:
                continue
            resolved = resolve_backtick_ref(rel, repo_root)
            if not resolved.exists():
                broken.append((lineno, target, resolved))

    return broken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Specific files to scan. Defaults to CLAUDE.md, docs/**/*.md, "
        ".claude/skills/**/*.md.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Path to repository root (defaults to two levels above this "
        "script).",
    )
    args = parser.parse_args()

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        # scripts/ is one level under the repo root.
        repo_root = Path(__file__).resolve().parent.parent

    if args.paths:
        files = [Path(p) for p in args.paths]
    else:
        files = gather_default_files(repo_root)

    total_broken = 0
    header_printed = False

    for path in files:
        broken = check_file(path, repo_root)
        if not broken:
            continue
        if not header_printed:
            print("ERROR: Found broken internal markdown links.")
            print("")
            print("  Each line below lists the file, line number, and the")
            print("  broken target.  Update the pointer or create the file.")
            print("")
            header_printed = True
        try:
            rel_display = path.resolve().relative_to(repo_root)
        except ValueError:
            rel_display = path
        for lineno, target, resolved in broken:
            try:
                resolved_display = resolved.relative_to(repo_root)
            except ValueError:
                resolved_display = resolved
            print(f"  {rel_display}:{lineno}: broken link -> {target}")
            print(f"    (resolved to: {resolved_display})")
            total_broken += 1

    if total_broken > 0:
        print("")
        print(f"  Found {total_broken} broken link(s).")
        print("")
        print("  Fix: update the pointer to an existing file, create the")
        print("  missing file, or remove the pointer if it is obsolete.")
        return 1

    print(f"All clean — checked {len(files)} file(s), no broken links found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
