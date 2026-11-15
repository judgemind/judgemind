"""The one PDF page renderer for every LLM image path (#5053).

OCR (``ingestion.llm_extract``) and the multimodal split
(``framework.llm_extractor.extract_from_pdf``) both send PDF pages to a
vision model as PNGs.  Each used to render every page before the first
call and hold them all, while MuPDF kept every page's decoded images in
its store (up to 256 MB by default).  A 25-page scan OOM-killed the
512 MB live ingestion worker that way (#5010 / #5042); the multimodal
path, which reads up to 500 pages since #4995, had the same loop.

Here a page is rendered only when it is asked for, and MuPDF's store is
emptied after every page, so a document costs about one page's memory
whatever its length.  ``tests/test_pdf_render_memory_5053.py`` measures
the peak on both paths and fails CI on a render-everything regression.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

#: Resolution (DPI) every PDF page is rendered at for an LLM.  150 DPI
#: balances readability with image size (token cost); validated in the OC
#: multimodal eval (PR #1692).
RENDER_DPI = 150

#: Media type of a rendered page.
PNG_MEDIA_TYPE = "image/png"


def render_page(doc: object, index: int, *, dpi: int = RENDER_DPI) -> bytes:
    """Page *index* (0-based) of an open pymupdf document as a PNG at *dpi*.

    Empties MuPDF's store afterwards: it keeps decoded page images (a
    scan's full-resolution image, one per page) until the store is full,
    so without this a long scan costs the whole document's memory, not
    one page's.
    """
    import pymupdf

    zoom = dpi / 72.0
    pix = doc[index].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))  # type: ignore[index]
    png = pix.tobytes("png")
    del pix
    pymupdf.TOOLS.store_shrink(100)
    return png


class PdfPageImages(Sequence[tuple[bytes, str]]):
    """The first *max_pages* pages of a PDF as ``(png_bytes, media_type)``,
    rendered lazily.

    A sequence, so a caller can take ``len()`` and walk it like the list
    it replaces, but no page is rendered until it is reached: iterating
    renders one page per step (at most the current page is alive), and
    indexing renders just that page.  Opening the PDF validates it, so an
    unreadable PDF raises here, before any page is sent.
    """

    def __init__(self, pdf_bytes: bytes, max_pages: int, *, dpi: int = RENDER_DPI) -> None:
        import pymupdf

        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            self.total_pages = len(doc)
        self._pdf_bytes = pdf_bytes
        self._dpi = dpi
        # page-cap: callers flag a PDF over max_pages as incomplete (#4995).
        self._count = min(self.total_pages, max(0, max_pages))

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[tuple[bytes, str]]:
        import pymupdf

        with pymupdf.open(stream=self._pdf_bytes, filetype="pdf") as doc:
            for index in range(self._count):
                yield render_page(doc, index, dpi=self._dpi), PNG_MEDIA_TYPE

    def __getitem__(self, index: int) -> tuple[bytes, str]:  # type: ignore[override]
        import pymupdf

        if not isinstance(index, int):
            # A slice would render all its pages at once: walk the pages.
            raise TypeError("PdfPageImages takes an int index, not a slice")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        with pymupdf.open(stream=self._pdf_bytes, filetype="pdf") as doc:
            return render_page(doc, index, dpi=self._dpi), PNG_MEDIA_TYPE
