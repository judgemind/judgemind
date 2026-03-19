"""Shared test configuration for the telegram-bridge package.

Adds the ``scripts/`` directory to ``sys.path`` so that scripts like
``tg-responder.py`` can be imported during tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add the scripts/ directory to sys.path so that script imports resolve
# when importing scripts like tg-responder.py.
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
