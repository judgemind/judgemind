"""Shared fixtures for scraper-framework tests.

Performance optimisation (see GitHub issue #66):
``_extract_pdf_text`` / ``extract_pdf_text`` uses pdfplumber which takes ~2 s
per call on a 36-page PDF.  In "full run" tests the mock HTTP layer returns the
*same* PDF bytes for every department link (33 for OC, 17 for Riverside, 20 for
SC), so parsing is repeated dozens of times for the same content.

We monkeypatch the extraction functions with a thin memoisation wrapper so the
first call still exercises the real pdfplumber code-path (preserving coverage)
but subsequent identical inputs are served from cache.
"""

from __future__ import annotations

import functools

import pytest

from courts.ca import pdf_link_scraper as _pls
from courts.ca import sc_tentatives as _sc


@pytest.fixture(autouse=True)
def _cache_pdf_text_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap PDF text extraction with an LRU cache keyed on the raw bytes."""
    # Cache for pdf_link_scraper (OC, Riverside, SB)
    _real_pls = _pls._extract_pdf_text

    @functools.lru_cache(maxsize=8)
    def _cached_pls(pdf_bytes: bytes) -> str:  # noqa: ANN001
        return _real_pls(pdf_bytes)

    monkeypatch.setattr(_pls, "_extract_pdf_text", _cached_pls)

    # Cache for sc_tentatives (Santa Clara)
    _real_sc = _sc.extract_pdf_text

    @functools.lru_cache(maxsize=8)
    def _cached_sc(pdf_bytes: bytes) -> str:  # noqa: ANN001
        return _real_sc(pdf_bytes)

    monkeypatch.setattr(_sc, "extract_pdf_text", _cached_sc)
