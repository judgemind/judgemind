#!/usr/bin/env python3
"""Test that scraper-framework internal modules are importable.

Oneshot scripts often import from the installed scraper-framework package
(e.g. framework.db, ingestion.models).  This test verifies that those
internal modules are accessible in the container.
"""

from __future__ import annotations

import importlib
import sys

# Modules from the scraper-framework package that oneshot scripts use.
# Keep in sync with packages/scraper-framework/src/ — if a module is
# removed or renamed, update this list.
FRAMEWORK_MODULES = [
    "framework",
    "framework.base",
    "framework.models",
    "framework.storage",
    "courts",
    "ingestion",
    "ingestion.db",
    "validation",
]

failures = []

for module_name in FRAMEWORK_MODULES:
    try:
        importlib.import_module(module_name)
        print(f"  OK: {module_name}")
    except ImportError as e:
        print(f"  FAIL: {module_name}: {e}")
        failures.append(module_name)

if failures:
    print(f"\n{len(failures)} framework import(s) failed: {', '.join(failures)}")
    sys.exit(1)

print(f"\nAll {len(FRAMEWORK_MODULES)} framework imports succeeded.")
