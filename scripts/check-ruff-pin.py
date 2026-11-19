#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-ruff-pin.py — Fail when a ruff install does not match the repo's pin.

Motivation (#5153): CI installed ruff unpinned (``pip install ruff``), the
packages' [dev] extras said ``ruff>=0.3``, and the pre-push hook used
whatever ruff each venv or PATH happened to have. A ruff release that
changed the format style turned every open PR red at once, and local
pre-push results disagreed with CI.

The pin lives in one place, ``requirements-ruff.txt`` at the repo root, as a
single ``ruff==X.Y.Z`` line. This guard checks every other place that
installs ruff:

  * ``pyproject.toml`` files: every ruff requirement in
    ``[project].dependencies``, ``[project.optional-dependencies]`` and
    ``[dependency-groups]`` must be exactly ``ruff==<pin>``.
  * ``requirements*.txt`` / ``*constraints*.txt`` files: a ruff line must be
    exactly ``ruff==<pin>`` (``-r`` includes of the pin file are fine).
  * Command files (``.github/workflows/*``, ``.githooks/*``,
    ``.claude/hooks/*.sh``, ``scripts/**/*.sh``, ``Dockerfile*``): a
    ``pip install`` line must not name ruff except as ``ruff==<pin>``.
    The preferred form is ``pip install -r requirements-ruff.txt``.

Test directories (``tests/``) and ``scripts/archive/`` are not scanned:
their ruff strings are fixtures and stubs, not installs.

Usage:
    scripts/check-ruff-pin.py               # scan the repo this script lives in
    scripts/check-ruff-pin.py --root DIR    # scan another tree (tests)

Exit codes: 0 = every ruff install matches the pin, 1 = mismatches (with a
Fix: block), 2 = the pin file is missing or malformed.

Stdlib only (tomllib, Python 3.11+) so it runs under a bare ``python3`` in
the repo-guards CI job and the pre-push guard umbrella.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

PIN_FILE_NAME = "requirements-ruff.txt"
_PIN_RE = re.compile(r"^ruff==(\d+\.\d+\.\d+)$")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
# A pip install command: `pip install`, `pip3 install`, `python -m pip install`,
# `uv pip install`, `"$PIP" install`.
_PIP_INSTALL_RE = re.compile(r"pip3?\b[\"']?\s+install\b", re.IGNORECASE)
# A ruff argument on such a line: `ruff`, `ruff>=0.3`, `"ruff==0.1.0"`, `ruff[x]`.
_RUFF_ARG_RE = re.compile(r"^ruff(\[[^\]]*\])?([<>=!~].*)?$", re.IGNORECASE)
_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "tmp",
    "vendor",
    "worktrees",
    "__pycache__",
}


class Violation(NamedTuple):
    path: str
    line_no: int
    line: str
    kind: str  # "requirement" or "install"


def read_pin(root: Path) -> str:
    """Return the pinned version from requirements-ruff.txt, or raise ValueError."""
    pin_file = root / PIN_FILE_NAME
    if not pin_file.is_file():
        raise ValueError(f"{PIN_FILE_NAME} not found at {pin_file}")
    specs = []
    for raw in pin_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            specs.append(line)
    if len(specs) != 1 or not _PIN_RE.match(specs[0]):
        raise ValueError(
            f"{PIN_FILE_NAME} must hold exactly one non-comment line of the form "
            f"ruff==X.Y.Z, found: {specs or 'nothing'}"
        )
    match = _PIN_RE.match(specs[0])
    assert match is not None
    return match.group(1)


def list_files(root: Path) -> list[str]:
    """Return repo-relative paths: tracked files when root is a git checkout, else a walk."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
            out = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=True,
            )
            return [p for p in out.stdout.split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        pass
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            paths.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(paths)


def is_excluded(rel: str) -> bool:
    parts = Path(rel).parts
    return "tests" in parts or rel.startswith("scripts/archive/")


def classify(rel: str) -> str | None:
    """Return "pyproject", "requirements", "commands" or None (not scanned)."""
    if rel == PIN_FILE_NAME:
        return None
    name = Path(rel).name
    # Requirements files are scanned even under tests/: scripts/tests/
    # requirements-ci.txt is what CI's scripts-tests job installs.
    if name.endswith(".txt") and (
        name.startswith("requirements") or "constraints" in name
    ):
        return "requirements"
    if is_excluded(rel):
        return None
    if name == "pyproject.toml":
        return "pyproject"
    if rel.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
        return "commands"
    if rel.startswith(".githooks/") and Path(rel).suffix in ("", ".sh"):
        return "commands"
    if rel.startswith(".claude/hooks/") and name.endswith(".sh"):
        return "commands"
    if rel.startswith("scripts/") and name.endswith(".sh"):
        return "commands"
    if name.startswith("Dockerfile"):
        return "commands"
    return None


def _requirement_is_ruff(req: str) -> tuple[bool, str]:
    """Return (names ruff, normalized spec text) for a PEP 508 requirement string."""
    match = _REQ_NAME_RE.match(req.split(";", 1)[0])
    if not match:
        return False, ""
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    return name == "ruff", re.sub(r"\s+", "", match.group(3))


def _find_line(lines: list[str], needle: str) -> tuple[int, str]:
    for i, line in enumerate(lines, start=1):
        if needle in line:
            return i, line.strip()
    return 0, needle


def scan_pyproject(rel: str, text: str, pin: str) -> list[Violation]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    project = data.get("project", {})
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs += list(group)
    for group in data.get("dependency-groups", {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    lines = text.splitlines()
    out = []
    for req in reqs:
        is_ruff, spec = _requirement_is_ruff(req)
        if is_ruff and spec != f"=={pin}":
            line_no, line = _find_line(lines, req)
            out.append(Violation(rel, line_no, line, "requirement"))
    return out


def scan_requirements(rel: str, text: str, pin: str) -> list[Violation]:
    out = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        is_ruff, spec = _requirement_is_ruff(line)
        if is_ruff and spec != f"=={pin}":
            out.append(Violation(rel, i, raw.strip(), "requirement"))
    return out


def scan_commands(rel: str, text: str, pin: str) -> list[Violation]:
    out = []
    for i, raw in enumerate(text.splitlines(), start=1):
        match = _PIP_INSTALL_RE.search(raw)
        if not match:
            continue
        for token in raw[match.end() :].split():
            arg = token.strip("\"'`;,()")
            if _RUFF_ARG_RE.match(arg) and arg.lower() != f"ruff=={pin}":
                out.append(Violation(rel, i, raw.strip(), "install"))
                break
    return out


SCANNERS = {
    "pyproject": scan_pyproject,
    "requirements": scan_requirements,
    "commands": scan_commands,
}


def scan(root: Path, pin: str) -> list[Violation]:
    violations: list[Violation] = []
    for rel in list_files(root):
        kind = classify(rel)
        if kind is None:
            continue
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        violations += SCANNERS[kind](rel, text, pin)
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="tree to scan (default: this repo)",
    )
    args = parser.parse_args(argv)
    root: Path = args.root

    try:
        pin = read_pin(root)
    except ValueError as exc:
        print(f"FAILED: {exc} (#5153)", file=sys.stderr)
        print("Fix:", file=sys.stderr)
        print(f"  git checkout origin/main -- {PIN_FILE_NAME}", file=sys.stderr)
        return 2

    violations = scan(root, pin)
    if not violations:
        print(
            f"check-ruff-pin: every ruff install matches {PIN_FILE_NAME} (ruff=={pin})"
        )
        return 0

    print(
        f"FAILED: {len(violations)} ruff install(s) do not match the pin in "
        f"{PIN_FILE_NAME} (ruff=={pin}) (#5153):",
        file=sys.stderr,
    )
    for v in violations:
        print(f"  {v.path}:{v.line_no}: {v.line}", file=sys.stderr)
    print(file=sys.stderr)
    print("Fix:", file=sys.stderr)
    if any(v.kind == "install" for v in violations):
        print(
            "  # pip install lines: install ruff from the pin file instead of by name",
            file=sys.stderr,
        )
        print(f"  pip install -r {PIN_FILE_NAME}", file=sys.stderr)
    if any(v.kind == "requirement" for v in violations):
        print(
            "  # pyproject.toml / requirements files: write the pinned spec",
            file=sys.stderr,
        )
        print(f'  "ruff=={pin}"', file=sys.stderr)
    print(
        f"  # To move the pin instead, follow the upgrade steps in {PIN_FILE_NAME}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
