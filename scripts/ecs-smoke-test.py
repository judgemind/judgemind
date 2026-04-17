#!/usr/bin/env python3
"""ECS smoke test — verify the ECS oneshot environment is functional.

This script is designed to run as an ECS oneshot task via ecs-run-task.sh.
It performs basic environment checks to catch real-world failures that
Docker-based simulation cannot detect:

  1. Prints a known marker string (for log retrieval verification)
  2. Imports key dependencies to verify they are installed
  3. Checks basic network connectivity (DNS resolution)
  4. Verifies environment variables are injected by ECS

The script is intentionally read-only and idempotent — it makes no DB writes
or state changes.

Exit code 0 means the environment is healthy.
Exit code 1 means one or more checks failed.
"""
# permanent: true

from __future__ import annotations

import importlib
import os
import socket
import sys

# ─── Marker ──────────────────────────────────────────────────────────────────
# This exact string is checked by CI to verify log retrieval works end-to-end.
MARKER = "JUDGEMIND_ECS_SMOKE_TEST_OK"

# ─── Dependency imports ──────────────────────────────────────────────────────
# Same set as .github/ecs-oneshot-test/test-scripts/test_imports.py — kept in
# sync to ensure the real ECS environment matches the Docker simulation.
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

# ─── Environment variables expected from ECS task definition ──────────────────
# These are injected by the task definition (from secrets or environment config).
# We only check that they exist, not their values — values may be sensitive.
EXPECTED_ENV_VARS = [
    "AWS_DEFAULT_REGION",
]


def check_imports() -> list[str]:
    """Verify all required Python packages are importable."""
    failures = []
    for module_name, package_name in REQUIRED_IMPORTS.items():
        try:
            importlib.import_module(module_name)
            print(f"  OK: {module_name} ({package_name})")
        except ImportError as e:
            print(f"  FAIL: {module_name} ({package_name}): {e}")
            failures.append(module_name)
    return failures


def check_network() -> list[str]:
    """Verify basic DNS resolution works (proves VPC/network config is OK)."""
    failures = []
    hosts = [
        "s3.us-west-2.amazonaws.com",
        "ecs.us-west-2.amazonaws.com",
    ]
    for host in hosts:
        try:
            addr = socket.getaddrinfo(host, 443, socket.AF_INET)
            print(f"  OK: {host} -> {addr[0][4][0]}")
        except socket.gaierror as e:
            print(f"  FAIL: {host}: {e}")
            failures.append(host)
    return failures


def check_env_vars() -> list[str]:
    """Verify expected environment variables are set."""
    failures = []
    for var in EXPECTED_ENV_VARS:
        value = os.environ.get(var)
        if value:
            # Mask the value for security
            print(f"  OK: {var} is set")
        else:
            print(f"  FAIL: {var} is not set")
            failures.append(var)
    return failures


def main() -> int:
    """Run all smoke test checks and return exit code."""
    print(f"\n{MARKER}\n")

    all_failures: list[str] = []

    print("=== Dependency Imports ===")
    all_failures.extend(check_imports())

    print("\n=== Network Connectivity ===")
    all_failures.extend(check_network())

    print("\n=== Environment Variables ===")
    all_failures.extend(check_env_vars())

    print(f"\n{'=' * 40}")
    if all_failures:
        print(f"FAILED: {len(all_failures)} check(s) failed")
        return 1

    print("ALL CHECKS PASSED")
    print(f"\n{MARKER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
