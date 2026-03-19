#!/usr/bin/env python3
"""Test that the _venv_helper stub works correctly in the ECS environment.

In the real ECS container, ecs-run-task.sh creates a stub _venv_helper
module at /tmp/_venv_helper.py and prepends /tmp to PYTHONPATH.  This
test verifies that:

1. The stub is importable
2. ensure_venv() is callable and does nothing (no-op)
3. Subsequent imports of real packages still work
"""

from __future__ import annotations

import sys

# This import should succeed via the stub created by the test runner
from _venv_helper import ensure_venv

# Call ensure_venv — should be a no-op in the container
ensure_venv("scraper-framework")

# Verify that real imports still work after the stub
import httpx  # noqa: E402, F401
import structlog  # noqa: E402, F401

print("_venv_helper stub works correctly.")
print(f"  _venv_helper module location: {sys.modules['_venv_helper'].__file__}")
print(f"  ensure_venv is callable: {callable(ensure_venv)}")
print(f"  Post-stub imports succeeded.")
