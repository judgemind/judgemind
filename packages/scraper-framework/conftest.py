"""Root conftest for scraper-framework package.

Configures pytest to gracefully handle collection errors from test files
that import scripts with heavy external dependencies (psycopg, boto3, etc.)
not available in all CI environments.  These tests are collected and run
in the 'scripts-tests' CI job which has the full dependency set.
"""

from __future__ import annotations

# Test files that import scripts with dependencies beyond the
# scraper-framework package.  These fail to import in CI's
# scraper-framework-tests job but run in the scripts-tests job.
collect_ignore_glob = [
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
