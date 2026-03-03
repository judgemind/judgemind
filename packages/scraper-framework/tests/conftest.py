"""Shared fixtures for scraper-framework tests.

Performance optimisation (see GitHub issue #66):
``_extract_pdf_text`` uses pdfplumber which takes ~2 s per call on a 36-page
PDF.  In "full run" tests the mock HTTP layer returns the *same* PDF bytes for
every department link (33 for OC, 17 for Riverside), so parsing is repeated
dozens of times for the same content.

We monkeypatch ``_extract_pdf_text`` with a thin memoisation wrapper so the
first call still exercises the real pdfplumber code-path (preserving coverage)
but subsequent identical inputs are served from cache.
"""

from __future__ import annotations

import functools

import pytest

from courts.ca import pdf_link_scraper as _pls


@pytest.fixture(autouse=True)
def _cache_pdf_text_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap ``_extract_pdf_text`` with an LRU cache keyed on the raw bytes."""
    _real = _pls._extract_pdf_text

    @functools.lru_cache(maxsize=8)
    def _cached(pdf_bytes: bytes) -> str:  # noqa: ANN001
        return _real(pdf_bytes)

    monkeypatch.setattr(_pls, "_extract_pdf_text", _cached)
