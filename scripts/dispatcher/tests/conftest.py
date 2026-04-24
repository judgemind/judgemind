"""Shared pytest configuration for the dispatcher test suite.

Installs a canonical ``psycopg`` stub at collection time so every
``test_daemon_*.py`` module imports against the same object regardless
of collection order.  Individual test files no longer need their own
preamble guards.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


class _UniqueViolation(Exception):
    """Test sentinel — real Exception subclass so daemon's except clauses work."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules["psycopg"] = _psycopg_stub  # always overwrite (collection runs first)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so ``pytest -m integration`` works without warnings."""
    config.addinivalue_line(
        "markers",
        "integration: tests exercising real subprocess/filesystem/git — excluded from default CI run",
    )


@pytest.fixture
def psycopg_stub() -> MagicMock:
    """Return the conftest-installed psycopg stub for tests that need to wire .connect, etc."""
    return sys.modules["psycopg"]  # type: ignore[return-value]
