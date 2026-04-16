"""Scraper registry tests -- REMOVED in #2289.

The reingest script no longer maintains its own scraper registry.
Document processing is delegated to ``IngestionWorker.process_event()``,
which has its own scraper discovery via the framework's LlmExtractor
and per-scraper parse_document() calls.

Scraper discoverability is still tested by:
- ``tests/test_worker.py`` (IngestionWorker integration tests)
- ``tests/test_scraper_base.py`` (BaseScraper contract tests)
"""
