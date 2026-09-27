#!/usr/bin/env python3
"""List the requirements a package declares that its venv does not have.

Used by .githooks/pre-push before it runs a package's tests (#4822).

Usage:
    missing_venv_deps.py <pyproject.toml> <site-packages-dir> [<dir> ...]

Reads [project] dependencies and [project.optional-dependencies] dev from
the pyproject.toml. For each one, looks for an installed distribution of
that name in the given site-packages directories. Only presence is
checked, not the version.

Prints each missing requirement, one per line, as written in the
pyproject.toml.

Exit codes:
    0 - every requirement is installed
    1 - at least one requirement is missing (printed on stdout)
    2 - usage error, or the pyproject.toml could not be read (this
        includes Python < 3.11, which has no tomllib). The hook warns
        and carries on.

Why: a venv built before a pyproject.toml change lacks the new
dependency. The hook used to adapt to what it found, e.g. it passed
--timeout only when pytest-timeout happened to be installed, so a stale
venv silently ran the suite with no per-test timeout (#4822).
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def canonical(name: str) -> str:
    """Normalize a distribution name the way PEP 503 does."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_toml(path: Path) -> dict:
    """Parse the pyproject.toml. tomllib needs Python 3.11+ (ImportError before)."""
    import tomllib

    with path.open("rb") as fh:
        return tomllib.load(fh)


def declared_requirements(pyproject: dict) -> list[str]:
    """Return runtime dependencies followed by the [dev] extras."""
    project = pyproject.get("project", {})
    reqs = list(project.get("dependencies", []))
    reqs += list(project.get("optional-dependencies", {}).get("dev", []))
    return reqs


def _marker_applies(req: str) -> bool:
    """True unless the requirement has an environment marker that is false here.

    Without the `packaging` library the marker cannot be evaluated, so the
    requirement is skipped rather than reported as missing: a false alarm
    would block every push.
    """
    if ";" not in req:
        return True
    try:
        from packaging.requirements import Requirement
    except ImportError:
        return False
    marker = Requirement(req).marker
    return marker is None or marker.evaluate()


def installed_names(site_dirs: list[str]) -> set[str]:
    """Canonical names of every distribution installed in the given dirs."""
    names = set()
    for dist in metadata.distributions(path=site_dirs):
        name = dist.metadata["Name"]
        if name:
            names.add(canonical(name))
    return names


def missing_requirements(reqs: list[str], installed: set[str]) -> list[str]:
    """Requirements whose distribution is not in `installed`."""
    missing = []
    for req in reqs:
        match = _NAME_RE.match(req)
        if not match or not _marker_applies(req):
            continue
        if canonical(match.group(1)) not in installed:
            missing.append(req.strip())
    return missing


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: missing_venv_deps.py <pyproject.toml> <site-packages-dir> [<dir> ...]",
            file=sys.stderr,
        )
        return 2
    pyproject_path = Path(argv[1])
    site_dirs = [d for d in argv[2:] if Path(d).is_dir()]
    if not site_dirs:
        print(f"no site-packages directory found among: {' '.join(argv[2:])}", file=sys.stderr)
        return 2
    try:
        pyproject = _load_toml(pyproject_path)
    except (ImportError, OSError, ValueError) as exc:
        print(f"cannot read {pyproject_path}: {exc}", file=sys.stderr)
        return 2
    missing = missing_requirements(declared_requirements(pyproject), installed_names(site_dirs))
    for req in missing:
        print(req)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
