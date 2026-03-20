"""Shared fixtures for scraper-framework tests.

Performance optimisation (see GitHub issues #66, #1207):
``_extract_pdf_text`` / ``extract_pdf_text`` uses pdfplumber which takes ~2 s
per call on a multi-page PDF.  In "full run" tests the mock HTTP layer returns
the *same* PDF bytes for every department link (300+ for CC, 33 for OC, 20 for
Fresno, 17 for Riverside), so parsing is repeated hundreds of times for the
same content.

Session-scoped caching patches every module that defines or imports a PDF text
extraction function.  The first call per unique ``pdf_bytes`` still exercises
the real pdfplumber code-path (preserving coverage) but subsequent identical
inputs are served from an in-memory cache shared across the whole test session.

Additionally, ``time.sleep`` in the retry module is replaced with a no-op so
that retry-backoff tests (which sleep 2 s + 4 s = 6 s by default) complete
instantly.
"""

from __future__ import annotations

import functools
from collections.abc import Generator
from unittest.mock import patch

import pytest

from courts.ca import cc_tentatives as _cc
from courts.ca import fresno_tentatives as _fresno
from courts.ca import pdf_link_scraper as _pls
from courts.ca import riverside_tentatives as _riv
from courts.ca import sc_tentatives as _sc
from courts.ca import ventura_tentatives as _ven


@pytest.fixture(autouse=True, scope="session")
def _cache_pdf_text_extraction() -> Generator[None, None, None]:
    """Wrap PDF text extraction with a session-wide LRU cache.

    Uses ``unittest.mock.patch`` instead of ``monkeypatch`` because
    ``monkeypatch`` is limited to function scope.  Every module that
    defines *or imports* ``_extract_pdf_text`` / ``extract_pdf_text``
    must be patched so that the imported reference in each module's
    namespace is replaced.
    """
    # --- pdf_link_scraper._extract_pdf_text (OC, Riverside, SB, Fresno, CC) ---
    _real_pls = _pls._extract_pdf_text

    @functools.lru_cache(maxsize=32)
    def _cached_pls(pdf_bytes: bytes) -> str:
        return _real_pls(pdf_bytes)

    # --- sc_tentatives.extract_pdf_text (Santa Clara) ---
    _real_sc = _sc.extract_pdf_text

    @functools.lru_cache(maxsize=32)
    def _cached_sc(pdf_bytes: bytes) -> str:
        return _real_sc(pdf_bytes)

    # --- ventura_tentatives._extract_pdf_text (local copy) ---
    _real_ven = _ven._extract_pdf_text

    @functools.lru_cache(maxsize=32)
    def _cached_ven(pdf_bytes: bytes) -> str:
        return _real_ven(pdf_bytes)

    # Patch every module that holds its own reference to the function.
    patches = [
        patch.object(_pls, "_extract_pdf_text", _cached_pls),
        patch.object(_cc, "_extract_pdf_text", _cached_pls),
        patch.object(_riv, "_extract_pdf_text", _cached_pls),
        patch.object(_fresno, "_extract_pdf_text", _cached_pls),
        patch.object(_sc, "extract_pdf_text", _cached_sc),
        patch.object(_ven, "_extract_pdf_text", _cached_ven),
    ]

    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


@pytest.fixture(autouse=True, scope="session")
def _fast_retry_sleeps() -> Generator[None, None, None]:
    """Replace ``time.sleep`` in the retry module with a no-op.

    Tests that exercise retry paths (e.g. HTTP 503 → retry → fail) would
    otherwise sleep 2 s + 4 s = 6 s per test due to exponential backoff.
    The retry *logic* (attempt counting, exception propagation) is still
    fully exercised — only the wall-clock wait is removed.
    """
    with patch("framework.retry.time.sleep"):
        yield
