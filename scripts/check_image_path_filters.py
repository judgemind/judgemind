#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check_image_path_filters.py — every file a Docker image COPYs must trigger
the workflow that rebuilds that image.

Called by ``scripts/check-image-path-filters.sh`` (the CI / pre-push entry
point).  Standard library only, so it runs on a bare ubuntu-latest runner and
in the pre-push hook's minimal environment.

Why (#5060)
───────────
``packages/scraper-framework/Dockerfile`` bakes ``scripts/reingest_from_s3.py``
and ``scripts/rebuild_db.py`` into the scraper image, but
``deploy-scraper.yml``'s ``on.push.paths`` filter did not list them.  A PR
that changed only one of those scripts merged without rebuilding the image,
so every caller of the baked copy (the daily raw-key reconciler among them)
kept running the old code.  The filter was a hand-maintained copy of the
Dockerfile's COPY list, and the two drifted.  This check derives the
requirement from the Dockerfile itself, so they cannot drift again.

What it checks
──────────────
For each ``.github/workflows/*.yml`` that has an ``on.push.paths`` filter AND
runs ``docker build ... -f <Dockerfile> .``:

1. Parse every ``COPY`` / ``ADD`` instruction in the Dockerfile (line
   continuations, ``--chown=``-style flags and the JSON-array form are
   handled; ``COPY --from=<stage>`` copies from another build stage, not the
   repo, and is skipped; ``ADD <url>`` is skipped).
2. Expand each source (relative to the build context, which must be the
   repo root ``.``) to the files it brings into the image.
3. Assert each of those files, and the Dockerfile itself, is matched by at
   least one pattern in ``on.push.paths``, using GitHub's glob rules
   (``*`` stays inside one path segment, ``**`` crosses segments).

A file matched only by a negated ``!pattern`` counts as covered: the
negation is an explicit, reviewed decision not to rebuild for it (e.g.
``!scripts/dispatcher/tests/**`` in deploy-dispatcher.yml).  The check
catches files nobody decided about.

Workflows without an ``on.push.paths`` filter run on every push, so they
cannot miss a file and are skipped.

Usage:
    python3 scripts/check_image_path_filters.py [--repo-root PATH]

Exit codes:
    0 — every COPY'd file triggers its image's workflow.
    1 — one or more COPY'd files are missing from a path filter.
    2 — usage error, or a workflow shape this check does not understand
        (e.g. a build context other than the repo root).
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Directories never part of a build's real input (local venvs, caches).
#: Walking them would only add noise; they are not tracked in git.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)

_DOCKER_BUILD_RE = re.compile(r"\bdocker\s+build\b")


class ShapeError(Exception):
    """A workflow or Dockerfile shape this check cannot reason about."""


@dataclass
class CopySource:
    """One source argument of a COPY/ADD instruction."""

    source: str  # as written, relative to the build context
    line: int  # Dockerfile line the instruction starts on


@dataclass
class ImageWorkflow:
    workflow: str  # repo-relative path
    dockerfile: str  # repo-relative path
    paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Workflow parsing
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Drop a trailing YAML ``# comment`` that is outside quotes."""
    out = []
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_push_paths(text: str) -> list[str] | None:
    """The ``on.push.paths`` list of a workflow, or ``None`` when the workflow
    has no push path filter.

    A small line-based reader rather than a YAML parser, so the check needs
    no third-party package.  It handles the block-list form every workflow in
    this repo uses (``paths:`` followed by ``- "pattern"`` lines) and the
    inline ``paths: [a, b]`` form.
    """
    lines = text.splitlines()
    on_indent = push_indent = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = _strip_comment(raw)
        if not line.strip():
            i += 1
            continue
        ind = _indent(line)
        key = line.strip()
        if on_indent is None:
            if ind == 0 and key in ("on:", '"on":', "'on':"):
                on_indent = ind
        elif ind <= on_indent:
            return None  # left the on: block without finding push.paths
        elif push_indent is None:
            if key == "push:":
                push_indent = ind
        elif ind <= push_indent:
            push_indent = None  # left push:, keep scanning on:
            continue
        elif key.startswith("paths:"):
            rest = key[len("paths:") :].strip()
            if rest.startswith("["):
                inner = rest.strip("[]")
                return [_unquote(p.strip()) for p in inner.split(",") if p.strip()]
            paths_indent = ind
            out: list[str] = []
            i += 1
            while i < len(lines):
                item = _strip_comment(lines[i])
                if not item.strip():
                    i += 1
                    continue
                stripped = item.strip()
                if _indent(item) < paths_indent or not stripped.startswith("-"):
                    break
                out.append(_unquote(stripped[1:].strip()))
                i += 1
            return out
        i += 1
    return None


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def find_docker_builds(text: str) -> list[tuple[str, str]]:
    """``(dockerfile, context)`` for every ``docker build`` in a workflow.

    Joins backslash-continued shell lines, then reads the ``-f``/``--file``
    argument and the final positional argument (the build context).
    """
    lines = text.splitlines()
    builds: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _DOCKER_BUILD_RE.search(line) or line.lstrip().startswith("#"):
            i += 1
            continue
        cmd = line
        while cmd.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            cmd = cmd.rstrip()[:-1] + " " + lines[i]
        i += 1
        cmd = cmd[_DOCKER_BUILD_RE.search(cmd).end() :]  # type: ignore[union-attr]
        try:
            tokens = shlex.split(cmd, comments=True)
        except ValueError as exc:
            raise ShapeError(f"cannot tokenize docker build command: {exc}") from exc
        dockerfile = None
        for j, tok in enumerate(tokens):
            if tok in ("-f", "--file") and j + 1 < len(tokens):
                dockerfile = tokens[j + 1]
            elif tok.startswith("--file="):
                dockerfile = tok.split("=", 1)[1]
        if dockerfile is None:
            dockerfile = "Dockerfile"
        context = tokens[-1] if tokens else ""
        builds.append((dockerfile, context))
    return builds


# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------


def parse_copy_sources(text: str) -> list[CopySource]:
    """Every repo-relative source of a COPY/ADD instruction."""
    sources: list[CopySource] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start = i
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        instr = stripped
        while instr.endswith("\\") and i < len(lines):
            nxt = lines[i].strip()
            i += 1
            if nxt.startswith("#"):
                continue
            instr = instr[:-1].rstrip() + " " + nxt
        word, _, rest = instr.partition(" ")
        if word.upper() not in ("COPY", "ADD"):
            continue
        rest = rest.strip()
        args: list[str]
        flags: list[str] = []
        while rest.startswith("--"):
            flag, _, rest = rest.partition(" ")
            flags.append(flag)
            rest = rest.strip()
        if any(f.startswith("--from") for f in flags):
            continue  # copies from a build stage, not the repo
        if rest.startswith("["):
            try:
                args = json.loads(rest)
            except json.JSONDecodeError as exc:
                raise ShapeError(
                    f"line {start + 1}: cannot parse JSON-form {word}: {exc}"
                ) from exc
        else:
            args = shlex.split(rest)
        if len(args) < 2:
            raise ShapeError(f"line {start + 1}: {word} needs a source and a dest")
        for src in args[:-1]:
            if re.match(r"^[a-z]+://", src):
                continue  # ADD <url>
            sources.append(CopySource(src, start + 1))
    return sources


def expand_source(repo_root: Path, source: str) -> list[str]:
    """Repo-relative files a COPY source brings into the image."""
    rel = source[2:] if source.startswith("./") else source
    rel = rel.rstrip("/") or "."
    matches = globmod.glob(str(repo_root / rel)) if globmod.has_magic(rel) else []
    roots = [Path(m) for m in matches] if matches else [repo_root / rel]
    files: list[str] = []
    for root in roots:
        if root.is_file():
            files.append(root.relative_to(repo_root).as_posix())
        elif root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d
                    for d in dirnames
                    if d not in SKIP_DIRS and not d.endswith(".egg-info")
                )
                for name in sorted(filenames):
                    full = Path(dirpath) / name
                    files.append(full.relative_to(repo_root).as_posix())
    return files


# ---------------------------------------------------------------------------
# GitHub path-filter matching
# ---------------------------------------------------------------------------


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """GitHub Actions path-filter glob as a regex (filter pattern cheat
    sheet): ``**`` matches any characters including ``/``, ``*`` matches
    any characters except ``/``, ``?`` matches one character except ``/``.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            else:
                out.append(".*")
                i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def is_covered(path: str, patterns: list[str]) -> bool:
    """Whether some filter pattern, positive or negated, names *path*."""
    for pat in patterns:
        body = pat[1:] if pat.startswith("!") else pat
        if glob_to_regex(body).match(path):
            return True
    return False


def suggest_pattern(source: str, repo_root: Path) -> str:
    """The ``on.push.paths`` entry that covers a COPY source."""
    rel = source[2:] if source.startswith("./") else source
    rel = rel.rstrip("/")
    if globmod.has_magic(rel) or (repo_root / rel).is_file():
        return rel
    return f"{rel}/**"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class Gap:
    workflow: str
    dockerfile: str
    source: str  # COPY source (or the Dockerfile itself)
    line: int  # Dockerfile line, 0 for the Dockerfile itself
    files: list[str]
    suggestion: str


def discover(repo_root: Path) -> list[ImageWorkflow]:
    found: list[ImageWorkflow] = []
    wf_dir = repo_root / ".github" / "workflows"
    for wf in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        text = wf.read_text(encoding="utf-8")
        paths = parse_push_paths(text)
        if paths is None:
            continue
        rel_wf = wf.relative_to(repo_root).as_posix()
        for dockerfile, context in find_docker_builds(text):
            if context not in (".", "./"):
                raise ShapeError(
                    f"{rel_wf}: docker build -f {dockerfile} uses build context "
                    f"{context!r}; this check resolves COPY sources against the "
                    "repo root and only understands context '.'"
                )
            found.append(ImageWorkflow(rel_wf, dockerfile, paths))
    return found


def find_gaps(repo_root: Path) -> list[Gap]:
    gaps: list[Gap] = []
    for iw in discover(repo_root):
        df_path = repo_root / iw.dockerfile
        if not df_path.is_file():
            raise ShapeError(f"{iw.workflow}: Dockerfile {iw.dockerfile} not found")
        if not is_covered(iw.dockerfile, iw.paths):
            gaps.append(
                Gap(
                    iw.workflow,
                    iw.dockerfile,
                    iw.dockerfile,
                    0,
                    [iw.dockerfile],
                    iw.dockerfile,
                )
            )
        for cs in parse_copy_sources(df_path.read_text(encoding="utf-8")):
            files = expand_source(repo_root, cs.source)
            if not files:
                raise ShapeError(
                    f"{iw.dockerfile}:{cs.line}: COPY source {cs.source!r} "
                    "matches no file in the repo"
                )
            missing = [f for f in files if not is_covered(f, iw.paths)]
            if missing:
                gaps.append(
                    Gap(
                        iw.workflow,
                        iw.dockerfile,
                        cs.source,
                        cs.line,
                        missing,
                        suggest_pattern(cs.source, repo_root),
                    )
                )
    return gaps


def report(gaps: list[Gap]) -> None:
    err = sys.stderr
    print(
        f"FAIL: {len(gaps)} file group(s) baked into an image do not trigger "
        "the workflow that rebuilds it:",
        file=err,
    )
    for g in gaps:
        where = f"{g.dockerfile}:{g.line}" if g.line else g.dockerfile
        sample = ", ".join(g.files[:3]) + (
            f" (+{len(g.files) - 3} more)" if len(g.files) > 3 else ""
        )
        print(
            f"  - {g.workflow}: COPY source '{g.source}' ({where}) -> {sample}",
            file=err,
        )
    print("", file=err)
    print(
        "A change to these files merges without rebuilding the image, so "
        "everything that runs the baked copy keeps the old code (#5060).",
        file=err,
    )
    print("", file=err)
    by_wf: dict[str, list[str]] = {}
    for g in gaps:
        entries = by_wf.setdefault(g.workflow, [])
        if g.suggestion not in entries:
            entries.append(g.suggestion)
    print("Fix: add these entries under on.push.paths in each workflow:", file=err)
    for wf, entries in by_wf.items():
        print(f"  # {wf}", file=err)
        for e in entries:
            print(f'      - "{e}"', file=err)
    print("", file=err)
    print(
        "If a file is in the image on purpose but must NOT trigger a rebuild, "
        "add an explicit negation instead (e.g. '!scripts/dispatcher/tests/**').",
        file=err,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repo root to scan (default: this script's repo).",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        workflows = discover(repo_root)
        gaps = find_gaps(repo_root)
    except ShapeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if gaps:
        report(gaps)
        return 1
    names = ", ".join(sorted({w.workflow.rsplit("/", 1)[-1] for w in workflows}))
    print(
        f"OK: every file COPY'd into {len(workflows)} path-filtered image "
        f"build(s) triggers its workflow ({names})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
