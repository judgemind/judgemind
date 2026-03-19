#!/usr/bin/env python3
"""Test that common dependencies used by oneshot scripts are importable.

This script verifies that all non-optional dependencies from the
scraper-framework pyproject.toml are installed and importable in the
container environment.  If any import fails, the script exits with
a non-zero status.
"""

from __future__ import annotations

import importlib
import sys

# Mapping of Python import names to the pip package they come from.
# These are the deps that oneshot scripts commonly use.
REQUIRED_IMPORTS = {
    "httpx": "httpx",
    "boto3": "boto3",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "pdfplumber": "pdfplumber",
    "redis": "redis",
    "pydantic": "pydantic",
    "structlog": "structlog",
    "opensearchpy": "opensearch-py",
    "psycopg": "psycopg",
    "anthropic": "anthropic",
}

failures = []

for module_name, package_name in REQUIRED_IMPORTS.items():
    try:
        importlib.import_module(module_name)
        print(f"  OK: {module_name} ({package_name})")
    except ImportError as e:
        print(f"  FAIL: {module_name} ({package_name}): {e}")
        failures.append(module_name)

if failures:
    print(f"\n{len(failures)} import(s) failed: {', '.join(failures)}")
    sys.exit(1)

print(f"\nAll {len(REQUIRED_IMPORTS)} imports succeeded.")
