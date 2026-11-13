"""PDF-Link Scraper Template (Pattern 2).

Strategy: fetch an index page, discover PDF links, download each PDF,
archive the raw bytes, extract text for parsing.

Used by Orange County, Riverside County, and (future) San Bernardino County.

Verified against live sites 2026-03-02:
  OC:  https://www.occourts.org/online-services/tentative-rulings/civil-tentative-rulings
  RIV: https://www.riverside.courts.ca.gov/online-services/tentative-rulings

Design decisions:
- One CapturedDocument per PDF (one per judge/department)
- raw_content = raw PDF bytes; content_format = PDF
- parse_document() extracts text via pdfplumber, finds case numbers
- PdfLinkConfig encapsulates all per-court differences
- verify_ssl=False required for Riverside (bad cert on live site)
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
import pdfplumber
import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.fetch_tally import FetchTally

logger = structlog.get_logger(__name__)

# Common "no rulings" boilerplate pattern — matches text that starts with
# "No Tentative Ruling(s)" (case-insensitive, optional leading whitespace).
# Used by the default _is_boilerplate() implementation.  Courts like Riverside
# publish placeholder PDFs with this text for departments with no rulings.
_NO_RULINGS_RE = re.compile(
    r"^\s*No\s+Tentative\s+Rulings?\b",
    re.IGNORECASE,
)

# The PDF spec (ISO 32000-1 §7.5.2, Implementation Note 13) lets readers find
# the "%PDF-" header anywhere in the first 1024 bytes.
_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_WINDOW = 1024

# Block reason recorded on a FetchTally when a PDF URL serves something else.
NOT_A_PDF_REASON = "PDF URL returned a body that is not a PDF (no %PDF- header)"


def source_url_filename(source_url: str | None) -> str:
    """Return the decoded last path segment of a captured PDF URL.

    ``hearing_date_for_raw`` hooks read the filename a live capture parsed
    from the link (#4774).  The URL comes from S3 object metadata, which
    may hold it percent-encoded (``403%20Tentative%20Rulings...``) or not.
    Returns ``""`` for an empty URL.
    """
    if not source_url:
        return ""
    path = urlsplit(source_url).path
    return unquote(path.rsplit("/", 1)[-1])


def looks_like_pdf(content: bytes) -> bool:
    """Return True if *content* carries a PDF header in its first 1024 bytes.

    A 200 response at a PDF URL can be an HTML block page, an error page, or
    a login redirect. Archiving that as ``ContentFormat.PDF`` stores garbage
    and reports a green fetch (#4748).
    """
    return _PDF_MAGIC in content[:_PDF_MAGIC_WINDOW]


@dataclass
class PdfLinkConfig:
    """Per-court configuration for the PDF-link scraper template.

    Encapsulates the differences between OC, Riverside, San Bernardino, etc.
    """

    # Index page
    index_url: str
    pdf_base_url: str  # for resolving relative PDF hrefs

    # Link parsing: regex applied to the <a> link text.
    # Must capture named groups: 'department' and 'judge_name'.
    link_text_re: re.Pattern

    # Courthouse mapping: given a department code, return the courthouse name.
    # If None, courthouse is left unset.
    courthouse_from_dept: Callable[[str], str | None] | None = None

    # HTTP
    verify_ssl: bool = True

    # When True (default), links whose text does not match link_text_re are
    # skipped before fetching (#1845).  Set to False for courts where link
    # text doesn't follow a predictable pattern (e.g. Fresno, where link
    # text is a date and department is extracted from the URL filename).
    filter_by_link_text: bool = True

    # Optional regex applied to the PDF URL (or its filename).  When set,
    # links whose URL does not match are skipped before fetching.  Used by
    # courts like Fresno where link text is uninformative but the URL
    # filename encodes a predictable pattern (e.g. "MM-DD-YY-dept-NNN.pdf").
    # This filters out non-ruling PDFs (e-Court transition notices, holiday
    # closure banners, "we moved" announcements) linked from the same index
    # page as tentative rulings (#2644).
    href_filter_re: re.Pattern | None = None

    # Case number regex applied to extracted PDF text
    case_number_re: re.Pattern = field(default_factory=lambda: re.compile(r"\b\d{2}-\d{8}\b"))


class PdfLinkScraper(BaseScraper):
    """Scrapes Pattern-2 courts: PDF links on an index page, one PDF per judge.

    Subclasses can override ``_is_boilerplate`` to detect placeholder PDFs that
    should be silently skipped (e.g. "No Tentative Rulings" pages).  The base
    implementation catches common patterns; subclasses may extend or replace it.
    """

    def __init__(
        self,
        config: ScraperConfig,
        pdf_config: PdfLinkConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._pdf_config = pdf_config

    # ------------------------------------------------------------------
    # Boilerplate detection hook (override in subclasses as needed)
    # ------------------------------------------------------------------

    def _is_boilerplate(self, text: str) -> bool:
        """Return True if *text* is a boilerplate placeholder, not a real ruling.

        Courts sometimes publish PDFs for departments with no rulings that
        contain only instructional text (e.g. "No Tentative Rulings") or a
        department header with no case entries.  These should be skipped
        rather than ingested as UNKNOWN case records.

        The base implementation checks for common "No Tentative Rulings"
        patterns.  Subclasses should call ``super()._is_boilerplate(text)``
        and add court-specific checks.
        """
        return bool(_NO_RULINGS_RE.match(text))

    def _empty_index_block_reason(self, html: str) -> str | None:
        """Explain why an index with no ruling PDF links is not a quiet day.

        Called only when the index yields no ruling PDF links.  Returns
        ``None`` when the page carries the court's recognized "nothing
        posted" marker, which ``_check_listing`` takes as ``empty_state``.
        Otherwise returns a one-line reason that goes into the
        ``EmptyListing`` run error (#4654, #4912).

        The base implementation always returns a reason.  The OC civil and
        probate, Fresno, Riverside, SB and SF Family Law indexes keep weeks
        of past PDFs (6 to 37 ruling links each on 2026-11-13), so none has
        a legitimately empty index.  A court that does must recognize a
        marker taken from a real capture (#4800); OC Family Law's empty
        Family Law Panel is the example.
        """
        return "this court's index has no recognized empty state"

    def _ruling_links(self, links: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Drop index links that are not tentative-ruling PDFs.

        Two per-court filters, each logged per skipped link:

        - ``link_text_re`` on the link text (#1845).  Ruling PDFs carry text
          like "Department X - Honorable Y"; other PDFs on the page (escheat
          notices, admin orders, claim forms) do not.  Courts whose link text
          is unpredictable (Fresno, CC) opt out via ``filter_by_link_text``.
        - ``href_filter_re`` on the URL (#2644).  Used where the link text
          is uninformative (Fresno "March 10, 2026") but the filename is
          strict ("dept-NNN.pdf"), to skip e-Court notices, holiday closure
          banners and other admin PDFs that would become all-NULL rows.
        """
        pc = self._pdf_config
        kept: list[tuple[str, str]] = []
        for href, link_text in links:
            if pc.filter_by_link_text and not pc.link_text_re.search(link_text):
                self._log.warning(
                    "Skipping PDF link — text does not match expected pattern",
                    link_text=link_text,
                    url=href,
                    pattern=pc.link_text_re.pattern,
                )
                continue
            if pc.href_filter_re is not None and not pc.href_filter_re.search(href):
                self._log.warning(
                    "Skipping PDF link — URL does not match expected pattern",
                    link_text=link_text,
                    url=href,
                    pattern=pc.href_filter_re.pattern,
                )
                continue
            kept.append((href, link_text))
        return kept

    def fetch_documents(self) -> list[CapturedDocument]:
        pc = self._pdf_config
        docs = []
        # A run where every PDF GET raises must fail, not record success/0 (#4693).
        tally = FetchTally("PDF fetches")

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            verify=pc.verify_ssl,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            self._log.info("Fetching index page", url=pc.index_url)
            response = client.get(pc.index_url)
            response.raise_for_status()

            links = _extract_pdf_links(response.text, pc.index_url, pc.pdf_base_url)
            self._log.info("Found PDF links", count=len(links))
            ruling_links = self._ruling_links(links)
            # The listing is the links that survive the filters: a renamed
            # link-text or filename pattern skips every link, which is the
            # same silent success/0 as an anchor selector matching nothing
            # (#4912).  Only the court's own empty-state marker makes an
            # empty listing a quiet day.
            reason = None if ruling_links else self._empty_index_block_reason(response.text)
            if links and not ruling_links:
                # PDF links are present but every one was filtered out: the
                # court relabelled them, whatever the empty-state hook says.
                reason = f"{len(links)} PDF link(s), none matching the ruling-link filters" + (
                    f"; {reason}" if reason else ""
                )
            self._check_listing(
                ruling_links,
                what=f"{self.config.scraper_id} index PDF links",
                url=pc.index_url,
                body=response.text,
                empty_state=not ruling_links and reason is None,
                detail=reason,
            )

            for href, link_text in ruling_links:
                time.sleep(self.config.request_delay_seconds)
                tally.attempt()
                try:
                    doc = self._fetch_one_pdf(client, href, link_text)

                    if not looks_like_pdf(doc.raw_content):
                        tally.blocked(NOT_A_PDF_REASON)
                        self._log.error(
                            "pdf_link.not_a_pdf",
                            url=href,
                            link_text=link_text,
                            body_prefix=doc.raw_content[:200].decode("utf-8", "replace"),
                        )
                        continue

                    # Check for boilerplate before adding to results (#322).
                    # Extract text early so subclasses that override
                    # _is_boilerplate can inspect it.
                    try:
                        text = _extract_pdf_text(doc.raw_content)
                    except Exception:
                        text = None

                    if text is not None and self._is_boilerplate(text):
                        self._log.info(
                            "Skipping boilerplate PDF",
                            department=doc.department,
                            judge=doc.judge_name,
                            url=href,
                        )
                        continue

                    docs.append(doc)
                    self._log.debug(
                        "Fetched PDF",
                        judge=doc.judge_name,
                        dept=doc.department,
                        url=href,
                    )
                except Exception as exc:
                    tally.failed(exc)
                    self._log.error(
                        "Failed to fetch PDF",
                        url=href,
                        link_text=link_text,
                        error=str(exc),
                    )

        tally.raise_if_all_failed(docs)
        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        try:
            text = _extract_pdf_text(doc.raw_content)
            doc.ruling_text = text

            case_numbers = self._pdf_config.case_number_re.findall(text)
            if case_numbers:
                doc.case_number = case_numbers[0]
                if len(case_numbers) > 1:
                    doc.extra["all_case_numbers"] = list(dict.fromkeys(case_numbers))
        except Exception as exc:
            self._log.warning("PDF parse error", error=str(exc))
        return doc

    def _fetch_one_pdf(self, client: httpx.Client, href: str, link_text: str) -> CapturedDocument:
        pc = self._pdf_config

        # Parse judge name and department from link text
        judge_name: str | None = None
        department: str | None = None
        courthouse: str | None = None

        m = pc.link_text_re.search(link_text)
        if m:
            gd = m.groupdict()
            department = gd.get("department", "").strip()
            raw_name = (gd.get("judge_name") or "").strip()
            # Normalize whitespace
            judge_name = " ".join(raw_name.split()) if raw_name else None
            if department and pc.courthouse_from_dept:
                courthouse = pc.courthouse_from_dept(department)

        response = client.get(href)
        response.raise_for_status()

        doc = self._make_base_doc(
            source_url=href,
            raw_content=response.content,
            content_format=ContentFormat.PDF,
        )
        doc.judge_name = judge_name
        doc.department = department
        doc.courthouse = courthouse
        doc.extra["link_text"] = link_text
        return doc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_pdf_links(html: str, index_url: str, pdf_base_url: str) -> list[tuple[str, str]]:
    """Return list of (absolute_pdf_url, link_text) for all PDF links on the page."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        # Resolve relative URLs against the pdf_base_url
        if href.startswith("http"):
            abs_url = href
        else:
            abs_url = urljoin(pdf_base_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        # Normalize non-breaking spaces in link text
        link_text = a.get_text(separator=" ", strip=True).replace("\xa0", " ")
        results.append((abs_url, link_text))

    return results


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract full text from a PDF using pdfplumber."""
    import io

    lines = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)
