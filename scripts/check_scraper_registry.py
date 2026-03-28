#!/usr/bin/env python3
"""Verify California tentative scraper modules are registered in runner.py."""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredScraper:
    """A scraper module that should be present in the runner registry."""

    scraper_id: str
    module_name: str
    module_path: Path


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    if isinstance(base, ast.Call):
        return _base_name(base.func)
    return None


def _module_has_scraper_class(module: ast.Module) -> bool:
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_name = _base_name(base)
            if base_name and base_name.endswith("Scraper"):
                return True
    return False


def _extract_scraper_id_from_default_config(module: ast.Module) -> str | None:
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "default_config":
            continue

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            call_name = _base_name(child.func)
            if call_name != "ScraperConfig":
                continue

            for keyword in child.keywords:
                if keyword.arg != "scraper_id":
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    return keyword.value.value
    return None


def discover_expected_scrapers(courts_dir: Path) -> list[DiscoveredScraper]:
    """Return California tentative scraper modules that require registry entries."""
    scrapers: list[DiscoveredScraper] = []

    for module_path in sorted(courts_dir.glob("*_tentatives.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        scraper_id = _extract_scraper_id_from_default_config(tree)
        if scraper_id is None or not _module_has_scraper_class(tree):
            continue

        scrapers.append(
            DiscoveredScraper(
                scraper_id=scraper_id,
                module_name=module_path.stem,
                module_path=module_path,
            )
        )

    return scrapers


def extract_registered_scraper_ids(runner_path: Path) -> set[str]:
    """Return scraper IDs explicitly registered in framework.runner._REGISTRY."""
    tree = ast.parse(runner_path.read_text(encoding="utf-8"), filename=str(runner_path))
    registered_ids: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "extend":
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "_REGISTRY":
            continue
        if len(node.args) != 1:
            continue

        entries = node.args[0]
        if not isinstance(entries, (ast.List, ast.Tuple)):
            continue

        for entry in entries.elts:
            if not isinstance(entry, ast.Tuple) or not entry.elts:
                continue
            first = entry.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                registered_ids.add(first.value)

    return registered_ids


def find_unregistered_scrapers(courts_dir: Path, runner_path: Path) -> list[DiscoveredScraper]:
    """Return scrapers defined in courts/ca but missing from runner.py."""
    registered_ids = extract_registered_scraper_ids(runner_path)
    return [
        scraper
        for scraper in discover_expected_scrapers(courts_dir)
        if scraper.scraper_id not in registered_ids
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Fail if a California tentative scraper exists but is not registered."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    courts_dir = repo_root / "packages" / "scraper-framework" / "src" / "courts" / "ca"
    runner_path = repo_root / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"

    missing = find_unregistered_scrapers(courts_dir, runner_path)
    if not missing:
        print("All scraper modules are registered in framework.runner._build_registry().")
        return 0

    print("Unregistered scraper modules found:")
    for scraper in missing:
        print(f"- {scraper.scraper_id} ({scraper.module_path.name})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
