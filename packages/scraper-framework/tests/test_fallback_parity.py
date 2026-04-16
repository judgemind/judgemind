"""Fallback parity tests -- REMOVED in #2289.

This test file previously verified parity between the worker's regex
fallback chain and reingest's ``_apply_regex_fallbacks()``.

After #2289, reingest delegates entirely to
``IngestionWorker.process_event()``.  There is no separate extraction
path to maintain parity with -- both live ingestion and reingest use
the same worker code.

The regex fallback chain is still tested by the worker's own tests in
``tests/test_worker.py``.
"""
