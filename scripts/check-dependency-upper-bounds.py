#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-dependency-upper-bounds.py — Fail when a Python package's runtime
dependency has no upper version bound.

Motivation (#4756): ``redis>=5.0`` with no upper bound let the scraper image
silently pick up redis-py 8.x on a routine rebuild. redis-py 8 changed its
default ``socket_timeout`` from ``None`` to 5 seconds, which broke the
ingestion worker's ``XREADGROUP BLOCK 5000`` loop (#4705 / PR #4751). No code
in the repo changed; the image build resolved a new major version.

The policy (docs/agent/code-standards.md §Dependency version bounds): every
entry in ``[project].dependencies`` of every ``packages/*/pyproject.toml``
carries an upper bound at the next major version above the one the image
resolves today (``redis>=5.0,<9``). For ``0.x`` packages the minor is the
breaking axis, so the bound is the next minor (``httpx>=0.27,<0.29``). A
major bump is then a deliberate PR with tests.

What counts as an upper bound: any specifier clause using ``<``, ``<=``,
``==``, ``===`` or ``~=``, or a direct URL reference (``name @ url``).

Local monorepo siblings (``judgemind-config``) are exempt automatically:
they are installed from the same commit, never from PyPI.

Exemptions: a package can exempt a dependency by listing it, with a reason,
in its own pyproject::

    [tool.judgemind.dependency-bounds.unbounded-ok]
    certifi = "CA bundle; must track upstream root-store updates"

Only ``[project].dependencies`` is checked. ``[project.optional-dependencies]``
(dev tooling) is not shipped in images and is out of scope.

Usage:
    scripts/check-dependency-upper-bounds.py              # all packages/*/pyproject.toml
    scripts/check-dependency-upper-bounds.py PATH [...]   # specific pyproject files

Exit codes: 0 = all bounded, 1 = violations (with a Fix: block), 2 = usage /
parse error.

Stdlib only (tomllib, Python 3.11+) so it runs under a bare ``python3`` in
CI and the pre-push guard umbrella.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_UPPER_OPS = ("<", "<=", "==", "===", "~=")
_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class Violation:
    pyproject: Path
    requirement: str
    name: str
    resolved: str | None
    suggestion: str | None


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def has_upper_bound(spec: str) -> bool:
    """Return True if a requirement specifier string carries an upper bound."""
    spec = spec.split(";", 1)[0].strip()
    if spec.startswith("@"):
        return True
    return any(clause.strip().startswith(_UPPER_OPS) for clause in spec.split(","))


def local_sibling_names(pyproject: Path) -> set[str]:
    """Names of the monorepo's own packages (``packages/*/pyproject.toml``).

    Local siblings such as ``judgemind-config`` are installed from the same
    commit, never from PyPI, so a version bound on them is meaningless.
    """
    names: set[str] = set()
    for sibling in pyproject.resolve().parent.parent.glob("*/pyproject.toml"):
        try:
            name = tomllib.loads(sibling.read_text()).get("project", {}).get("name")
        except tomllib.TOMLDecodeError:
            continue
        if name:
            names.add(normalize(name))
    return names


def next_breaking(version: str) -> str | None:
    """Next breaking version for ``version``: next major, or next minor for 0.x."""
    m = _VERSION_RE.match(version)
    if not m:
        return None
    major = int(m.group(1))
    if major == 0:
        minor = int(m.group(2) or 0)
        return f"0.{minor + 1}"
    return str(major + 1)


def resolved_version(pyproject: Path, name: str) -> str | None:
    """Look up ``name``'s installed version in the package's local ``.venv``."""
    target = normalize(name).replace("-", "_")
    for site in sorted(
        (pyproject.parent / ".venv" / "lib").glob("python*/site-packages")
    ):
        for dist in site.glob("*.dist-info"):
            stem = dist.name[: -len(".dist-info")]
            if "-" not in stem:
                continue
            dist_name, version = stem.rsplit("-", 1)
            if normalize(dist_name).replace("-", "_") == target:
                return version
    return None


def check_pyproject(pyproject: Path) -> list[Violation]:
    data = tomllib.loads(pyproject.read_text())
    deps = data.get("project", {}).get("dependencies", [])
    exempt_table = (
        data.get("tool", {})
        .get("judgemind", {})
        .get("dependency-bounds", {})
        .get("unbounded-ok", {})
    )
    exempt = {normalize(k) for k, v in exempt_table.items() if str(v).strip()}
    exempt |= local_sibling_names(pyproject)

    violations: list[Violation] = []
    for req in deps:
        m = _REQ_RE.match(req)
        if not m:
            continue
        name, extras, spec = m.group(1), m.group(2) or "", m.group(3)
        if normalize(name) in exempt or has_upper_bound(spec):
            continue
        resolved = resolved_version(pyproject, name)
        suggestion = None
        if resolved is not None:
            bound = next_breaking(resolved)
            if bound is not None:
                spec_clean = spec.split(";", 1)[0].strip()
                joined = f"{spec_clean},<{bound}" if spec_clean else f"<{bound}"
                suggestion = f'"{name}{extras}{joined}"'
        violations.append(Violation(pyproject, req, name, resolved, suggestion))
    return violations


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def report(violations: list[Violation]) -> None:
    err = sys.stderr
    print(
        "ERROR: runtime dependencies without an upper version bound "
        "(see docs/agent/code-standards.md §Dependency version bounds, #4756):",
        file=err,
    )
    for v in violations:
        print(f"  {_rel(v.pyproject)}: {v.requirement!r}", file=err)
    print("", file=err)
    print("Fix:", file=err)
    print(
        "  Bound each dependency below the next major version the package resolves "
        "today (next minor for 0.x). Replace in [project].dependencies:",
        file=err,
    )
    for v in violations:
        rel = _rel(v.pyproject)
        if v.suggestion:
            print(
                f'    {rel}:  "{v.requirement}"  ->  {v.suggestion}'
                f"   # resolved {v.name}=={v.resolved}",
                file=err,
            )
        else:
            pkg_dir = _rel(v.pyproject.parent)
            print(
                f'    {rel}:  "{v.requirement}"  ->  "{v.requirement},<NEXT_MAJOR>"',
                file=err,
            )
            print(
                f"      find the resolved version: scripts/install-package-venv.sh "
                f"{Path(pkg_dir).name} then {pkg_dir}/.venv/bin/pip show {v.name}",
                file=err,
            )
    print(
        "  If the dependency must track upstream (e.g. a CA bundle), exempt it with a "
        "reason instead:",
        file=err,
    )
    print("    [tool.judgemind.dependency-bounds.unbounded-ok]", file=err)
    for v in violations[:1]:
        print(f'    {v.name} = "<why this dependency must stay unbounded>"', file=err)


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    if argv:
        paths = [Path(a) for a in argv]
    else:
        paths = sorted(REPO_ROOT.glob("packages/*/pyproject.toml"))
    violations: list[Violation] = []
    for p in paths:
        if not p.is_file():
            print(f"error: {p} is not a file", file=sys.stderr)
            return 2
        try:
            violations.extend(check_pyproject(p))
        except tomllib.TOMLDecodeError as exc:
            print(f"error: cannot parse {p}: {exc}", file=sys.stderr)
            return 2
    if violations:
        report(violations)
        return 1
    print(
        f"ok: every runtime dependency in {len(paths)} pyproject.toml file(s) is upper-bounded"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
