#!/usr/bin/env python3
"""check-scraper-registry.py — Verify all scraper modules are registered in runner.py.

Every Python module under courts/ that exports a ``default_config`` function
must be imported inside ``_build_registry()`` in ``framework/runner.py``.
Unregistered scrapers silently produce zero data because the runner never
instantiates them.

Usage:
    scripts/check-scraper-registry.py          # exits 0 if clean, 1 if violations
    scripts/check-scraper-registry.py --verbose # list all modules and their status

Exit codes:
    0 — All scraper modules are registered.
    1 — One or more scraper modules are not registered in runner.py.

See https://github.com/judgemind/judgemind/issues/2132 for context.
"""
# permanent: true

from __future__ import annotations

import re
import sys
from pathlib import Path

# Scraper modules intentionally kept in the tree but NOT wired into
# ``_build_registry()``. Each entry must carry a rationale so the guard
# stays honest — leaving a module unregistered silently produces zero data,
# which is exactly what this check exists to catch. A module belongs here
# only when it has been deliberately retired (not merely forgotten).
RETIRED_MODULES: set[str] = {
    # CourtListener federal-opinions scraper retired per #4571 / #4474 — its
    # envelopes drove dev ElastiCache OOM and wedged the dispatcher. The
    # module + its test stay in tree so the scraper can be re-enabled later,
    # but it is intentionally de-registered from the runner for now.
    "courts.federal.courtlistener",
}


def find_repo_root() -> Path:
    """Walk up from the script's location to find the repository root."""
    here = Path(__file__).resolve().parent
    # scripts/ is directly under the repo root
    return here.parent


def find_scraper_modules(courts_dir: Path) -> list[str]:
    """Find all Python modules under courts/ that define ``def default_config``.

    Returns a list of dotted module paths relative to the src/ directory,
    e.g. ``courts.ca.cc_tentatives``.
    """
    modules: list[str] = []
    src_dir = courts_dir.parent  # courts_dir is src/courts, src_dir is src/

    for py_file in sorted(courts_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue

        try:
            content = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        # Look for a top-level function definition: def default_config(
        if re.search(r"^def default_config\(", content, re.MULTILINE):
            # Convert file path to dotted module path relative to src/
            rel = py_file.relative_to(src_dir)
            module_path = ".".join(rel.with_suffix("").parts)
            modules.append(module_path)

    return modules


def find_registered_modules(runner_path: Path) -> set[str]:
    """Parse runner.py to extract all module paths imported in _build_registry().

    Looks for ``from <module> import ...`` lines inside the ``_build_registry``
    function body and returns the set of unique module paths.
    """
    content = runner_path.read_text(encoding="utf-8")
    registered: set[str] = set()

    # Find the _build_registry function body
    match = re.search(r"^def _build_registry\(\).*?:", content, re.MULTILINE)
    if not match:
        print("ERROR: Could not find _build_registry() in runner.py", file=sys.stderr)
        sys.exit(2)

    # Extract the function body (everything indented after the def line)
    func_start = match.end()
    lines = content[func_start:].split("\n")

    for line in lines:
        # Stop at the next top-level definition (unindented non-blank line)
        stripped = line.rstrip()
        if stripped and not stripped.startswith(" ") and not stripped.startswith("\t"):
            break

        # Match: from courts.ca.cc_tentatives import ...
        import_match = re.match(r"\s+from (courts\.\S+) import", line)
        if import_match:
            registered.add(import_match.group(1))

    return registered


def main() -> int:
    """Run the registry completeness check."""
    verbose = "--verbose" in sys.argv

    repo_root = find_repo_root()
    courts_dir = repo_root / "packages" / "scraper-framework" / "src" / "courts"
    runner_path = (
        repo_root / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"
    )

    if not courts_dir.is_dir():
        print(f"ERROR: Cannot find courts directory at {courts_dir}", file=sys.stderr)
        return 2

    if not runner_path.is_file():
        print(f"ERROR: Cannot find runner.py at {runner_path}", file=sys.stderr)
        return 2

    scraper_modules = find_scraper_modules(courts_dir)
    registered_modules = find_registered_modules(runner_path)

    if verbose:
        print(f"Found {len(scraper_modules)} scraper module(s) with default_config:")
        for mod in scraper_modules:
            status = "REGISTERED" if mod in registered_modules else "MISSING"
            print(f"  {status}: {mod}")
        print()

    unregistered = [
        mod
        for mod in scraper_modules
        if mod not in registered_modules and mod not in RETIRED_MODULES
    ]

    if unregistered:
        print(
            f"ERROR: {len(unregistered)} scraper module(s) not registered in runner.py:"
        )
        print()
        for mod in unregistered:
            print(f"  {mod}")
        print()
        print("Fix: Add import lines for the missing module(s) to _build_registry()")
        print("  in packages/scraper-framework/src/framework/runner.py")
        print()
        print("  Example:")
        for mod in unregistered:
            parts = mod.split(".")
            module_name = parts[-1]
            print(f"    from {mod} import <ScraperClass>")
            print(f"    from {mod} import default_config as {module_name}_config")
        print()
        print("See https://github.com/judgemind/judgemind/issues/2132 for context.")
        return 1

    print(f"All {len(scraper_modules)} scraper module(s) are registered in runner.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
