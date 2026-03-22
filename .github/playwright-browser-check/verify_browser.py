#!/usr/bin/env python3
"""Verify that Playwright's Chromium browser binary is accessible.

This script is run inside the scraper-framework Docker image as the
non-root ``scraper`` user to catch path/permission mismatches like #1625.

It checks:
  1. Playwright can resolve the Chromium executable path.
  2. The resolved path exists on disk.
  3. The file is executable by the current user.

Exit codes:
  0 — all checks passed
  1 — one or more checks failed
"""
from __future__ import annotations

import os
import stat
import sys


def main() -> int:
    errors: list[str] = []

    # Step 1: Resolve the Chromium executable path via Playwright
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            executable_path = p.chromium.executable_path
    except Exception as exc:
        print(f"FAIL: Could not resolve Chromium executable path: {exc}")
        return 1

    print(f"Chromium executable path: {executable_path}")

    # Step 2: Verify the binary exists on disk
    if not os.path.exists(executable_path):
        errors.append(f"Binary does not exist at {executable_path}")
    else:
        print(f"Binary exists at {executable_path}")

    # Step 3: Verify the binary is executable by the current user
    if os.path.exists(executable_path):
        file_stat = os.stat(executable_path)
        mode = file_stat.st_mode
        is_executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        if not is_executable:
            errors.append(
                f"Binary at {executable_path} is not executable "
                f"(mode: {oct(mode)})"
            )
        elif not os.access(executable_path, os.X_OK):
            errors.append(
                f"Binary at {executable_path} is not executable by current "
                f"user (uid={os.getuid()}, gid={os.getgid()})"
            )
        else:
            print(f"Binary is executable by current user (uid={os.getuid()})")

    if errors:
        for err in errors:
            print(f"FAIL: {err}")
        return 1

    print("PASS: Playwright Chromium browser binary is accessible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
