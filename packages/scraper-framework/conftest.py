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
