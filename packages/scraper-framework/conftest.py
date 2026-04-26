"""Root conftest for scraper-framework package.

Configures pytest to gracefully handle collection errors from test files
that reference scripts with heavy external dependencies (psycopg, boto3,
scraper-framework internals) not installed in the scraper-framework-tests
CI venv.  These tests are NOT currently run in any CI job — they are
suppressed here as a transitional measure while follow-up issues (#3387)
sweep and clean them up.
"""

from __future__ import annotations

# Test files that reference scripts with dependencies (psycopg, boto3,
# framework) not installed in the scraper-framework-tests CI venv.
# NOTE: these tests are NOT run anywhere in CI — they are ignored here
# to prevent collection failures.  Entries are kept while follow-up
# issues clean up the remaining dead tests; do not remove globs until
# the underlying test files are deleted or relocated to scripts/tests/.
collect_ignore_glob = [
    "tests/test_audit*.py",
    "tests/test_backfill*.py",
    "tests/test_cleanup*.py",
    "tests/test_dedup*.py",
    "tests/test_merge*.py",
    "tests/test_riverside_remediation.py",
    "tests/test_reingest_from_s3.py",
    "tests/test_reingest_registry.py",
]


def pytest_configure(config: object) -> None:
    """Patch pytest-xdist DSession to tolerate the worker_workerfinished race.

    pytest-xdist 3.8.0 (and earlier) has a race where ``worker_workerfinished``
    can be called for a node that was already removed from ``_active_nodes``
    during shutdown.  The stock implementation uses ``set.remove()``, which
    raises ``KeyError`` and causes an INTERNALERROR exit code even when all
    tests pass (exit code 3 instead of 0).

    We wrap the method and swallow ``KeyError`` so that the spurious INTERNALERROR
    never reaches the top-level error handler.

    See: https://github.com/pytest-dev/pytest-xdist/issues/1075
    """
    try:
        from xdist.dsession import DSession  # type: ignore[import-untyped]

        _original = DSession.worker_workerfinished

        def _safe_worker_workerfinished(self: object, node: object) -> None:
            try:
                _original(self, node)
            except KeyError:
                # Node already removed from _active_nodes — harmless shutdown race.
                pass

        DSession.worker_workerfinished = _safe_worker_workerfinished  # type: ignore[method-assign]
    except ImportError:
        # pytest-xdist not installed; nothing to patch.
        pass
