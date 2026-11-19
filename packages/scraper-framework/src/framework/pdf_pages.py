"""The one way to loop over a pdfplumber PDF's pages (#5147).

pdfplumber caches each ``Page``'s parsed layout (``_layout``, ``_objects``,
the chars, the ``get_textmap`` cache) on the ``Page`` object, and
``PDF.pages`` holds every ``Page`` until the PDF closes.  A plain
``for page in pdf.pages`` loop therefore keeps the whole document's layout
alive: a real 58-page Santa Clara PDF took the dev ingestion worker from
152 MB to 451 MB RSS, and since the freed heap is not handed back to the
OS, the 512 MB worker then idled near its limit and was OOM-killed every
30-90 minutes.

``iter_pdf_pages`` closes each page (``Page.close()``: flush the caches)
once the loop moves past it, so a document costs about one page's layout
whatever its length.  A closed page's cached data is gone: keep what you
extracted, not the page.  ``tests/test_pdf_pages_memory_5147.py`` fails CI
on any ``for ... in ....pages`` loop in ``src/`` outside this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_pdf_pages(pdf: Any) -> Iterator[Any]:
    """Yield each page of an open pdfplumber *pdf*, closing it when the loop
    moves on: on the next step, ``continue``, ``break`` (generator close) or
    an exception."""
    for page in pdf.pages:
        try:
            yield page
        finally:
            page.close()
