"""Memory guards for pdfplumber page loops (#5147).

pdfplumber caches every ``Page``'s parsed layout (``_layout``, ``_objects``,
chars, the ``get_textmap`` lru cache) on the ``Page`` object, and
``PDF.pages`` holds every ``Page`` until the PDF closes.  A plain
``for page in pdf.pages`` loop therefore keeps the whole document's layout
alive: a real 58-page Santa Clara PDF peaked ~300 MB over baseline that
way, and the 512 MB dev ingestion worker was OOM-killed every 30-90 min.
``framework.pdf_pages.iter_pdf_pages`` closes each page once the loop
moves past it.

These tests fail on a regression to an unclosed loop:

- a source guard: no ``for <name> in <expr>.pages`` loop in ``src/``
  outside ``framework/pdf_pages.py``;
- in process, deterministically: every page loop in ``src/`` has closed
  page N by the time it reads page N+1, and closes the last page too;
- ``tracemalloc``: the peak Python allocation of ``_read_pdf_pages`` on a
  dense 16-page text PDF is not much more than on a 2-page one.
"""

from __future__ import annotations

import ast
import gc
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pdfplumber
import pdfplumber.page
import pymupdf
import pytest

from courts.ca import la_dept_judges, sb_dept_judges, sc_tentatives, sf_dept_judges
from framework.pdf_pages import iter_pdf_pages
from ingestion import llm_extract

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
HELPER = SRC_DIR / "framework" / "pdf_pages.py"

#: How much more (MB) the 16-page PDF peak may be than the 2-page one.
#: Closed per page, the peak does not depend on the page count (measured:
#: well under 1 MB apart); unclosed, every page keeps its layout
#: (measured ~9 MB of Python objects per dense page here, ~130 MB in total).
PAGE_SCALING_BUDGET_MB = 8
LONG_PAGES = 16
SHORT_PAGES = 2


def _text_pdf(pages: int, lines: int = 50) -> bytes:
    """A dense text PDF: *lines* lines of ~90 characters on every page."""
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=612, height=792)
        for i in range(lines):
            page.insert_text(
                (36, 40 + i * 14),
                f"Page {n} line {i} " + "The motion for summary judgment is GRANTED. " * 2,
                fontsize=8,
            )
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# Source guard
# ---------------------------------------------------------------------------


def _page_loops(path: Path) -> list[int]:
    """Line numbers of ``for <name> in <expr>.pages`` loops in *path*."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        node.iter.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension)
        and isinstance(node.iter, ast.Attribute)
        and node.iter.attr == "pages"
    ]


def test_no_unclosed_pdfplumber_page_loops_in_src() -> None:
    offenders = [
        f"{path.relative_to(SRC_DIR)}:{line}"
        for path in sorted(SRC_DIR.rglob("*.py"))
        if path != HELPER
        for line in _page_loops(path)
    ]
    assert not offenders, (
        "Loop over `pdf.pages` keeps every page's pdfplumber layout alive until "
        "the PDF closes (#5147: the 512 MB ingestion worker OOM-killed). "
        "Use `for page in iter_pdf_pages(pdf):` from framework.pdf_pages instead. "
        f"Offenders: {offenders}"
    )


def test_source_guard_detects_a_page_loop(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f(pdf):\n"
        "    for page in pdf.pages:\n"
        "        pass\n"
        "    return [p.extract_text() for p in pdf.pages]\n"
        "def g(pdf):\n"
        "    for page in iter_pdf_pages(pdf):\n"
        "        pass\n"
    )
    assert sorted(_page_loops(sample)) == [2, 4]


# ---------------------------------------------------------------------------
# iter_pdf_pages
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePdf:
    def __init__(self, n: int) -> None:
        self.pages = [_FakePage() for _ in range(n)]


def test_iter_pdf_pages_closes_each_page_before_the_next() -> None:
    pdf = _FakePdf(3)
    seen: list[list[bool]] = []
    for page in iter_pdf_pages(pdf):
        assert not page.closed
        seen.append([p.closed for p in pdf.pages])
    assert seen == [[False, False, False], [True, False, False], [True, True, False]]
    assert all(p.closed for p in pdf.pages)


def test_iter_pdf_pages_closes_on_continue_and_break() -> None:
    pdf = _FakePdf(4)
    for i, _page in enumerate(iter_pdf_pages(pdf)):
        if i == 0:
            continue
        if i == 1:
            break
    gc.collect()
    assert [p.closed for p in pdf.pages] == [True, True, False, False]


def test_iter_pdf_pages_closes_on_exception() -> None:
    pdf = _FakePdf(3)
    with pytest.raises(RuntimeError):
        for _page in iter_pdf_pages(pdf):
            raise RuntimeError("boom")
    gc.collect()
    assert [p.closed for p in pdf.pages] == [True, False, False]


def test_iter_pdf_pages_empty_pdf() -> None:
    assert list(iter_pdf_pages(_FakePdf(0))) == []


# ---------------------------------------------------------------------------
# Every page loop in src/ closes page N before reading page N+1
# ---------------------------------------------------------------------------


class _CloseTracker:
    """Patches pdfplumber ``Page`` so each extraction records whether every
    page read before it has been closed.  Violations are recorded rather
    than raised: several callers swallow pdfplumber exceptions."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.read: list[Any] = []  # pages, in read order (held: ids stay unique)
        self.closed: set[int] = set()
        self.violations: list[int] = []
        real_close = pdfplumber.page.Page.close
        tracker = self

        def close(page: Any) -> None:
            tracker.closed.add(id(page))
            real_close(page)

        monkeypatch.setattr(pdfplumber.page.Page, "close", close)
        for name in ("extract_text", "extract_tables", "extract_words"):
            monkeypatch.setattr(pdfplumber.page.Page, name, self._wrap(name))

    def _wrap(self, name: str) -> Callable[..., Any]:
        real = getattr(pdfplumber.page.Page, name)
        tracker = self

        def wrapped(page: Any, *args: Any, **kwargs: Any) -> Any:
            if not any(p is page for p in tracker.read):
                if any(id(p) not in tracker.closed for p in tracker.read):
                    tracker.violations.append(page.page_number)
                tracker.read.append(page)
            return real(page, *args, **kwargs)

        return wrapped

    def assert_closed_per_page(self, pages: int) -> None:
        assert len(self.read) == pages
        assert self.violations == [], f"pages read while an earlier one was open: {self.violations}"
        assert all(id(p) in self.closed for p in self.read), "the last page was not closed"


def _read_pdf_pages(pdf: bytes) -> object:
    return llm_extract._read_pdf_pages(pdf)


def _sc_format_b(pdf: bytes) -> object:
    return sc_tentatives._split_rulings_format_b("", pdf)


def _sc_dept12(pdf: bytes) -> object:
    return sc_tentatives._dept12_table_entries(pdf)


PAGE_LOOPS: dict[str, Callable[[bytes], object]] = {
    "llm_extract._read_pdf_pages": _read_pdf_pages,
    "sc_tentatives._split_rulings_format_b": _sc_format_b,
    "sc_tentatives._dept12_table_entries": _sc_dept12,
    "sf_dept_judges.parse_dept_listing_pdf": sf_dept_judges.parse_dept_listing_pdf,
    "sb_dept_judges.parse_schedule_pdf": sb_dept_judges.parse_schedule_pdf,
    "la_dept_judges.parse_judicial_directory_pdf": la_dept_judges.parse_judicial_directory_pdf,
}


@pytest.mark.parametrize("name", sorted(PAGE_LOOPS))
def test_page_loop_closes_each_page_before_the_next(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _CloseTracker(monkeypatch)
    PAGE_LOOPS[name](_text_pdf(3, lines=5))
    tracker.assert_closed_per_page(3)


# ---------------------------------------------------------------------------
# tracemalloc: peak does not grow with the page count
# ---------------------------------------------------------------------------


def _peak_mb(fn: Callable[[], object]) -> float:
    gc.collect()
    tracemalloc.start()
    try:
        fn()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak / (1024 * 1024)


@pytest.fixture(scope="module")
def text_pdfs() -> Iterator[dict[int, bytes]]:
    yield {n: _text_pdf(n) for n in (SHORT_PAGES, LONG_PAGES)}


def test_read_pdf_pages_peak_memory_does_not_grow_with_page_count(
    text_pdfs: dict[int, bytes],
) -> None:
    short = _peak_mb(lambda: llm_extract._read_pdf_pages(text_pdfs[SHORT_PAGES]))
    long = _peak_mb(lambda: llm_extract._read_pdf_pages(text_pdfs[LONG_PAGES]))
    assert long - short <= PAGE_SCALING_BUDGET_MB, (short, long)


def test_sc_table_path_peak_memory_does_not_grow_with_page_count(
    text_pdfs: dict[int, bytes],
) -> None:
    short = _peak_mb(lambda: sc_tentatives._dept12_table_entries(text_pdfs[SHORT_PAGES]))
    long = _peak_mb(lambda: sc_tentatives._dept12_table_entries(text_pdfs[LONG_PAGES]))
    assert long - short <= PAGE_SCALING_BUDGET_MB, (short, long)
