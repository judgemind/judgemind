#!/usr/bin/env python3
"""Test that script arguments are passed through correctly.

ECS oneshot scripts receive arguments via ecs-run-task.sh.  This test
verifies that the argument passing mechanism works, including arguments
with spaces and special characters.
"""

from __future__ import annotations

import sys

expected_args = ["--dry-run", "--county", "Los Angeles", "--limit", "10"]

actual_args = sys.argv[1:]

if actual_args != expected_args:
    print(f"FAIL: argument mismatch")
    print(f"  Expected: {expected_args}")
    print(f"  Actual:   {actual_args}")
    sys.exit(1)

print(f"Arguments passed correctly: {actual_args}")
