"""Shared test configuration for the telegram-bridge package.

Sets up the environment so that scripts that use ``_venv_helper.ensure_venv()``
(e.g. ``scripts/tg-responder.py``) can be imported without the helper trying
to re-exec into a package venv.  The ``scripts/`` directory is also added to
``sys.path`` so that ``_venv_helper`` itself can be imported.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Skip venv auto-detection in all tests — we are already running inside the
# test venv and do not want ensure_venv() to call os.execv().
os.environ["_VENV_HELPER_SKIP"] = "1"

# Add the scripts/ directory to sys.path so that ``from _venv_helper import
# ensure_venv`` resolves when importing scripts like tg-responder.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


@pytest.fixture(autouse=True)
def _clear_anthropic_client_cache() -> None:
    """Clear the interpreter's module-level Anthropic client cache before each test.

    The interpreter caches ``anthropic.Anthropic`` instances by API key for
    connection reuse.  Without clearing between tests, a mock from one test
    can leak into subsequent tests that patch ``anthropic.Anthropic`` with a
    different mock.
    """
    from telegram_bridge.interpreter import clear_client_cache

    clear_client_cache()
