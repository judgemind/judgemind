"""Court-specific test fixtures.

This conftest is intentionally minimal — the shared session-scoped fixtures
(PDF text cache, fast retry sleeps, mock enrichment engine) are defined in
the parent ``tests/conftest.py`` and are inherited automatically by pytest.

Court-specific test files live in this ``tests/courts/`` subdirectory so that
CI can select them by directory (``pytest tests/courts/``) instead of
maintaining fragile explicit file lists.  See #1564.

IMPORTANT: Do NOT add an ``__init__.py`` to this directory.  The directory
name ``courts`` would shadow the installed ``courts`` package and break all
imports in the parent conftest and test files.
"""
